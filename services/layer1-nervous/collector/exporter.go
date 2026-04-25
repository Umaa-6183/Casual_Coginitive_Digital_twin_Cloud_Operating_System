// CCDT Layer-1 Nervous System — Metrics Exporter
//
// Maintains per-node sliding-window metric aggregations derived from the
// stream of NormalisedEvents. Exposes Prometheus metrics and publishes
// topology update payloads to Kafka every 5 seconds.

package main

import (
	"encoding/json"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// ─── Prometheus metric definitions ───────────────────────────────────────────

var (
	// Scheduler
	schedLatencyNS = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "sched_latency_nanoseconds",
		Help:      "Task run-queue latency in nanoseconds.",
		Buckets:   []float64{1e5, 5e5, 1e6, 5e6, 1e7, 5e7, 1e8, 5e8, 1e9},
	}, []string{"node", "cpu"})

	// OOM
	oomKillsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "oom_kills_total",
		Help:      "Total OOM kill events observed.",
	}, []string{"node", "cgroup"})

	// TCP
	tcpRetransmitsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "tcp_retransmits_total",
		Help:      "Total TCP retransmit events observed.",
	}, []string{"node", "dst_port"})

	tcpRTTMicros = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "tcp_rtt_microseconds",
		Help:      "TCP round-trip time in microseconds.",
		Buckets:   []float64{100, 500, 1000, 5000, 10000, 50000, 100000, 500000},
	}, []string{"node", "dst_port"})

	// Syscalls
	syscallEventsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "syscall_events_total",
		Help:      "Total security syscall events observed.",
	}, []string{"node", "syscall", "severity"})

	// File access
	fileEventsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "file_events_total",
		Help:      "Total sensitive file access events.",
	}, []string{"node", "severity"})

	// Capability events
	capEventsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "capability_events_total",
		Help:      "Total Linux capability acquisition events.",
	}, []string{"node", "cap_name", "severity"})

	// Ring buffer
	ringbufDropped = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "ringbuf_dropped_total",
		Help:      "Total events dropped due to ring buffer overflow.",
	}, []string{"node", "probe"})

	// Events processed
	eventsProcessedTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "events_processed_total",
		Help:      "Total normalised events processed by the collector.",
	}, []string{"node", "type", "severity"})

	// Kafka publish
	kafkaPublishTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "ccdt",
		Subsystem: "ebpf",
		Name:      "kafka_publish_total",
		Help:      "Total events published to Kafka.",
	}, []string{"node", "topic", "status"})
)

// ─── Per-node aggregated metrics (for topology update payloads) ───────────────

// NodeMetrics holds rolling aggregations for a single Kubernetes node.
type NodeMetrics struct {
	mu sync.RWMutex

	NodeName string
	// Scheduler
	SchedLatencyP99NS uint64
	SchedEventCount   uint64
	// OOM
	OOMKillCount uint64
	// TCP
	TCPRetransmitRate float64 // retransmits/s over last window
	TCPRTTLastUS      uint64
	TCPRetransmitTot  uint64
	// Security
	SyscallEventCount uint64
	CapEventCount     uint64
	FileEventCount    uint64
	// Severity counts (rolling window)
	CriticalCount uint64
	WarningCount  uint64
	InfoCount     uint64

	// Rolling window data (last 60 s)
	retransmitWindow []timestampedCount
	windowMu         sync.Mutex
}

type timestampedCount struct {
	ts    time.Time
	count uint64
}

// record updates the NodeMetrics from a NormalisedEvent.
func (m *NodeMetrics) record(evt *NormalisedEvent) {
	m.mu.Lock()
	defer m.mu.Unlock()

	switch evt.Severity {
	case SevCritical:
		m.CriticalCount++
	case SevWarning:
		m.WarningCount++
	default:
		m.InfoCount++
	}

	switch evt.Type {
	case EventSched:
		m.SchedEventCount++
		if evt.LatencyNS > m.SchedLatencyP99NS {
			m.SchedLatencyP99NS = evt.LatencyNS
		}
	case EventOOM:
		m.OOMKillCount++
	case EventTCP:
		if evt.TCPEventType == "retransmit" {
			m.TCPRetransmitTot++
			m.windowMu.Lock()
			m.retransmitWindow = append(m.retransmitWindow, timestampedCount{time.Now(), 1})
			m.windowMu.Unlock()
		}
		if evt.RTTUS > 0 {
			m.TCPRTTLastUS = evt.RTTUS
		}
	case EventSyscall:
		m.SyscallEventCount++
	case EventCapability:
		m.CapEventCount++
	case EventFile:
		m.FileEventCount++
	}
}

// computeRetransmitRate calculates retransmits/s over the last 60-second window.
func (m *NodeMetrics) computeRetransmitRate() float64 {
	m.windowMu.Lock()
	defer m.windowMu.Unlock()

	cutoff := time.Now().Add(-60 * time.Second)
	var count uint64
	var newWindow []timestampedCount
	for _, tc := range m.retransmitWindow {
		if tc.ts.After(cutoff) {
			count += tc.count
			newWindow = append(newWindow, tc)
		}
	}
	m.retransmitWindow = newWindow
	return float64(count) / 60.0
}

// Snapshot returns a map representation for JSON / Kafka publishing.
func (m *NodeMetrics) Snapshot() map[string]interface{} {
	m.mu.RLock()
	defer m.mu.RUnlock()

	return map[string]interface{}{
		"node":                   m.NodeName,
		"sched_latency_p99_ns":   m.SchedLatencyP99NS,
		"sched_event_count":      m.SchedEventCount,
		"oom_kill_count":         m.OOMKillCount,
		"tcp_retransmit_rate":    m.computeRetransmitRate(),
		"tcp_retransmit_total":   m.TCPRetransmitTot,
		"tcp_rtt_last_us":        m.TCPRTTLastUS,
		"syscall_event_count":    m.SyscallEventCount,
		"cap_event_count":        m.CapEventCount,
		"file_event_count":       m.FileEventCount,
		"critical_count":         m.CriticalCount,
		"warning_count":          m.WarningCount,
		"info_count":             m.InfoCount,
		"timestamp":              time.Now().UTC().Format(time.RFC3339),
	}
}

// ─── MetricAggregator ─────────────────────────────────────────────────────────

// MetricAggregator collects events and maintains per-node metric state.
type MetricAggregator struct {
	mu      sync.RWMutex
	nodes   map[string]*NodeMetrics
	nodeName string
}

// NewMetricAggregator creates a MetricAggregator for a single node host.
func NewMetricAggregator(nodeName string) *MetricAggregator {
	ma := &MetricAggregator{
		nodes:    make(map[string]*NodeMetrics),
		nodeName: nodeName,
	}
	// Pre-create entry for the local node
	ma.nodes[nodeName] = &NodeMetrics{NodeName: nodeName}
	return ma
}

// Record updates metrics from an incoming normalised event.
func (ma *MetricAggregator) Record(evt *NormalisedEvent) {
	ma.mu.Lock()
	nm, ok := ma.nodes[evt.NodeName]
	if !ok {
		nm = &NodeMetrics{NodeName: evt.NodeName}
		ma.nodes[evt.NodeName] = nm
	}
	ma.mu.Unlock()

	nm.record(evt)

	// Update Prometheus counters
	dstPort := fmt.Sprintf("%d", evt.DstPort)
	node := evt.NodeName
	sev := string(evt.Severity)

	eventsProcessedTotal.WithLabelValues(node, string(evt.Type), sev).Inc()

	switch evt.Type {
	case EventSched:
		schedLatencyNS.WithLabelValues(node, fmt.Sprintf("%d", evt.CPU)).
			Observe(float64(evt.LatencyNS))
	case EventOOM:
		cgrp := evt.CgroupName
		if cgrp == "" {
			cgrp = "unknown"
		}
		oomKillsTotal.WithLabelValues(node, cgrp).Inc()
	case EventTCP:
		if evt.TCPEventType == "retransmit" {
			tcpRetransmitsTotal.WithLabelValues(node, dstPort).Inc()
		}
		if evt.RTTUS > 0 {
			tcpRTTMicros.WithLabelValues(node, dstPort).Observe(float64(evt.RTTUS))
		}
	case EventSyscall:
		syscallEventsTotal.WithLabelValues(node, evt.SyscallName, sev).Inc()
	case EventFile:
		fileEventsTotal.WithLabelValues(node, sev).Inc()
	case EventCapability:
		capEventsTotal.WithLabelValues(node, evt.CapName, sev).Inc()
	}
}

// RecordDrop increments the ring-buffer-dropped counter for a probe.
func (ma *MetricAggregator) RecordDrop(probe string) {
	ringbufDropped.WithLabelValues(ma.nodeName, probe).Inc()
}

// RecordKafkaPublish records a Kafka publish attempt.
func (ma *MetricAggregator) RecordKafkaPublish(topic, status string) {
	kafkaPublishTotal.WithLabelValues(ma.nodeName, topic, status).Inc()
}

// TopologyPayload returns a JSON-serialisable topology update for Kafka.
func (ma *MetricAggregator) TopologyPayload() ([]byte, error) {
	ma.mu.RLock()
	defer ma.mu.RUnlock()

	snapshots := make([]map[string]interface{}, 0, len(ma.nodes))
	for _, nm := range ma.nodes {
		snapshots = append(snapshots, nm.Snapshot())
	}

	payload := map[string]interface{}{
		"type":      "topology_update",
		"source":    "layer1_nervous",
		"nodes":     snapshots,
		"timestamp": time.Now().UTC().Format(time.RFC3339),
	}
	return json.Marshal(payload)
}

// StartPeriodicPublisher starts a background goroutine that publishes topology
// updates to Kafka every 'interval'. It stops when 'stop' is closed.
func (ma *MetricAggregator) StartPeriodicPublisher(
	kp KafkaPublisher,
	topic string,
	interval time.Duration,
	stop <-chan struct{},
) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				payload, err := ma.TopologyPayload()
				if err != nil {
					log.Printf("topology payload marshal error: %v", err)
					continue
				}
				if err := kp.Publish(topic, ma.nodeName, payload); err != nil {
					log.Printf("topology publish error: %v", err)
					ma.RecordKafkaPublish(topic, "error")
				} else {
					ma.RecordKafkaPublish(topic, "ok")
				}
			case <-stop:
				return
			}
		}
	}()
}
