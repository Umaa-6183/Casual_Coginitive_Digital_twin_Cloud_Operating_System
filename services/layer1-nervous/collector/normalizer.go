// CCDT Layer-1 Nervous System — Event Normalizer
//
// Converts raw C structs read from eBPF ring buffers into a unified
// NormalisedEvent schema that the rest of the platform consumes.
// All downstream services (GNN, Guardian, API Gateway) only see NormalisedEvent.

package main

import (
	"encoding/binary"
	"fmt"
	"net"
	"strings"
	"time"

	"github.com/google/uuid"
)

// ─── Raw C struct mirrors ─────────────────────────────────────────────────────
// Field layout must match the corresponding .bpf.c event structs exactly.
// All multi-byte fields are little-endian on x86.

// SchedEvent mirrors struct sched_event in scheduler.bpf.c
type SchedEvent struct {
	LatencyNS   uint64
	WakeupTS    uint64
	PID         uint32
	TGID        uint32
	CPU         uint32
	Prio        int32
	Comm        [16]byte
	Pad         [4]byte
}

// OOMEvent mirrors struct oom_event in oom.bpf.c
type OOMEvent struct {
	TimestampNS   uint64
	RSSBytes      uint64
	MemLimitBytes uint64
	OOMScoreAdj   uint64
	VictimPID     uint32
	VictimTGID    uint32
	KillerPID     uint32
	Order         uint32
	VictimComm    [16]byte
	KillerComm    [16]byte
	CgroupName    [128]byte
}

// TCPEvent mirrors struct tcp_event in tcp.bpf.c
type TCPEvent struct {
	TimestampNS    uint64
	RTTUS          uint64
	Saddr          uint32
	Daddr          uint32
	PID            uint32
	RetransmitsTot uint32
	Sport          uint16
	Dport          uint16
	Family         uint16
	EventType      uint8
	NewState       uint8
	Comm           [16]byte
}

// TCP event type constants (must match tcp.bpf.c)
const (
	TCPEvtRetransmit uint8 = 1
	TCPEvtRTTSample  uint8 = 2
	TCPEvtConnState  uint8 = 3
)

// SyscallEvent mirrors struct syscall_event in syscall.bpf.c
type SyscallEvent struct {
	TimestampNS uint64
	UIDGid      uint64
	PID         uint32
	TGID        uint32
	PPID        uint32
	Comm        [16]byte
	ParentComm  [16]byte
	SyscallType uint8
	Severity    uint8
	Pad         [2]byte
	Path        [128]byte
	Arg0        int64
}

// Syscall type constants (must match syscall.bpf.c)
const (
	SCExecve    uint8 = 1
	SCSetuid    uint8 = 2
	SCPtrace    uint8 = 3
	SCMount     uint8 = 4
	SCPivotRoot uint8 = 5
	SCUnshare   uint8 = 6
)

// FileEvent mirrors struct file_event in file_access.bpf.c
type FileEvent struct {
	TimestampNS uint64
	UIDGid      uint64
	PID         uint32
	TGID        uint32
	PPID        uint32
	Inode       uint32
	Comm        [16]byte
	ParentComm  [16]byte
	Severity    uint8
	Flags       uint8
	Pad         [2]byte
	Filename    [256]byte
	FullPath    [256]byte
}

// CapEvent mirrors struct cap_event in capability.bpf.c
type CapEvent struct {
	TimestampNS uint64
	UIDGid      uint64
	PID         uint32
	TGID        uint32
	PPID        uint32
	Cap         uint32
	Audit       int32
	Severity    uint8
	Comm        [16]byte
	ParentComm  [16]byte
	Pad         [3]byte
	CapName     [20]byte
}

// ─── Unified output type ───────────────────────────────────────────────────────

// EventType identifies the probe that produced the event.
type EventType string

const (
	EventSched      EventType = "sched"
	EventOOM        EventType = "oom"
	EventTCP        EventType = "tcp"
	EventSyscall    EventType = "syscall"
	EventFile       EventType = "file"
	EventCapability EventType = "capability"
)

// SeverityLabel is a human-readable severity string.
type SeverityLabel string

const (
	SevInfo     SeverityLabel = "info"
	SevWarning  SeverityLabel = "warning"
	SevCritical SeverityLabel = "critical"
)

// NormalisedEvent is the canonical event schema consumed by all downstream services.
type NormalisedEvent struct {
	// Identification
	ID        string    `json:"id"`         // UUID v4
	Type      EventType `json:"type"`       // probe type
	Timestamp time.Time `json:"timestamp"`  // UTC wall-clock time
	NodeName  string    `json:"node"`       // Kubernetes node hostname (injected by collector)

	// Severity & classification
	Severity  SeverityLabel `json:"severity"`
	Detail    string        `json:"detail"`   // human-readable one-liner

	// Process context
	PID        uint32 `json:"pid"`
	TGID       uint32 `json:"tgid"`
	PPID       uint32 `json:"ppid,omitempty"`
	Comm       string `json:"comm"`
	ParentComm string `json:"parent_comm,omitempty"`
	UID        uint32 `json:"uid,omitempty"`
	GID        uint32 `json:"gid,omitempty"`

	// Type-specific fields (only the relevant ones are populated)
	LatencyNS      uint64 `json:"latency_ns,omitempty"`
	CPU            uint32 `json:"cpu,omitempty"`
	RSSB           uint64 `json:"rss_bytes,omitempty"`
	MemLimitB      uint64 `json:"mem_limit_bytes,omitempty"`
	CgroupName     string `json:"cgroup_name,omitempty"`
	SrcIP          string `json:"src_ip,omitempty"`
	DstIP          string `json:"dst_ip,omitempty"`
	SrcPort        uint16 `json:"src_port,omitempty"`
	DstPort        uint16 `json:"dst_port,omitempty"`
	RTTUS          uint64 `json:"rtt_us,omitempty"`
	RetransmitsTotal uint32 `json:"retransmits_total,omitempty"`
	TCPEventType   string `json:"tcp_event_type,omitempty"`
	SyscallName    string `json:"syscall_name,omitempty"`
	Path           string `json:"path,omitempty"`
	CapName        string `json:"cap_name,omitempty"`
	Cap            uint32 `json:"cap,omitempty"`

	// Raw bytes for debugging (omitted in production by default)
	// RawHex string `json:"raw_hex,omitempty"`
}

// ─── Normalizer ───────────────────────────────────────────────────────────────

// Normalizer converts raw eBPF byte slices into NormalisedEvent values.
type Normalizer struct {
	nodeName string
}

// NewNormalizer creates a Normalizer that stamps every event with the given node name.
func NewNormalizer(nodeName string) *Normalizer {
	return &Normalizer{nodeName: nodeName}
}

// NormaliseSched converts a raw SchedEvent byte slice to a NormalisedEvent.
func (n *Normalizer) NormaliseSched(raw []byte) (*NormalisedEvent, error) {
	if len(raw) < int(binary.Size(SchedEvent{})) {
		return nil, fmt.Errorf("sched event too short: %d bytes", len(raw))
	}
	var e SchedEvent
	if err := binary.Read(strings.NewReader(string(raw)), binary.LittleEndian, &e); err != nil {
		return nil, fmt.Errorf("decode sched event: %w", err)
	}

	latencyMS := float64(e.LatencyNS) / 1e6
	sev := SevInfo
	if latencyMS > 100 {
		sev = SevCritical
	} else if latencyMS > 10 {
		sev = SevWarning
	}

	return &NormalisedEvent{
		ID:        uuid.New().String(),
		Type:      EventSched,
		Timestamp: time.Now().UTC(),
		NodeName:  n.nodeName,
		Severity:  sev,
		Detail:    fmt.Sprintf("sched_latency p99=%.1fms cpu=%d comm=%s pid=%d", latencyMS, e.CPU, cstring(e.Comm[:]), e.PID),
		PID:       e.PID,
		TGID:      e.TGID,
		Comm:      cstring(e.Comm[:]),
		CPU:       e.CPU,
		LatencyNS: e.LatencyNS,
	}, nil
}

// NormaliseOOM converts a raw OOMEvent byte slice to a NormalisedEvent.
func (n *Normalizer) NormaliseOOM(raw []byte) (*NormalisedEvent, error) {
	if len(raw) < int(binary.Size(OOMEvent{})) {
		return nil, fmt.Errorf("oom event too short: %d bytes", len(raw))
	}
	var e OOMEvent
	if err := binary.Read(strings.NewReader(string(raw)), binary.LittleEndian, &e); err != nil {
		return nil, fmt.Errorf("decode oom event: %w", err)
	}

	rssGB := float64(e.RSSBytes) / (1024 * 1024 * 1024)
	limitGB := float64(e.MemLimitBytes) / (1024 * 1024 * 1024)
	cgrp := cstring(e.CgroupName[:])

	detail := fmt.Sprintf("OOM kill victim=%s pid=%d rss=%.1fGB", cstring(e.VictimComm[:]), e.VictimPID, rssGB)
	if limitGB > 0 {
		detail += fmt.Sprintf(" limit=%.1fGB", limitGB)
	}
	if cgrp != "" {
		detail += fmt.Sprintf(" cgroup=%s", cgrp)
	}

	return &NormalisedEvent{
		ID:         uuid.New().String(),
		Type:       EventOOM,
		Timestamp:  time.Now().UTC(),
		NodeName:   n.nodeName,
		Severity:   SevCritical,
		Detail:     detail,
		PID:        e.VictimPID,
		TGID:       e.VictimTGID,
		Comm:       cstring(e.VictimComm[:]),
		RSSB:       e.RSSBytes,
		MemLimitB:  e.MemLimitBytes,
		CgroupName: cgrp,
	}, nil
}

// NormaliseTCP converts a raw TCPEvent byte slice to a NormalisedEvent.
func (n *Normalizer) NormaliseTCP(raw []byte) (*NormalisedEvent, error) {
	if len(raw) < int(binary.Size(TCPEvent{})) {
		return nil, fmt.Errorf("tcp event too short: %d bytes", len(raw))
	}
	var e TCPEvent
	if err := binary.Read(strings.NewReader(string(raw)), binary.LittleEndian, &e); err != nil {
		return nil, fmt.Errorf("decode tcp event: %w", err)
	}

	srcIP := uint32ToIP(e.Saddr)
	dstIP := uint32ToIP(e.Daddr)

	var evtTypeName, detail string
	var sev SeverityLabel

	switch e.EventType {
	case TCPEvtRetransmit:
		evtTypeName = "retransmit"
		sev = SevWarning
		if e.RetransmitsTot > 50 {
			sev = SevCritical
		}
		detail = fmt.Sprintf("tcp_retransmit %s:%d→%s:%d total=%d comm=%s",
			srcIP, e.Sport, dstIP, e.Dport, e.RetransmitsTot, cstring(e.Comm[:]))
	case TCPEvtRTTSample:
		evtTypeName = "rtt_sample"
		sev = SevWarning
		if e.RTTUS > 200000 { // > 200ms
			sev = SevCritical
		}
		detail = fmt.Sprintf("tcp_rtt_high %s:%d→%s:%d rtt=%.1fms",
			srcIP, e.Sport, dstIP, e.Dport, float64(e.RTTUS)/1000.0)
	case TCPEvtConnState:
		evtTypeName = "conn_state"
		sev = SevInfo
		detail = fmt.Sprintf("tcp_state %s:%d→%s:%d state=%d",
			srcIP, e.Sport, dstIP, e.Dport, e.NewState)
	default:
		evtTypeName = "unknown"
		sev = SevInfo
		detail = fmt.Sprintf("tcp_event type=%d", e.EventType)
	}

	return &NormalisedEvent{
		ID:               uuid.New().String(),
		Type:             EventTCP,
		Timestamp:        time.Now().UTC(),
		NodeName:         n.nodeName,
		Severity:         sev,
		Detail:           detail,
		PID:              e.PID,
		Comm:             cstring(e.Comm[:]),
		SrcIP:            srcIP,
		DstIP:            dstIP,
		SrcPort:          e.Sport,
		DstPort:          e.Dport,
		RTTUS:            e.RTTUS,
		RetransmitsTotal: e.RetransmitsTot,
		TCPEventType:     evtTypeName,
	}, nil
}

// NormaliseSyscall converts a raw SyscallEvent byte slice to a NormalisedEvent.
func (n *Normalizer) NormaliseSyscall(raw []byte) (*NormalisedEvent, error) {
	if len(raw) < int(binary.Size(SyscallEvent{})) {
		return nil, fmt.Errorf("syscall event too short: %d bytes", len(raw))
	}
	var e SyscallEvent
	if err := binary.Read(strings.NewReader(string(raw)), binary.LittleEndian, &e); err != nil {
		return nil, fmt.Errorf("decode syscall event: %w", err)
	}

	uid := uint32(e.UIDGid & 0xFFFFFFFF)
	gid := uint32(e.UIDGid >> 32)
	path := cstring(e.Path[:])

	scName := syscallName(e.SyscallType)
	sev := bpfSev(e.Severity)

	detail := fmt.Sprintf("%s comm=%s pid=%d uid=%d", scName, cstring(e.Comm[:]), e.PID, uid)
	if path != "" {
		detail += fmt.Sprintf(" path=%s", path)
	}

	return &NormalisedEvent{
		ID:          uuid.New().String(),
		Type:        EventSyscall,
		Timestamp:   time.Now().UTC(),
		NodeName:    n.nodeName,
		Severity:    sev,
		Detail:      detail,
		PID:         e.PID,
		TGID:        e.TGID,
		PPID:        e.PPID,
		Comm:        cstring(e.Comm[:]),
		ParentComm:  cstring(e.ParentComm[:]),
		UID:         uid,
		GID:         gid,
		SyscallName: scName,
		Path:        path,
	}, nil
}

// NormaliseFile converts a raw FileEvent byte slice to a NormalisedEvent.
func (n *Normalizer) NormaliseFile(raw []byte) (*NormalisedEvent, error) {
	if len(raw) < int(binary.Size(FileEvent{})) {
		return nil, fmt.Errorf("file event too short: %d bytes", len(raw))
	}
	var e FileEvent
	if err := binary.Read(strings.NewReader(string(raw)), binary.LittleEndian, &e); err != nil {
		return nil, fmt.Errorf("decode file event: %w", err)
	}

	uid := uint32(e.UIDGid & 0xFFFFFFFF)
	fname := cstring(e.Filename[:])
	fpath := cstring(e.FullPath[:])
	sev := bpfSev(e.Severity)

	fullFile := fname
	if fpath != "" && fpath != "/" {
		fullFile = fpath + "/" + fname
	}

	detail := fmt.Sprintf("file_open %s comm=%s pid=%d uid=%d", fullFile, cstring(e.Comm[:]), e.PID, uid)

	return &NormalisedEvent{
		ID:         uuid.New().String(),
		Type:       EventFile,
		Timestamp:  time.Now().UTC(),
		NodeName:   n.nodeName,
		Severity:   sev,
		Detail:     detail,
		PID:        e.PID,
		TGID:       e.TGID,
		PPID:       e.PPID,
		Comm:       cstring(e.Comm[:]),
		ParentComm: cstring(e.ParentComm[:]),
		UID:        uid,
		Path:       fullFile,
	}, nil
}

// NormaliseCapability converts a raw CapEvent byte slice to a NormalisedEvent.
func (n *Normalizer) NormaliseCapability(raw []byte) (*NormalisedEvent, error) {
	if len(raw) < int(binary.Size(CapEvent{})) {
		return nil, fmt.Errorf("cap event too short: %d bytes", len(raw))
	}
	var e CapEvent
	if err := binary.Read(strings.NewReader(string(raw)), binary.LittleEndian, &e); err != nil {
		return nil, fmt.Errorf("decode cap event: %w", err)
	}

	uid := uint32(e.UIDGid & 0xFFFFFFFF)
	capName := cstring(e.CapName[:])
	sev := bpfSev(e.Severity)

	detail := fmt.Sprintf("%s granted pid=%d uid=%d comm=%s parent=%s",
		capName, e.PID, uid, cstring(e.Comm[:]), cstring(e.ParentComm[:]))

	return &NormalisedEvent{
		ID:         uuid.New().String(),
		Type:       EventCapability,
		Timestamp:  time.Now().UTC(),
		NodeName:   n.nodeName,
		Severity:   sev,
		Detail:     detail,
		PID:        e.PID,
		TGID:       e.TGID,
		PPID:       e.PPID,
		Comm:       cstring(e.Comm[:]),
		ParentComm: cstring(e.ParentComm[:]),
		UID:        uid,
		Cap:        e.Cap,
		CapName:    capName,
	}, nil
}

// ─── Private helpers ──────────────────────────────────────────────────────────

// cstring converts a null-terminated C byte array to a Go string.
func cstring(b []byte) string {
	for i, v := range b {
		if v == 0 {
			return string(b[:i])
		}
	}
	return string(b)
}

// uint32ToIP converts a little-endian packed IPv4 address to dotted notation.
func uint32ToIP(addr uint32) string {
	b := make([]byte, 4)
	binary.LittleEndian.PutUint32(b, addr)
	return net.IP(b).String()
}

// bpfSev maps a BPF severity byte (0=info, 1=warning, 2=critical) to SeverityLabel.
func bpfSev(s uint8) SeverityLabel {
	switch s {
	case 2:
		return SevCritical
	case 1:
		return SevWarning
	default:
		return SevInfo
	}
}

// syscallName maps SC_* constants to human-readable names.
func syscallName(t uint8) string {
	switch t {
	case SCExecve:
		return "execve"
	case SCSetuid:
		return "setuid"
	case SCPtrace:
		return "ptrace"
	case SCMount:
		return "mount"
	case SCPivotRoot:
		return "pivot_root"
	case SCUnshare:
		return "unshare"
	default:
		return fmt.Sprintf("syscall_%d", t)
	}
}
