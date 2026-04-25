# ─────────────────────────────────────────────────────────────────────────────
# CCDT events_pb2.py — Pure-Python message shims
# ─────────────────────────────────────────────────────────────────────────────
# This file provides dataclass-based shims that mirror the protobuf message API
# so that application code works without running protoc.
#
# For production deployments run:
#   make proto
# to generate the real protoc-compiled stubs which are ~10× faster.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Dict, List, Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class LinuxCapability(IntEnum):
    CAP_UNKNOWN          = 0
    CAP_CHOWN            = 1
    CAP_DAC_OVERRIDE     = 2
    CAP_DAC_READ_SEARCH  = 3
    CAP_FOWNER           = 4
    CAP_KILL             = 6
    CAP_SETGID           = 7
    CAP_SETUID           = 8
    CAP_NET_ADMIN        = 12
    CAP_NET_RAW          = 13
    CAP_SYS_PTRACE       = 19
    CAP_SYS_ADMIN        = 21
    CAP_SYS_BOOT         = 22
    CAP_BPF              = 39

class EventSeverity(IntEnum):
    SEVERITY_UNKNOWN  = 0
    SEVERITY_INFO     = 1
    SEVERITY_LOW      = 2
    SEVERITY_MEDIUM   = 3
    SEVERITY_HIGH     = 4
    SEVERITY_CRITICAL = 5

class NetworkProtocol(IntEnum):
    PROTO_UNKNOWN = 0
    PROTO_TCP     = 1
    PROTO_UDP     = 2
    PROTO_ICMP    = 3

class SchedEventType(IntEnum):
    SCHED_UNKNOWN = 0
    SCHED_SWITCH  = 1
    SCHED_WAKEUP  = 2
    SCHED_MIGRATE = 3
    SCHED_LATENCY = 4


# ── Base message mixin ────────────────────────────────────────────────────────

class _ProtoMessage:
    """Minimal protobuf-compatible message base."""

    def SerializeToString(self) -> bytes:
        return json.dumps(self._to_dict(), default=str).encode()

    @classmethod
    def FromString(cls, data: bytes):
        d = json.loads(data.decode())
        return cls._from_dict(d)

    def _to_dict(self) -> dict:
        result = {}
        for k, v in self.__dict__.items():
            if v is None or v == 0 or v == "" or v == [] or v == {}:
                continue
            if isinstance(v, _ProtoMessage):
                result[k] = v._to_dict()
            elif isinstance(v, list):
                result[k] = [i._to_dict() if isinstance(i, _ProtoMessage) else i for i in v]
            elif isinstance(v, IntEnum):
                result[k] = int(v)
            else:
                result[k] = v
        return result

    @classmethod
    def _from_dict(cls, d: dict):
        obj = cls.__new__(cls)
        for f in cls.__dataclass_fields__:
            setattr(obj, f, d.get(f, cls.__dataclass_fields__[f].default_factory()
                                   if hasattr(cls.__dataclass_fields__[f].default_factory, "__call__")
                                   else cls.__dataclass_fields__[f].default))
        return obj

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._to_dict()!r})"

    def ByteSize(self) -> int:
        return len(self.SerializeToString())

    def ListFields(self):
        return [(k, v) for k, v in self.__dict__.items() if v]


# ── Messages ──────────────────────────────────────────────────────────────────

@dataclass
class EventMetadata(_ProtoMessage):
    kernel_ts_ns:    int                    = 0
    wall_clock_ts:   str                    = ""  # ISO-8601
    node_name:       str                    = ""
    pod_name:        str                    = ""
    namespace:       str                    = ""
    container_id:    str                    = ""
    container_name:  str                    = ""
    pid:             int                    = 0
    tgid:            int                    = 0
    uid:             int                    = 0
    gid:             int                    = 0
    comm:            str                    = ""
    ppid:            int                    = 0
    collector_ver:   str                    = ""
    severity:        EventSeverity          = EventSeverity.SEVERITY_INFO
    labels:          Dict[str, str]         = field(default_factory=dict)

    @staticmethod
    def now(node: str, pid: int, comm: str, **kw) -> "EventMetadata":
        return EventMetadata(
            kernel_ts_ns=time.monotonic_ns(),
            wall_clock_ts=datetime.now(timezone.utc).isoformat(),
            node_name=node,
            pid=pid,
            comm=comm,
            **kw,
        )


@dataclass
class CapabilityEvent(_ProtoMessage):
    meta:          EventMetadata    = field(default_factory=EventMetadata)
    capability:    LinuxCapability  = LinuxCapability.CAP_UNKNOWN
    syscall_nr:    int              = 0
    allowed:       bool             = False
    cap_bitmask:   str              = ""
    audit_serial:  int              = 0


@dataclass
class OomKillEvent(_ProtoMessage):
    meta:              EventMetadata = field(default_factory=EventMetadata)
    victim_pid:        int           = 0
    victim_comm:       str           = ""
    oom_score:         int           = 0
    victim_rss_bytes:  int           = 0
    total_vm_bytes:    int           = 0
    total_rss_bytes:   int           = 0
    cgroup_path:       str           = ""
    oom_kill_count_5m: int           = 0
    oom_flags:         int           = 0


@dataclass
class TcpRetransmitEvent(_ProtoMessage):
    meta:             EventMetadata   = field(default_factory=EventMetadata)
    src_addr:         str             = ""
    src_port:         int             = 0
    dst_addr:         str             = ""
    dst_port:         int             = 0
    protocol:         NetworkProtocol = NetworkProtocol.PROTO_TCP
    tcp_state:        int             = 0
    retransmit_count: int             = 0
    rtt_us:           int             = 0
    rto_us:           int             = 0
    snd_cwnd:         int             = 0
    sk_backlog:       int             = 0


@dataclass
class SchedLatencyEvent(_ProtoMessage):
    meta:       EventMetadata  = field(default_factory=EventMetadata)
    event_type: SchedEventType = SchedEventType.SCHED_UNKNOWN
    latency_us: int            = 0
    cpu_id:     int            = 0
    prev_comm:  str            = ""
    prev_pid:   int            = 0
    next_comm:  str            = ""
    next_pid:   int            = 0
    task_prio:  int            = 0
    nvcsw:      int            = 0
    nivcsw:     int            = 0


@dataclass
class FileAccessEvent(_ProtoMessage):
    meta:        EventMetadata = field(default_factory=EventMetadata)
    filepath:    str           = ""
    flags:       int           = 0
    mode:        int           = 0
    success:     bool          = False
    errno_val:   int           = 0
    inode:       int           = 0
    mount_point: str           = ""
    dev:         int           = 0
    access_type: str           = ""


@dataclass
class SyscallEvent(_ProtoMessage):
    meta:         EventMetadata = field(default_factory=EventMetadata)
    syscall_nr:   int           = 0
    syscall_name: str           = ""
    ret_val:      int           = 0
    args:         List[int]     = field(default_factory=list)
    is_flagged:   bool          = False
    flag_reason:  str           = ""


@dataclass
class ExecveEvent(_ProtoMessage):
    meta:         EventMetadata = field(default_factory=EventMetadata)
    filename:     str           = ""
    args:         List[str]     = field(default_factory=list)
    env_vars:     List[str]     = field(default_factory=list)
    is_setuid:    bool          = False
    is_setgid:    bool          = False
    return_code:  int           = 0
    binary_hash:  str           = ""
    interpreter:  str           = ""


@dataclass
class NetworkConnectEvent(_ProtoMessage):
    meta:         EventMetadata   = field(default_factory=EventMetadata)
    src_addr:     str             = ""
    src_port:     int             = 0
    dst_addr:     str             = ""
    dst_port:     int             = 0
    protocol:     NetworkProtocol = NetworkProtocol.PROTO_TCP
    is_outbound:  bool            = True
    success:      bool            = False
    bytes_sent:   int             = 0
    bytes_recv:   int             = 0
    duration_ms:  int             = 0
    dst_hostname: str             = ""


@dataclass
class TypedEbpfBatch(_ProtoMessage):
    """One Kafka message on ccdt.ebpf.events."""
    batch_id:              str                       = ""
    node_name:             str                       = ""
    collector_id:          str                       = ""
    batch_ts:              str                       = ""  # ISO-8601
    capability_events:     List[CapabilityEvent]     = field(default_factory=list)
    oom_kill_events:       List[OomKillEvent]        = field(default_factory=list)
    tcp_retransmit_events: List[TcpRetransmitEvent]  = field(default_factory=list)
    sched_latency_events:  List[SchedLatencyEvent]   = field(default_factory=list)
    file_access_events:    List[FileAccessEvent]     = field(default_factory=list)
    syscall_events:        List[SyscallEvent]        = field(default_factory=list)
    execve_events:         List[ExecveEvent]         = field(default_factory=list)
    network_events:        List[NetworkConnectEvent] = field(default_factory=list)
    type_counts:           Dict[str, int]            = field(default_factory=dict)
    schema_ver:            str                       = "1.0"

    def total_events(self) -> int:
        return sum([
            len(self.capability_events),
            len(self.oom_kill_events),
            len(self.tcp_retransmit_events),
            len(self.sched_latency_events),
            len(self.file_access_events),
            len(self.syscall_events),
            len(self.execve_events),
            len(self.network_events),
        ])

    def compute_type_counts(self) -> None:
        """Populate type_counts dict from event lists."""
        self.type_counts = {
            k: v for k, v in {
                "capability":     len(self.capability_events),
                "oom_kill":       len(self.oom_kill_events),
                "tcp_retransmit": len(self.tcp_retransmit_events),
                "sched_latency":  len(self.sched_latency_events),
                "file_access":    len(self.file_access_events),
                "syscall":        len(self.syscall_events),
                "execve":         len(self.execve_events),
                "network":        len(self.network_events),
            }.items() if v > 0
        }
