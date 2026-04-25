"""
CCDT Chaos Test Suite — Shared Fixtures
═══════════════════════════════════════════════════════════════════════════════
Fixtures that simulate infrastructure failures, network partitions, data
corruption, and service degradation without touching real infrastructure.

Chaos fixture categories
────────────────────────
  FaultInjector        Drop / delay / corrupt Kafka messages
  DegradedServices     HTTP mocks that timeout, 500, or return garbage
  NetworkPartition     Selective connectivity between services
  ClockManipulation    Advance time to trigger TTLs / expiries
  StateCorruptor       Malformed proto bytes, invalid JSON payloads

Design principles
─────────────────
  • All chaos is deterministic (seeded RNG where needed)
  • Chaos fixtures yield clean state after test teardown
  • No live Kafka / K8s / OPA needed
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, OomKillEvent, CapabilityEvent, TcpRetransmitEvent,
    EventMetadata, EventSeverity, LinuxCapability,
)
from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, NodeFeatures, TopologyNode,
    GnnInferenceResult,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    GhostSimulationResult, ActionRequest, ActionResult,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, SessionState, ChatMessage, SessionContext, TokenUsage,
)


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Fault injection primitives
# ══════════════════════════════════════════════════════════════════════════════

class FaultInjector:
    """
    Wraps any async callable and injects configurable faults:
      • random message drops
      • artificial latency
      • payload corruption
      • intermittent errors
    """

    def __init__(
        self,
        drop_rate: float = 0.0,
        latency_ms: float = 0.0,
        latency_jitter_ms: float = 0.0,
        error_rate: float = 0.0,
        corrupt_rate: float = 0.0,
        seed: int = 42,
    ):
        self.drop_rate          = drop_rate
        self.latency_ms         = latency_ms
        self.latency_jitter_ms  = latency_jitter_ms
        self.error_rate         = error_rate
        self.corrupt_rate       = corrupt_rate
        self._rng               = random.Random(seed)
        self.dropped_count      = 0
        self.errored_count      = 0
        self.corrupted_count    = 0
        self.delivered_count    = 0

    def should_drop(self) -> bool:
        if self._rng.random() < self.drop_rate:
            self.dropped_count += 1
            return True
        return False

    def should_error(self) -> bool:
        if self._rng.random() < self.error_rate:
            self.errored_count += 1
            return True
        return False

    def should_corrupt(self) -> bool:
        if self._rng.random() < self.corrupt_rate:
            self.corrupted_count += 1
            return True
        return False

    def corrupt_bytes(self, payload: bytes) -> bytes:
        """Flip random bytes to simulate bit rot / truncation."""
        if not payload:
            return payload
        data = bytearray(payload)
        n_corrupted = max(1, len(data) // 10)
        for _ in range(n_corrupted):
            idx = self._rng.randint(0, len(data) - 1)
            data[idx] = self._rng.randint(0, 255)
        return bytes(data)

    async def maybe_delay(self) -> None:
        if self.latency_ms > 0:
            jitter = self._rng.uniform(0, self.latency_jitter_ms)
            await asyncio.sleep((self.latency_ms + jitter) / 1000.0)

    @property
    def total_attempted(self) -> int:
        return self.dropped_count + self.errored_count + self.delivered_count

    @property
    def delivery_ratio(self) -> float:
        if self.total_attempted == 0:
            return 1.0
        return self.delivered_count / self.total_attempted


class FaultyKafkaProducer:
    """
    In-memory Kafka producer with fault injection.
    Messages may be dropped, delayed, or corrupted before being 'sent'.
    """

    def __init__(self, injector: FaultInjector):
        self._injector = injector
        self.messages: list[dict] = []
        self.attempted: int = 0

    async def send(
        self,
        topic: str,
        value: bytes,
        key: bytes | None = None,
        **kwargs: Any,
    ) -> None:
        self.attempted += 1
        await self._injector.maybe_delay()

        if self._injector.should_drop():
            return

        if self._injector.should_error():
            raise IOError(f"Kafka broker unavailable (injected fault #{self.attempted})")

        if self._injector.should_corrupt():
            value = self._injector.corrupt_bytes(value)

        self.messages.append({"topic": topic, "value": value, "key": key})
        self._injector.delivered_count += 1

    async def flush(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class FaultyKafkaConsumer:
    """
    Async-iterable consumer that injects drops and delays between messages.
    """

    def __init__(self, messages: list[bytes], injector: FaultInjector):
        self._messages = messages
        self._injector = injector
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._idx < len(self._messages):
            await self._injector.maybe_delay()
            payload = self._messages[self._idx]
            self._idx += 1

            if self._injector.should_drop():
                continue

            if self._injector.should_corrupt():
                payload = self._injector.corrupt_bytes(payload)

            msg = MagicMock()
            msg.value = payload
            return msg

        raise StopAsyncIteration

    async def stop(self) -> None:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Degraded HTTP service mocks
# ══════════════════════════════════════════════════════════════════════════════

class DegradedServiceConfig:
    """
    Configuration for a degraded upstream service.
    Controls response codes, latency, and partial failure rates.
    """

    def __init__(
        self,
        *,
        timeout_rate: float = 0.0,
        error_500_rate: float = 0.0,
        latency_ms: float = 0.0,
        partial_response_rate: float = 0.0,
        always_timeout: bool = False,
        seed: int = 42,
    ):
        self.timeout_rate         = timeout_rate
        self.error_500_rate       = error_500_rate
        self.latency_ms           = latency_ms
        self.partial_response_rate = partial_response_rate
        self.always_timeout       = always_timeout
        self._rng                 = random.Random(seed)
        self.call_count           = 0
        self.timeout_count        = 0
        self.error_count          = 0
        self.success_count        = 0


def _make_degraded_http_client(
    gnn_cfg: DegradedServiceConfig,
    guardian_cfg: DegradedServiceConfig,
    normal_gnn_payload: dict,
    normal_guardian_payload: dict,
) -> AsyncMock:
    """
    Build an AsyncMock httpx.AsyncClient that applies DegradedServiceConfig
    rules to GET and POST requests.
    """
    client = AsyncMock()

    async def _route(url: str, cfg: DegradedServiceConfig, normal_payload: dict):
        cfg.call_count += 1

        if cfg.always_timeout or cfg._rng.random() < cfg.timeout_rate:
            cfg.timeout_count += 1
            await asyncio.sleep(30)  # simulate hung connection
            raise asyncio.TimeoutError("Connection timed out (injected fault)")

        if cfg.latency_ms > 0:
            await asyncio.sleep(cfg.latency_ms / 1000.0)

        if cfg._rng.random() < cfg.error_500_rate:
            cfg.error_count += 1
            resp = AsyncMock()
            resp.status_code = 500
            resp.raise_for_status = MagicMock(
                side_effect=Exception(f"500 Internal Server Error: {url}")
            )
            return resp

        cfg.success_count += 1
        resp = AsyncMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()

        if cfg._rng.random() < cfg.partial_response_rate:
            resp.json.return_value = {}  # empty / partial response
        else:
            resp.json.return_value = normal_payload

        return resp

    async def _get(url: str, **kwargs):
        if "layer2" in url or "gnn" in url or ":8001" in url:
            return await _route(url, gnn_cfg, normal_gnn_payload)
        return await _route(url, guardian_cfg, normal_guardian_payload)

    async def _post(url: str, **kwargs):
        if "layer2" in url or "gnn" in url or ":8001" in url:
            return await _route(url, gnn_cfg, normal_gnn_payload)
        return await _route(url, guardian_cfg, normal_guardian_payload)

    client.get  = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    return client


# ══════════════════════════════════════════════════════════════════════════════
# Chaos fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def fault_injector_low() -> FaultInjector:
    """5% drop, 10ms latency — light degradation."""
    return FaultInjector(drop_rate=0.05, latency_ms=10, latency_jitter_ms=5)


@pytest.fixture
def fault_injector_medium() -> FaultInjector:
    """25% drop, 50ms latency, 10% errors — moderate degradation."""
    return FaultInjector(
        drop_rate=0.25, latency_ms=50, latency_jitter_ms=20,
        error_rate=0.10,
    )


@pytest.fixture
def fault_injector_severe() -> FaultInjector:
    """50% drop, 200ms latency, 30% errors, 10% corruption — severe degradation."""
    return FaultInjector(
        drop_rate=0.50, latency_ms=200, latency_jitter_ms=50,
        error_rate=0.30, corrupt_rate=0.10,
    )


@pytest.fixture
def fault_injector_total_outage() -> FaultInjector:
    """100% drop — complete Kafka broker failure."""
    return FaultInjector(drop_rate=1.0)


@pytest.fixture
def fault_injector_corruption_only() -> FaultInjector:
    """No drops, 100% corruption — simulate message serialization bugs."""
    return FaultInjector(corrupt_rate=1.0)


@pytest.fixture
def faulty_producer_low(fault_injector_low) -> FaultyKafkaProducer:
    return FaultyKafkaProducer(fault_injector_low)


@pytest.fixture
def faulty_producer_severe(fault_injector_severe) -> FaultyKafkaProducer:
    return FaultyKafkaProducer(fault_injector_severe)


@pytest.fixture
def faulty_producer_total_outage(fault_injector_total_outage) -> FaultyKafkaProducer:
    return FaultyKafkaProducer(fault_injector_total_outage)


@pytest.fixture
def faulty_producer_corruption(fault_injector_corruption_only) -> FaultyKafkaProducer:
    return FaultyKafkaProducer(fault_injector_corruption_only)


@pytest.fixture
def gnn_service_down() -> DegradedServiceConfig:
    return DegradedServiceConfig(always_timeout=True)


@pytest.fixture
def gnn_service_flapping() -> DegradedServiceConfig:
    return DegradedServiceConfig(error_500_rate=0.50, latency_ms=100)


@pytest.fixture
def guardian_service_slow() -> DegradedServiceConfig:
    return DegradedServiceConfig(latency_ms=800, latency_jitter_ms=200)


@pytest.fixture
def guardian_service_partial() -> DegradedServiceConfig:
    """Guardian responds with partial / empty JSON bodies."""
    return DegradedServiceConfig(partial_response_rate=1.0)


# ── Normal responses (used when constructing degraded clients) ─────────────────

@pytest.fixture
def normal_gnn_payload() -> dict:
    return {
        "inference_id":     _uid(),
        "timestamp":        _now(),
        "incident_type":    "FAULT",
        "graph_confidence": 0.88,
        "root_cause_node_name": "payment-svc",
        "blast_radius_count": 2,
        "is_heartbeat":     False,
        "node_count":       8,
        "inference_latency_ms": 42.0,
    }


@pytest.fixture
def normal_guardian_payload() -> dict:
    return {
        "approved":          True,
        "risk_score":        0.12,
        "risk_category":     "VERY_LOW",
        "confidence":        0.92,
        "mttr_delta_seconds": -180.0,
        "affected_pod_count": 1,
        "opa_violations":    [],
        "dry_run_succeeded": True,
        "projected_status":  "healthy",
    }


# ── Composite chaos client fixtures ───────────────────────────────────────────

@pytest.fixture
def degraded_http_client_gnn_down(
    gnn_service_down, guardian_service_slow,
    normal_gnn_payload, normal_guardian_payload,
):
    """GNN is completely down; Guardian is slow."""
    return _make_degraded_http_client(
        gnn_cfg=gnn_service_down,
        guardian_cfg=guardian_service_slow,
        normal_gnn_payload=normal_gnn_payload,
        normal_guardian_payload=normal_guardian_payload,
    )


@pytest.fixture
def degraded_http_client_guardian_partial(
    gnn_service_flapping, guardian_service_partial,
    normal_gnn_payload, normal_guardian_payload,
):
    """GNN is flapping; Guardian returns empty bodies."""
    return _make_degraded_http_client(
        gnn_cfg=gnn_service_flapping,
        guardian_cfg=guardian_service_partial,
        normal_gnn_payload=normal_gnn_payload,
        normal_guardian_payload=normal_guardian_payload,
    )


# ── Malformed payload generators ──────────────────────────────────────────────

@pytest.fixture
def malformed_ebpf_payloads() -> list[bytes]:
    """A set of invalid byte sequences that should never crash the consumer."""
    return [
        b"",                            # empty
        b"\x00" * 10,                   # null bytes
        b"\xff\xfe\xfd" * 20,           # garbage bytes
        b"not protobuf at all",          # ASCII garbage
        b'{"json": "not proto"}',        # valid JSON but not a proto
        bytes(range(256)),               # all possible byte values
        b"\x0a" * 1000,                  # malformed varint flood
    ]


@pytest.fixture
def malformed_gnn_json_payloads() -> list[dict | str]:
    """Invalid GNN inference JSON payloads that the pipeline must handle gracefully."""
    return [
        {},                                              # missing all required fields
        {"inference_id": "not-a-uuid"},                  # missing required fields
        {"incident_type": "EXPLOSION"},                  # invalid enum value
        {"graph_confidence": 99.9},                      # confidence > 1
        {"graph_confidence": -0.5},                      # negative confidence
        {"is_heartbeat": "yes"},                         # wrong type
        {"node_count": "many"},                          # wrong type
        None,                                            # null
        "not a dict",                                    # wrong type entirely
    ]


# ── Cascade simulation helpers ─────────────────────────────────────────────────

def build_oom_cascade_batch(n_oom_events: int = 10) -> TypedEbpfBatch:
    """Simulate an OOM cascade — large burst of OOM kill events."""
    meta = EventMetadata(
        kernel_ts_ns=time.monotonic_ns(),
        node_name="ip-10-0-1-42.us-east-1.compute.internal",
        pid=1234, comm="payment-svc",
        severity=EventSeverity.SEVERITY_CRITICAL,
    )
    oom_events = [
        OomKillEvent(
            meta=meta,
            victim_pid=5000 + i,
            victim_comm=f"worker-{i}",
            oom_score=990,
            victim_rss_bytes=512 * 1024 * 1024,
        )
        for i in range(n_oom_events)
    ]
    batch = TypedEbpfBatch(
        batch_id=_uid(),
        node_name="ip-10-0-1-42.us-east-1.compute.internal",
        collector_id=_uid(),
        batch_ts=_now(),
        oom_kill_events=oom_events,
        schema_ver="1.0",
    )
    batch.compute_type_counts()
    return batch


def build_capability_storm_batch(n_events: int = 50) -> TypedEbpfBatch:
    """Simulate a privilege escalation attack — rapid CAP_SYS_ADMIN checks."""
    meta = EventMetadata(
        kernel_ts_ns=time.monotonic_ns(),
        node_name="ip-10-0-1-55.us-east-1.compute.internal",
        pid=9999, comm="exploit",
        severity=EventSeverity.SEVERITY_CRITICAL,
    )
    cap_events = [
        CapabilityEvent(
            meta=meta,
            capability=LinuxCapability.CAP_SYS_ADMIN,
            allowed=False,
        )
        for _ in range(n_events)
    ]
    batch = TypedEbpfBatch(
        batch_id=_uid(),
        node_name="ip-10-0-1-55.us-east-1.compute.internal",
        collector_id=_uid(),
        batch_ts=_now(),
        capability_events=cap_events,
        schema_ver="1.0",
    )
    batch.compute_type_counts()
    return batch


@pytest.fixture
def oom_cascade_batch() -> TypedEbpfBatch:
    return build_oom_cascade_batch(n_oom_events=10)


@pytest.fixture
def capability_storm_batch() -> TypedEbpfBatch:
    return build_capability_storm_batch(n_events=50)


@pytest.fixture
def high_volume_batch_factory():
    """Factory for creating large batches — use for throughput / backpressure tests."""
    def _factory(n_batches: int = 100, oom_per_batch: int = 5) -> list[TypedEbpfBatch]:
        return [build_oom_cascade_batch(n_oom_events=oom_per_batch) for _ in range(n_batches)]
    return _factory
