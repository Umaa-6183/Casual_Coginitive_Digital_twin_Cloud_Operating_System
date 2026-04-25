// CCDT Layer-1 Nervous System — Collector Main
//
// Entry point for the eBPF collector process.
// Responsibilities:
//   1. Load all eBPF object files (CO-RE, compiled by Makefile)
//   2. Attach each probe to its kernel hook
//   3. Drain ring buffers concurrently, normalise events
//   4. Publish NormalisedEvents to Kafka topic ccdt.ebpf.events
//   5. Expose Prometheus /metrics and a minimal REST API (/health, /events, /probes)
//   6. Publish periodic topology updates to ccdt.topology.updates
//
// Build: see Makefile
// Run:   ./collector --kafka kafka:9092 --node $(NODE_NAME)

package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
	"github.com/cilium/ebpf/ringbuf"
	"github.com/cilium/ebpf/rlimit"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/segmentio/kafka-go"
)

// ─── CLI flags ────────────────────────────────────────────────────────────────

var (
	flagKafka         = flag.String("kafka", getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"), "Kafka bootstrap servers")
	flagNodeName      = flag.String("node", getenv("NODE_NAME", "unknown-node"), "Kubernetes node name")
	flagListenAddr    = flag.String("listen", getenv("LISTEN_ADDR", ":9100"), "HTTP listen address for metrics + API")
	flagObjectDir     = flag.String("objects", getenv("BPF_OBJECT_DIR", "/app/probes"), "Directory containing compiled .bpf.o files")
	flagTopicEvents   = flag.String("topic-events", "ccdt.ebpf.events", "Kafka topic for eBPF events")
	flagTopicTopology = flag.String("topic-topology", "ccdt.topology.updates", "Kafka topic for topology updates")
	flagPublishInt    = flag.Duration("publish-interval", 5*time.Second, "Topology publish interval")
	flagBatchSize     = flag.Int("batch-size", 100, "Kafka publish batch size")
	flagRingDrainMS   = flag.Int("ring-drain-ms", 50, "Ring buffer drain poll interval (ms)")
)

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// ─── KafkaPublisher interface + implementation ────────────────────────────────

// KafkaPublisher is the interface used by MetricAggregator for topology publishing.
type KafkaPublisher interface {
	Publish(topic, key string, value []byte) error
	Close() error
}

// kafkaWriter wraps kafka-go writer.
type kafkaWriter struct {
	mu     sync.Mutex
	writer *kafka.Writer
}

func newKafkaWriter(brokers string) *kafkaWriter {
	return &kafkaWriter{
		writer: &kafka.Writer{
			Addr:         kafka.TCP(brokers),
			Balancer:     &kafka.LeastBytes{},
			BatchTimeout: 10 * time.Millisecond,
			Async:        false,
			Logger:       kafka.LoggerFunc(log.Printf),
			ErrorLogger:  kafka.LoggerFunc(log.Printf),
		},
	}
}

func (kw *kafkaWriter) Publish(topic, key string, value []byte) error {
	kw.mu.Lock()
	defer kw.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return kw.writer.WriteMessages(ctx, kafka.Message{
		Topic: topic,
		Key:   []byte(key),
		Value: value,
	})
}

func (kw *kafkaWriter) Close() error {
	return kw.writer.Close()
}

// noopPublisher is used when Kafka is unreachable (dev / offline mode).
type noopPublisher struct{}

func (n *noopPublisher) Publish(topic, key string, value []byte) error { return nil }
func (n *noopPublisher) Close() error                                  { return nil }

// ─── eBPF object wrappers ─────────────────────────────────────────────────────
// Each wrapper holds the loaded BPF collection + the ring-buffer reader.

type probeHandle struct {
	name    string
	coll    *ebpf.Collection
	reader  *ringbuf.Reader
	links   []link.Link
}

func (p *probeHandle) close() {
	if p.reader != nil {
		p.reader.Close()
	}
	for _, l := range p.links {
		l.Close()
	}
	if p.coll != nil {
		p.coll.Close()
	}
}

// ─── Loader: load one .bpf.o file and attach its programs ────────────────────

func loadProbe(objectDir, name string) (*probeHandle, error) {
	path := fmt.Sprintf("%s/%s.bpf.o", objectDir, name)

	spec, err := ebpf.LoadCollectionSpec(path)
	if err != nil {
		return nil, fmt.Errorf("load spec %s: %w", path, err)
	}

	coll, err := ebpf.NewCollection(spec)
	if err != nil {
		return nil, fmt.Errorf("new collection %s: %w", path, err)
	}

	h := &probeHandle{name: name, coll: coll}

	// Attach every program in the collection to its section-declared hook
	for progName, prog := range coll.Programs {
		l, err := attachProgram(prog, spec.Programs[progName].SectionName)
		if err != nil {
			log.Printf("WARN: attach %s/%s: %v — skipping", name, progName, err)
			continue
		}
		if l != nil {
			h.links = append(h.links, l)
		}
	}

	// Open ring buffer map named "events"
	rb, ok := coll.Maps["events"]
	if !ok {
		// Not every probe has a ring buffer (some use only array maps)
		return h, nil
	}
	reader, err := ringbuf.NewReader(rb)
	if err != nil {
		h.close()
		return nil, fmt.Errorf("ringbuf reader %s: %w", name, err)
	}
	h.reader = reader

	log.Printf("Probe loaded: %s (%d programs, %d links)", name, len(coll.Programs), len(h.links))
	return h, nil
}

// attachProgram attaches a BPF program based on its ELF section name.
func attachProgram(prog *ebpf.Program, section string) (link.Link, error) {
	pt := prog.Type()

	switch pt {
	case ebpf.TracePoint:
		// section: "tp/<group>/<name>"
		var group, name string
		fmt.Sscanf(section, "tp/%s", &section)
		// Parse "sched/sched_switch" style
		n, _ := fmt.Sscanf(section, "%[^/]/%s", &group, &name)
		if n < 2 {
			return nil, fmt.Errorf("cannot parse tracepoint section: %s", section)
		}
		return link.Tracepoint(group, name, prog, nil)

	case ebpf.Kprobe:
		// section: "kprobe/<func>" or "kretprobe/<func>"
		var funcName string
		if _, err := fmt.Sscanf(section, "kprobe/%s", &funcName); err == nil {
			return link.Kprobe(funcName, prog, nil)
		}
		if _, err := fmt.Sscanf(section, "kretprobe/%s", &funcName); err == nil {
			return link.Kretprobe(funcName, prog, nil)
		}
		return nil, fmt.Errorf("cannot parse kprobe section: %s", section)

	case ebpf.LSM:
		// section: "lsm/<hook>"
		var hook string
		fmt.Sscanf(section, "lsm/%s", &hook)
		return link.AttachLSM(link.LSMOptions{Program: prog})

	default:
		// Raw tracepoint, fexit, etc. — attach via RawTracepoint if possible
		log.Printf("Unhandled program type %v section=%s — skipping attach", pt, section)
		return nil, nil
	}
}

// ─── Ring buffer drain loop ────────────────────────────────────────────────────

// drainProbe reads events from a probe's ring buffer, normalises them, and
// sends them to the outCh channel. Runs until ctx is cancelled or reader errors.
func drainProbe(
	ctx context.Context,
	h *probeHandle,
	norm *Normalizer,
	agg *MetricAggregator,
	outCh chan<- *NormalisedEvent,
	wg *sync.WaitGroup,
) {
	defer wg.Done()

	if h.reader == nil {
		log.Printf("Probe %s: no ring buffer — skipping drain", h.name)
		return
	}

	log.Printf("Draining probe: %s", h.name)

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		rec, err := h.reader.Read()
		if err != nil {
			if err == ringbuf.ErrClosed {
				return
			}
			// LostSamples indicates ring buffer overflow
			if rec.LostSamples > 0 {
				agg.RecordDrop(h.name)
				log.Printf("WARN probe=%s lost_samples=%d", h.name, rec.LostSamples)
			}
			continue
		}

		if rec.LostSamples > 0 {
			agg.RecordDrop(h.name)
		}

		evt, err := normaliseRaw(norm, h.name, rec.RawSample)
		if err != nil {
			log.Printf("WARN normalise %s: %v", h.name, err)
			continue
		}
		if evt == nil {
			continue
		}

		agg.Record(evt)

		select {
		case outCh <- evt:
		case <-ctx.Done():
			return
		}
	}
}

// normaliseRaw dispatches raw bytes to the correct normaliser based on probe name.
func normaliseRaw(norm *Normalizer, probe string, raw []byte) (*NormalisedEvent, error) {
	switch probe {
	case "scheduler":
		return norm.NormaliseSched(raw)
	case "oom_kill":
		return norm.NormaliseOOM(raw)
	case "tcp_retransmit":
		return norm.NormaliseTCP(raw)
	case "syscall":
		return norm.NormaliseSyscall(raw)
	case "file_access":
		return norm.NormaliseFile(raw)
	case "capability":
		return norm.NormaliseCapability(raw)
	default:
		return nil, fmt.Errorf("unknown probe: %s", probe)
	}
}

// ─── Kafka publisher loop ─────────────────────────────────────────────────────

// publishLoop batches events from inCh and writes them to Kafka.
func publishLoop(
	ctx context.Context,
	writer KafkaPublisher,
	topic string,
	inCh <-chan *NormalisedEvent,
	agg *MetricAggregator,
	batchSize int,
	wg *sync.WaitGroup,
) {
	defer wg.Done()

	batch := make([]*NormalisedEvent, 0, batchSize)
	flush := func() {
		if len(batch) == 0 {
			return
		}
		msgs := make([]kafka.Message, 0, len(batch))
		for _, evt := range batch {
			payload, err := json.Marshal(evt)
			if err != nil {
				log.Printf("WARN marshal event: %v", err)
				continue
			}
			msgs = append(msgs, kafka.Message{
				Topic: topic,
				Key:   []byte(evt.NodeName),
				Value: payload,
			})
		}

		// Write as a single batch
		for _, msg := range msgs {
			if err := writer.Publish(msg.Topic, string(msg.Key), msg.Value); err != nil {
				log.Printf("WARN kafka publish: %v", err)
				agg.RecordKafkaPublish(topic, "error")
			} else {
				agg.RecordKafkaPublish(topic, "ok")
			}
		}
		batch = batch[:0]
	}

	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case evt, ok := <-inCh:
			if !ok {
				flush()
				return
			}
			batch = append(batch, evt)
			if len(batch) >= batchSize {
				flush()
			}

		case <-ticker.C:
			flush()

		case <-ctx.Done():
			flush()
			return
		}
	}
}

// ─── In-memory event ring buffer (for /events API) ────────────────────────────

type eventRing struct {
	mu     sync.RWMutex
	events []*NormalisedEvent
	head   int
	size   int
	cap_   int
}

func newEventRing(capacity int) *eventRing {
	return &eventRing{
		events: make([]*NormalisedEvent, capacity),
		cap_:   capacity,
	}
}

func (r *eventRing) push(evt *NormalisedEvent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events[r.head%r.cap_] = evt
	r.head++
	if r.size < r.cap_ {
		r.size++
	}
}

func (r *eventRing) latest(n int) []*NormalisedEvent {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if n > r.size {
		n = r.size
	}
	out := make([]*NormalisedEvent, 0, n)
	start := r.head - n
	if start < 0 {
		start = 0
	}
	for i := start; i < r.head; i++ {
		e := r.events[i%r.cap_]
		if e != nil {
			out = append(out, e)
		}
	}
	// Return newest first
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out
}

// ─── HTTP server ───────────────────────────────────────────────────────────────

func startHTTPServer(
	addr string,
	ring *eventRing,
	agg *MetricAggregator,
	probes []*probeHandle,
) *http.Server {
	mux := http.NewServeMux()

	// Prometheus metrics
	mux.Handle("/metrics", promhttp.Handler())

	// Health probe
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"status":    "ok",
			"service":   "layer1-nervous",
			"node":      *flagNodeName,
			"timestamp": time.Now().UTC(),
		})
	})

	// Recent events (last N from ring buffer)
	mux.HandleFunc("/events", func(w http.ResponseWriter, r *http.Request) {
		n := 100
		fmt.Sscanf(r.URL.Query().Get("limit"), "%d", &n)
		if n < 1 || n > 500 {
			n = 100
		}
		events := ring.latest(n)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"events": events,
			"total":  len(events),
			"node":   *flagNodeName,
		})
	})

	// Probe status
	mux.HandleFunc("/probes", func(w http.ResponseWriter, r *http.Request) {
		type probeInfo struct {
			Name    string `json:"name"`
			Status  string `json:"status"`
			HasRing bool   `json:"has_ring_buffer"`
		}
		infos := make([]probeInfo, 0, len(probes))
		for _, p := range probes {
			infos = append(infos, probeInfo{
				Name:    p.name,
				Status:  "active",
				HasRing: p.reader != nil,
			})
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"probes": infos,
			"total":  len(infos),
			"node":   *flagNodeName,
		})
	})

	// Per-node metrics snapshot
	mux.HandleFunc("/node-metrics", func(w http.ResponseWriter, r *http.Request) {
		payload, err := agg.TopologyPayload()
		if err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(payload)
	})

	srv := &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("HTTP server listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("HTTP server error: %v", err)
		}
	}()

	return srv
}

// ─── Main ─────────────────────────────────────────────────────────────────────

func main() {
	flag.Parse()

	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)
	log.Printf("CCDT Layer-1 Nervous System starting")
	log.Printf("  node=%s kafka=%s listen=%s", *flagNodeName, *flagKafka, *flagListenAddr)

	// Remove memory lock limit so eBPF maps can be locked in RAM
	if err := rlimit.RemoveMemlock(); err != nil {
		log.Fatalf("remove memlock: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// ── Load all probes ──────────────────────────────────────────────────────
	probeNames := []string{
		"scheduler",
		"oom_kill",
		"tcp_retransmit",
		"syscall",
		"file_access",
		"capability",
	}

	var loadedProbes []*probeHandle
	for _, name := range probeNames {
		h, err := loadProbe(*flagObjectDir, name)
		if err != nil {
			log.Printf("WARN: probe %s failed to load: %v — continuing without it", name, err)
			continue
		}
		loadedProbes = append(loadedProbes, h)
	}

	if len(loadedProbes) == 0 {
		log.Fatal("No probes loaded — cannot continue")
	}
	log.Printf("%d/%d probes loaded", len(loadedProbes), len(probeNames))

	// ── Setup event pipeline ────────────────────────────────────────────────
	norm    := NewNormalizer(*flagNodeName)
	agg     := NewMetricAggregator(*flagNodeName)
	ring    := newEventRing(500)
	outCh   := make(chan *NormalisedEvent, 4096)
	var wg  sync.WaitGroup

	// ── Kafka writer ────────────────────────────────────────────────────────
	var publisher KafkaPublisher
	kw := newKafkaWriter(*flagKafka)

	// Test connectivity with a short timeout
	testCtx, testCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer testCancel()
	testMsg := kafka.Message{Topic: *flagTopicEvents, Key: []byte("ping"), Value: []byte(`{"type":"ping"}`)}
	_ = testCtx
	if err := kw.writer.WriteMessages(context.Background(), testMsg); err != nil {
		log.Printf("WARN: Kafka unreachable (%v) — using noop publisher (events will not be forwarded)", err)
		publisher = &noopPublisher{}
	} else {
		publisher = kw
		log.Printf("Kafka connected: %s", *flagKafka)
	}
	defer publisher.Close()

	// ── Start ring buffer drainers ──────────────────────────────────────────
	for _, h := range loadedProbes {
		wg.Add(1)
		go drainProbe(ctx, h, norm, agg, outCh, &wg)
	}

	// ── Event fan-out: ring + Kafka ─────────────────────────────────────────
	kafkaCh := make(chan *NormalisedEvent, 4096)
	go func() {
		for evt := range outCh {
			ring.push(evt)
			select {
			case kafkaCh <- evt:
			default:
				// kafkaCh full — drop to avoid blocking probe drainers
				agg.RecordDrop("kafka_fanout")
			}
		}
	}()

	// ── Kafka publish loop ──────────────────────────────────────────────────
	wg.Add(1)
	go publishLoop(ctx, publisher, *flagTopicEvents, kafkaCh, agg, *flagBatchSize, &wg)

	// ── Periodic topology publisher ─────────────────────────────────────────
	stopTopology := make(chan struct{})
	agg.StartPeriodicPublisher(publisher, *flagTopicTopology, *flagPublishInt, stopTopology)

	// ── HTTP server ─────────────────────────────────────────────────────────
	srv := startHTTPServer(*flagListenAddr, ring, agg, loadedProbes)

	// ── Signal handling ─────────────────────────────────────────────────────
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	sig := <-sigCh
	log.Printf("Received signal %v — shutting down", sig)

	// Graceful shutdown sequence
	cancel() // cancel context → drainers + publisher stop

	close(stopTopology)

	// Close all probe readers (unblocks drainProbe goroutines)
	for _, h := range loadedProbes {
		h.close()
	}

	close(outCh)

	// Wait for all goroutines
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		log.Printf("WARN: shutdown timeout — forcing exit")
	}

	// Shutdown HTTP server
	shutCtx, shutCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutCancel()
	srv.Shutdown(shutCtx)

	log.Printf("CCDT Layer-1 Nervous System stopped")
}

// ─── Suppress unused import error for unsafe ──────────────────────────────────
var _ = unsafe.Sizeof
