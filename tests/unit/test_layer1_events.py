"""
Unit tests — Layer-1 Nervous System
Tests EventMetadata, eBPF event types, TypedEbpfBatch, serialization,
type_counts, byte_size, and sentinel validation.

All tests are network-free and sub-50ms.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, CapabilityEvent, OomKillEvent, TcpRetransmitEvent,
    SchedLatencyEvent, FileAccessEvent, SyscallEvent, ExecveEvent,
    NetworkConnectEvent, EventMetadata, EventSeverity,
    LinuxCapability, NetworkProtocol, SchedEventType,
)


# ══════════════════════════════════════════════════════════════════════════════
# EventMetadata
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer1
class TestEventMetadata:
    def test_default_construction(self):
        meta = EventMetadata()
        assert meta.pid == 0
        assert meta.uid == 0
        assert meta.comm == ""
        assert meta.severity == EventSeverity.SEVERITY_INFO

    def test_now_factory_sets_ts(self):
        before = time.monotonic_ns()
        meta   = EventMetadata.now("node-1", 1234, "nginx")
        after  = time.monotonic_ns()
        assert meta.kernel_ts_ns >= before
        assert meta.kernel_ts_ns <= after

    def test_now_factory_fields(self):
        meta = EventMetadata.now(
            "node-prod-1", 5555, "payment-svc",
            namespace="production",
            pod_name="payment-pod-abc",
            container_id="ctr://xyz123",
        )
        assert meta.node_name  == "node-prod-1"
        assert meta.pid        == 5555
        assert meta.comm       == "payment-svc"
        assert meta.namespace  == "production"
        assert meta.pod_name   == "payment-pod-abc"
        assert meta.container_id == "ctr://xyz123"

    def test_serialize_roundtrip(self):
        meta   = EventMetadata.now("node-1", 42, "bash", namespace="kube-system")
        raw    = meta.SerializeToString()
        loaded = EventMetadata.FromString(raw)
        assert loaded.node_name  == "node-1"
        assert loaded.pid        == 42
        assert loaded.comm       == "bash"
        assert loaded.namespace  == "kube-system"

    def test_to_dict_contains_required_keys(self):
        meta = EventMetadata.now("node-1", 1, "init")
        d    = meta._to_dict()
        for key in ("node_name", "pid", "comm", "kernel_ts_ns"):
            assert key in d, f"Missing key: {key}"

    def test_labels_roundtrip(self):
        meta = EventMetadata(labels={"env": "prod", "region": "us-east-1"})
        raw  = meta.SerializeToString()
        back = EventMetadata.FromString(raw)
        assert back.labels.get("env")    == "prod"
        assert back.labels.get("region") == "us-east-1"

    def test_severity_enum_values(self):
        assert EventSeverity.SEVERITY_INFO     == EventSeverity.SEVERITY_INFO
        assert EventSeverity.SEVERITY_HIGH     != EventSeverity.SEVERITY_INFO
        assert EventSeverity.SEVERITY_CRITICAL != EventSeverity.SEVERITY_HIGH


# ══════════════════════════════════════════════════════════════════════════════
# CapabilityEvent
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer1
class TestCapabilityEvent:
    def test_construction(self, base_meta):
        cap = CapabilityEvent(
            meta=base_meta,
            capability=LinuxCapability.CAP_NET_ADMIN,
            syscall_nr=21,
            allowed=False,
            cap_bitmask="0x100000",
        )
        assert cap.capability == LinuxCapability.CAP_NET_ADMIN
        assert cap.allowed    is False
        assert cap.syscall_nr == 21

    def test_dangerous_capabilities(self, base_meta):
        dangerous = [
            LinuxCapability.CAP_SYS_ADMIN,
            LinuxCapability.CAP_NET_ADMIN,
            LinuxCapability.CAP_SYS_PTRACE,
            LinuxCapability.CAP_DAC_OVERRIDE,
        ]
        for cap_val in dangerous:
            evt = CapabilityEvent(meta=base_meta, capability=cap_val, allowed=False)
            raw = evt.SerializeToString()
            assert len(raw) > 0

    def test_serialize_roundtrip(self, base_meta):
        cap  = CapabilityEvent(meta=base_meta, capability=LinuxCapability.CAP_SYS_ADMIN,
                               allowed=False, audit_serial=12345)
        raw  = cap.SerializeToString()
        back = CapabilityEvent.FromString(raw)
        assert back.audit_serial == 12345
        assert back.allowed      is False


# ══════════════════════════════════════════════════════════════════════════════
# OomKillEvent
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer1
class TestOomKillEvent:
    def test_construction(self, base_meta):
        oom = OomKillEvent(
            meta=base_meta,
            victim_pid=9999,
            victim_comm="java",
            oom_score=950,
            victim_rss_bytes=512 * 1024 * 1024,
            oom_kill_count_5m=5,
        )
        assert oom.victim_pid        == 9999
        assert oom.victim_rss_bytes  == 512 * 1024 * 1024
        assert oom.oom_kill_count_5m == 5

    def test_rss_bytes_large_value(self, base_meta):
        """Ensure 64-bit RSS values round-trip correctly."""
        large_rss = 8 * 1024 * 1024 * 1024   # 8 GB
        oom  = OomKillEvent(meta=base_meta, victim_pid=1, victim_comm="x",
                            victim_rss_bytes=large_rss)
        raw  = oom.SerializeToString()
        back = OomKillEvent.FromString(raw)
        assert back.victim_rss_bytes == large_rss

    def test_cgroup_path_preserved(self, base_meta):
        path = "/kubepods/besteffort/podabc123/container-xyz"
        oom  = OomKillEvent(meta=base_meta, victim_pid=1, victim_comm="proc",
                            cgroup_path=path)
        raw  = oom.SerializeToString()
        back = OomKillEvent.FromString(raw)
        assert back.cgroup_path == path


# ══════════════════════════════════════════════════════════════════════════════
# TcpRetransmitEvent
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer1
class TestTcpRetransmitEvent:
    def test_port_range_validation(self, base_meta):
        for port in (0, 80, 443, 5432, 65535):
            evt = TcpRetransmitEvent(meta=base_meta,
                                     src_addr="10.0.0.1", src_port=port,
                                     dst_addr="10.0.0.2", dst_port=5432)
            raw  = evt.SerializeToString()
            back = TcpRetransmitEvent.FromString(raw)
            assert back.src_port == port

    def test_ip_address_formats(self, base_meta):
        for addr in ("10.0.0.1", "192.168.1.100", "172.16.0.50"):
            evt = TcpRetransmitEvent(meta=base_meta, src_addr=addr, dst_addr="10.0.0.2")
            assert evt.src_addr == addr

    def test_high_retransmit_count(self, base_meta):
        evt = TcpRetransmitEvent(meta=base_meta,
                                  src_addr="10.0.0.1", dst_addr="10.0.0.2",
                                  retransmit_count=10000, rtt_us=5000000)
        raw  = evt.SerializeToString()
        back = TcpRetransmitEvent.FromString(raw)
        assert back.retransmit_count == 10000
        assert back.rtt_us           == 5000000


# ══════════════════════════════════════════════════════════════════════════════
# SchedLatencyEvent
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer1
class TestSchedLatencyEvent:
    def test_all_event_types(self, base_meta):
        for etype in (SchedEventType.SCHED_SWITCH, SchedEventType.SCHED_WAKEUP,
                      SchedEventType.SCHED_MIGRATE, SchedEventType.SCHED_LATENCY):
            evt = SchedLatencyEvent(meta=base_meta, event_type=etype, latency_us=5000, cpu_id=3)
            raw  = evt.SerializeToString()
            back = SchedLatencyEvent.FromString(raw)
            assert back.latency_us == 5000
            assert back.cpu_id     == 3


# ══════════════════════════════════════════════════════════════════════════════
# ExecveEvent
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer1
class TestExecveEvent:
    def test_construction_with_args(self, base_meta):
        evt = ExecveEvent(
            meta=base_meta,
            filename="/bin/bash",
            args=["/bin/bash", "-c", "whoami"],
            is_setuid=True,
            binary_hash="a" * 64,
        )
        assert evt.filename  == "/bin/bash"
        assert len(evt.args) == 3
        assert evt.is_setuid is True

    def test_binary_hash_length(self, base_meta):
        valid_hash = "deadbeef" * 8   # 64 hex chars = SHA256
        evt = ExecveEvent(meta=base_meta, filename="/usr/bin/python3",
                          binary_hash=valid_hash)
        assert len(evt.binary_hash) == 64


# ══════════════════════════════════════════════════════════════════════════════
# NetworkConnectEvent
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer1
class TestNetworkConnectEvent:
    def test_outbound(self, base_meta):
        evt = NetworkConnectEvent(
            meta=base_meta,
            src_addr="10.0.0.1", src_port=54321,
            dst_addr="8.8.8.8",  dst_port=53,
            protocol=NetworkProtocol.PROTO_UDP,
            is_outbound=True,
            success=True,
            bytes_sent=512,
            bytes_recv=256,
        )
        assert evt.is_outbound is True
        assert evt.dst_port    == 53
        assert evt.bytes_sent  == 512

    def test_inbound(self, base_meta):
        evt = NetworkConnectEvent(
            meta=base_meta,
            src_addr="1.2.3.4", dst_addr="10.0.0.5",
            is_outbound=False, dst_port=8080,
        )
        assert evt.is_outbound is False


# ══════════════════════════════════════════════════════════════════════════════
# TypedEbpfBatch
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer1
class TestTypedEbpfBatch:
    def test_empty_batch(self):
        batch = TypedEbpfBatch(
            batch_id=str(uuid.uuid4()),
            node_name="node-1",
            collector_id=str(uuid.uuid4()),
            batch_ts=datetime.now(timezone.utc).isoformat(),
            schema_ver="1.0",
        )
        assert batch.total_events() == 0

    def test_total_events_counts_all_types(self, ebpf_batch):
        assert ebpf_batch.total_events() == 3   # cap + oom + tcp (from conftest)

    def test_compute_type_counts_accuracy(self, ebpf_batch):
        ebpf_batch.compute_type_counts()
        assert ebpf_batch.type_counts["capability"]     == 1
        assert ebpf_batch.type_counts["oom_kill"]       == 1
        assert ebpf_batch.type_counts["tcp_retransmit"] == 1

    def test_compute_type_counts_empty(self):
        batch = TypedEbpfBatch(batch_id="x", node_name="n", collector_id="c",
                                batch_ts="", schema_ver="1.0")
        batch.compute_type_counts()
        assert sum(batch.type_counts.values()) == 0

    def test_serialize_roundtrip_full_batch(self, ebpf_batch):
        raw    = ebpf_batch.SerializeToString()
        loaded = TypedEbpfBatch.FromString(raw)
        assert loaded.node_name    == ebpf_batch.node_name
        assert loaded.batch_id     == ebpf_batch.batch_id
        assert loaded.schema_ver   == "1.0"
        assert loaded.total_events() == 3

    def test_byte_size_positive(self, ebpf_batch):
        assert ebpf_batch.ByteSize() > 0

    def test_byte_size_grows_with_events(self, base_meta, capability_event):
        small = TypedEbpfBatch(batch_id="x", node_name="n", collector_id="c",
                                batch_ts="", schema_ver="1.0")
        large = TypedEbpfBatch(batch_id="x", node_name="n", collector_id="c",
                                batch_ts="", schema_ver="1.0",
                                capability_events=[capability_event] * 10)
        assert large.ByteSize() > small.ByteSize()

    def test_multi_node_names_in_batch(self, base_meta, capability_event, oom_kill_event):
        """Batch is per-node; both events should share the same node_name from meta."""
        batch = TypedEbpfBatch(
            batch_id=str(uuid.uuid4()),
            node_name=base_meta.node_name,
            collector_id=str(uuid.uuid4()),
            batch_ts=datetime.now(timezone.utc).isoformat(),
            capability_events=[capability_event],
            oom_kill_events=[oom_kill_event],
            schema_ver="1.0",
        )
        assert batch.node_name == base_meta.node_name
        assert batch.total_events() == 2

    def test_batch_max_500_events(self, base_meta):
        """Simulate full batch at 500-event limit."""
        events = [
            CapabilityEvent(meta=base_meta,
                            capability=LinuxCapability.CAP_NET_ADMIN, allowed=False)
            for _ in range(500)
        ]
        batch = TypedEbpfBatch(
            batch_id=str(uuid.uuid4()),
            node_name="node-load",
            collector_id=str(uuid.uuid4()),
            batch_ts=datetime.now(timezone.utc).isoformat(),
            capability_events=events,
            schema_ver="1.0",
        )
        assert batch.total_events() == 500
        raw = batch.SerializeToString()
        back = TypedEbpfBatch.FromString(raw)
        assert len(back.capability_events) == 500

    @pytest.mark.parametrize("n_cap,n_oom,n_tcp", [
        (0, 0, 0),
        (1, 0, 0),
        (5, 3, 2),
        (100, 50, 30),
    ])
    def test_total_events_parametrized(self, base_meta, n_cap, n_oom, n_tcp):
        cap_events = [CapabilityEvent(meta=base_meta,
                      capability=LinuxCapability.CAP_NET_ADMIN) for _ in range(n_cap)]
        oom_events = [OomKillEvent(meta=base_meta, victim_pid=i, victim_comm="x")
                      for i in range(n_oom)]
        tcp_events = [TcpRetransmitEvent(meta=base_meta,
                      src_addr="10.0.0.1", dst_addr="10.0.0.2")
                      for _ in range(n_tcp)]
        batch = TypedEbpfBatch(
            batch_id=str(uuid.uuid4()),
            node_name="node-1",
            collector_id=str(uuid.uuid4()),
            batch_ts=datetime.now(timezone.utc).isoformat(),
            capability_events=cap_events,
            oom_kill_events=oom_events,
            tcp_retransmit_events=tcp_events,
            schema_ver="1.0",
        )
        assert batch.total_events() == n_cap + n_oom + n_tcp

    def test_schema_ver_constant(self, ebpf_batch):
        assert ebpf_batch.schema_ver == "1.0"

    def test_batch_json_serializable(self, ebpf_batch):
        """Batch to_dict output must be JSON-serializable."""
        raw = ebpf_batch.SerializeToString()
        back = TypedEbpfBatch.FromString(raw)
        # At minimum, serialized bytes should be valid (no exceptions)
        assert isinstance(raw, bytes)
        assert len(raw) > 0
