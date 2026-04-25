"""
CCDT Integration Test Suite — Shared Fixtures (tests/integration/conftest.py)
═══════════════════════════════════════════════════════════════════════════════
Integration-scoped fixtures wire multiple layers together using in-memory
transport stubs (Kafka, Redis, HTTP) without requiring running services.

Fixture categories
──────────────────
  InMemoryKafka    Async publish/consume loop with a deque as the broker
  ServiceCluster   Wires all 4 layer mocks into a coherent system
  AssertHelpers    Cross-layer assertion helpers (e.g. assert_incident_published)
  ScenarioBuilders Helpers that pre-wire common incident scenarios end-to-end

Extends:
  tests/conftest.py (top-level)
  tests/unit/conftest.py  (re-exported for convenience)

Usage:
  The in-memory Kafka broker routes messages between layers without any
  network. Each topic maps to an asyncio.Queue. Producers call
  `kafka.produce(topic, message)` and consumers `await kafka.consume(topic)`.
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, CapabilityEvent, OomKillEvent, TcpRetransmitEvent,
    EventMetadata, EventSeverity, LinuxCapability,
)
from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, NodeFeatures, TopologyNode, TopologyEdge,
    GnnInferenceResult, TopologySnapshot, CausalChainNode, TopFeature,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    GhostSimulationResult, ActionRequest, ActionResult,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, SessionContext, ChatMessage, IncidentReport, TokenUsage,
)


def _uid() -> str: return str(uuid.uuid4())
def _ts()  -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# In-memory Kafka broker
# ══════════════════════════════════════════════════════════════════════════════

class InMemoryKafkaBroker:
    """
    Async in-memory Kafka broker backed by asyncio.Queue per topic.

    Supports:
      produce(topic, value, key=None)   → put message onto topic queue
      consume(topic, timeout=1.0)       → get one message (or None on timeout)
      consume_all(topic)                → drain all messages from topic
      reset()                           → clear all queues (call between tests)
      message_count(topic)              → number of messages currently queued
    """

    TOPICS = [
        "ccdt.ebpf.events",
        "ccdt.gnn.inference",
        "ccdt.guardian.actions",
        "ccdt.incidents",
    ]

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._produced_count: dict[str, int] = defaultdict(int)
        for t in self.TOPICS:
            self._queues[t] = asyncio.Queue()

    async def produce(
        self, topic: str, value: bytes | dict | str,
        key: bytes | str | None = None,
    ) -> None:
        """Produce a message. Auto-creates topic queue if not in TOPICS list."""
        if topic not in self._queues:
            self._queues[topic] = asyncio.Queue()
        if isinstance(value, dict):
            value = json.dumps(value).encode()
        elif isinstance(value, str):
            value = value.encode()
        await self._queues[topic].put({"key": key, "value": value, "topic": topic})
        self._produced_count[topic] += 1

    async def consume(
        self, topic: str, timeout: float = 1.0
    ) -> dict | None:
        """Consume one message from topic. Returns None if empty after timeout."""
        if topic not in self._queues:
            return None
        try:
            return await asyncio.wait_for(self._queues[topic].get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def consume_all(self, topic: str) -> list[dict]:
        """Drain all messages currently on topic. Non-blocking."""
        msgs = []
        q = self._queues.get(topic)
        if q is None:
            return msgs
        while not q.empty():
            try:
                msgs.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return msgs

    def message_count(self, topic: str) -> int:
        """Return number of messages currently queued."""
        q = self._queues.get(topic)
        return q.qsize() if q else 0

    def total_produced(self, topic: str) -> int:
        return self._produced_count[topic]

    def reset(self) -> None:
        """Clear all queues between tests."""
        for q in self._queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._produced_count.clear()

    def make_producer_mock(self) -> AsyncMock:
        """Return an AsyncMock that delegates to this broker's produce()."""
        producer = AsyncMock()
        producer.send = self.produce
        producer.flush = AsyncMock()
        producer.close = AsyncMock()
        return producer

    def make_consumer_mock(self, topic: str) -> AsyncMock:
        """Return an AsyncMock that delegates to this broker's consume()."""
        consumer = AsyncMock()
        consumer.topic = topic
        consumer.__aiter__ = self._make_async_iter(topic)
        consumer.close = AsyncMock()
        return consumer

    def _make_async_iter(self, topic: str):
        broker = self
        async def _aiter(self_inner):
            while True:
                msg = await broker.consume(topic, timeout=0.05)
                if msg is None:
                    break
                yield MagicMock(
                    topic=topic,
                    value=msg["value"],
                    key=msg["key"],
                    offset=0,
                    partition=0,
                )
        return _aiter


@pytest.fixture
def kafka_broker() -> InMemoryKafkaBroker:
    """Fresh in-memory Kafka broker per test."""
    return InMemoryKafkaBroker()


@pytest.fixture
async def kafka_broker_async() -> AsyncGenerator[InMemoryKafkaBroker, None]:
    """Async fixture version — cleans up queues after each test."""
    broker = InMemoryKafkaBroker()
    yield broker
    broker.reset()


# ══════════════════════════════════════════════════════════════════════════════
# Integration-grade proto fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def oom_batch() -> TypedEbpfBatch:
    """OOM kill batch from payment-svc node — canonical FAULT trigger."""
    meta = EventMetadata.now("node-prod-1", 42001, "payment-svc",
                              namespace="production", pod_name="payment-pod-abc")
    return TypedEbpfBatch(
        batch_id=_uid(),
        node_name="node-prod-1",
        collector_id=_uid(),
        batch_ts=_ts(),
        schema_ver="1.0",
        oom_kill_events=[
            OomKillEvent(
                meta=meta,
                victim_pid=42001, victim_comm="payment-svc",
                oom_score=1000, victim_rss_bytes=2_147_483_648,
                total_vm_bytes=3_221_225_472, cgroup_path="/kubepods/payment-svc",
            ),
            OomKillEvent(
                meta=meta,
                victim_pid=42002, victim_comm="payment-worker",
                oom_score=950, victim_rss_bytes=1_073_741_824,
            ),
        ],
    )


@pytest.fixture
def attack_batch() -> TypedEbpfBatch:
    """Capability event batch from auth-svc — canonical ATTACK trigger."""
    meta = EventMetadata.now(
        "node-prod-2", 7001, "auth-svc",
        namespace="production", pod_name="auth-pod-xyz",
        severity=EventSeverity.SEVERITY_CRITICAL,
    )
    return TypedEbpfBatch(
        batch_id=_uid(),
        node_name="node-prod-2",
        collector_id=_uid(),
        batch_ts=_ts(),
        schema_ver="1.0",
        capability_events=[
            CapabilityEvent(
                meta=meta,
                capability=LinuxCapability.CAP_NET_ADMIN, allowed=True,
                syscall_nr=317, cap_bitmask="0x0000000000080000",
            ),
            CapabilityEvent(
                meta=meta,
                capability=LinuxCapability.CAP_SYS_PTRACE, allowed=True,
                syscall_nr=321, cap_bitmask="0x0000000000400000",
            ),
        ],
    )


@pytest.fixture
def fault_gnn_result() -> GnnInferenceResult:
    """FAULT inference result for the OOM cascade scenario."""
    nodes = [
        TopologyNode(
            node_id="svc-payment", name="payment-svc",
            node_class=NodeClass.NODE_CLASS_FAULT, class_confidence=0.91,
            is_root_cause=True, namespace="production",
            features=NodeFeatures(oom_kill_rate=0.85, cpu_utilization=0.92,
                                   memory_utilization=0.97, restart_count=5),
        ),
        TopologyNode(
            node_id="svc-api", name="api-gateway",
            node_class=NodeClass.NODE_CLASS_HEALTHY, class_confidence=0.88,
            namespace="production",
        ),
    ]
    return GnnInferenceResult(
        inference_id=_uid(),
        incident_type=IncidentType.INCIDENT_FAULT,
        graph_confidence=0.91,
        root_cause_node_id="svc-payment",
        root_cause_node_name="payment-svc",
        root_cause_confidence=0.91,
        blast_radius_node_ids=["svc-api"],
        blast_radius_count=1,
        node_classifications=nodes,
        node_count=2, edge_count=3,
        inference_latency_ms=38.7,
        causal_chain=[
            CausalChainNode(
                node_id="svc-payment", node_name="payment-svc",
                causal_score=0.91, depth=0,
                explanation="OOM rate 0.85/s — memory leak suspected",
            ),
        ],
        top_features=[
            TopFeature(
                feature_name="oom_kill_rate", node_name="payment-svc",
                importance=0.94, value=0.85, threshold=0.05,
                explanation="17× baseline OOM rate",
            ),
        ],
    )


@pytest.fixture
def attack_gnn_result() -> GnnInferenceResult:
    """ATTACK inference result for the lateral movement scenario."""
    nodes = [
        TopologyNode(
            node_id="svc-auth",    name="auth-svc",
            node_class=NodeClass.NODE_CLASS_ATTACK, class_confidence=0.93,
            is_root_cause=True, namespace="production",
            features=NodeFeatures(
                capability_event_rate=1.0,
                syscall_anomaly_score=0.91,
                sensitive_file_rate=0.78,
            ),
        ),
        TopologyNode(
            node_id="svc-secrets", name="secrets-store",
            node_class=NodeClass.NODE_CLASS_ATTACK, class_confidence=0.72,
            namespace="production",
            features=NodeFeatures(sensitive_file_rate=0.92),
        ),
    ]
    return GnnInferenceResult(
        inference_id=_uid(),
        incident_type=IncidentType.INCIDENT_ATTACK,
        graph_confidence=0.93,
        root_cause_node_id="svc-auth",
        root_cause_node_name="auth-svc",
        root_cause_confidence=0.93,
        blast_radius_node_ids=["svc-secrets", "svc-payment"],
        blast_radius_count=2,
        node_classifications=nodes,
        node_count=2, edge_count=3,
        inference_latency_ms=41.2,
    )


@pytest.fixture
def guardian_action_approved(fault_gnn_result) -> ActionRequest:
    return ActionRequest(
        request_id=_uid(),
        action_name=ActionName.ACTION_RESTART_POD,
        action_label="restart_pod",
        target_node_name="payment-svc-pod-abc",
        target_namespace="production",
        inference_id=fault_gnn_result.inference_id,
        trigger_class=NodeClass.NODE_CLASS_FAULT,
        trigger_confidence=0.91,
        autonomy_mode=AutonomyMode.AUTONOMY_SUPERVISED,
        ghost_result=GhostSimulationResult(
            risk_score=0.12, risk_category=RiskCategory.RISK_VERY_LOW,
            confidence=0.92, opa_approved=True, affected_pod_count=1,
            mttr_delta_seconds=-180.0,
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Service wire-up helpers
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_layer1_http():
    """HTTP client stub for Layer-1 /events endpoint."""
    client = AsyncMock()
    client.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "node":          "node-prod-1",
            "oom_kills_1m":  12,
            "capabilities":  3,
            "tcp_retransmits": 45,
        }),
    )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    return client


@pytest.fixture
def mock_guardian_http(fault_gnn_result):
    """HTTP client stub for Guardian /actions endpoints."""
    client = AsyncMock()

    preview_resp = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "risk_score": 0.12, "risk_category": "VERY_LOW",
            "approved": True,   "confidence": 0.92,
            "opa_approved": True,
        }),
    )
    exec_resp = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "audit_id": str(uuid.uuid4()),
            "status":   "SUCCEEDED",
            "message":  "Pod restarted",
            "execution_duration_ms": 1243.0,
        }),
    )
    history_resp = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"actions": [], "total": 0}),
    )
    client.post.side_effect = lambda url, **kw: (
        preview_resp if "preview" in url else exec_resp
    )
    client.get.return_value = history_resp
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    return client


# ══════════════════════════════════════════════════════════════════════════════
# Cross-layer assertion helpers
# ══════════════════════════════════════════════════════════════════════════════

class IntegrationAssertions:
    """
    Rich assertion helpers for cross-layer integration tests.
    Call via the `assert_integration` fixture.
    """

    @staticmethod
    async def assert_incident_published(
        kafka: InMemoryKafkaBroker,
        expected_type: str,
        timeout: float = 1.0,
    ) -> dict:
        """Assert a message appeared on ccdt.incidents within timeout."""
        msg = await kafka.consume("ccdt.incidents", timeout=timeout)
        assert msg is not None, "No incident published to ccdt.incidents"
        body = json.loads(msg["value"])
        assert body.get("incident_type") == expected_type, (
            f"Expected incident_type={expected_type!r}, got {body.get('incident_type')!r}"
        )
        return body

    @staticmethod
    async def assert_action_published(
        kafka: InMemoryKafkaBroker,
        expected_action: str,
        timeout: float = 1.0,
    ) -> dict:
        """Assert a Guardian action appeared on ccdt.guardian.actions."""
        msg = await kafka.consume("ccdt.guardian.actions", timeout=timeout)
        assert msg is not None, "No action published to ccdt.guardian.actions"
        body = json.loads(msg["value"])
        assert body.get("action_name") == expected_action, (
            f"Expected action_name={expected_action!r}, got {body.get('action_name')!r}"
        )
        return body

    @staticmethod
    def assert_gnn_result_valid(result: GnnInferenceResult) -> None:
        """Assert GnnInferenceResult meets minimum validity requirements."""
        assert result.inference_id, "inference_id must be set"
        assert 0.0 <= result.graph_confidence <= 1.0, (
            f"graph_confidence {result.graph_confidence} out of [0,1]"
        )
        if result.is_active_incident:
            assert result.root_cause_node_name, "root_cause_node_name required for active incident"
            assert result.blast_radius_count >= 0

    @staticmethod
    def assert_action_request_valid(req: ActionRequest) -> None:
        """Assert ActionRequest meets Guardian's minimum requirements."""
        assert req.request_id, "request_id must be set"
        assert req.action_name, "action_name must be set"
        assert req.target_namespace, "target_namespace must be set"
        if req.ghost_result:
            assert 0.0 <= req.ghost_result.risk_score <= 1.0


@pytest.fixture
def assert_integration() -> IntegrationAssertions:
    return IntegrationAssertions()


# ══════════════════════════════════════════════════════════════════════════════
# Full environment setup
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def env_integration(monkeypatch):
    """
    Environment variables for integration tests — uses mock service URLs
    and relaxed thresholds to encourage action execution.
    """
    env_vars = {
        "LOG_LEVEL":                  "WARNING",   # less noise in integration
        "AUTONOMY_MODE":              "supervised",
        "GNN_SERVICE_URL":            "http://mock-gnn:8001",
        "GUARDIAN_SERVICE_URL":       "http://mock-guardian:8002",
        "EBPF_SERVICE_URL":           "http://mock-layer1:9100",
        "OPA_URL":                    "http://mock-opa:8181",
        "KAFKA_BOOTSTRAP_SERVERS":    "mock-kafka:9092",
        "ANTHROPIC_API_KEY":          "sk-ant-integration-test",
        "JWT_SECRET":                 "integration-test-secret-32chars",
        "GHOST_RISK_THRESHOLD":       "0.50",      # relaxed for integration tests
        "GHOST_CONFIDENCE_MIN":       "0.60",
        "AUTO_REPORT_CONFIDENCE":     "0.80",
        "K8S_NAMESPACE":              "integration-test",
        "SERVICE_NAME":               "integration-test",
        "INFER_POLL_S":               "0.1",       # fast polling in tests
    }
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    return env_vars
