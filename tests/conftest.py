"""
CCDT Test Suite — Shared Fixtures (conftest.py)
═══════════════════════════════════════════════════════════════════════════════
Central fixture repository shared across all test layers (unit, integration,
e2e, chaos).

Fixture categories
──────────────────
  Proto fixtures       Fully populated proto/dataclass objects for each layer
  Service mocks        AsyncMock HTTP clients for upstream dependencies
  Kafka fixtures       In-memory Kafka producer/consumer stubs
  FastAPI fixtures     Starlette TestClient factories for each service app
  Data factories       Helpers that build realistic test data at any scale

Design principles
─────────────────
  • All fixtures are independent — no hidden inter-fixture state
  • Network-free by default: every fixture mocks external I/O
  • Async-compatible: async fixtures use asyncio_mode=auto
  • No real Kubernetes, Kafka, or OPA required for unit/integration tests
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Proto shims ───────────────────────────────────────────────────────────────
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, CapabilityEvent, OomKillEvent, TcpRetransmitEvent,
    SchedLatencyEvent, ExecveEvent, NetworkConnectEvent,
    EventMetadata, EventSeverity, LinuxCapability, NetworkProtocol,
)
from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, NodeFeatures, EdgeFeatures,
    TopologyNode, TopologyEdge, CausalChainNode, TopFeature,
    CounterfactualResult, GnnInferenceResult, TopologySnapshot,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    ScaleParameters, RollbackParameters, GhostSimulationResult,
    ActionRequest, ActionResult, ActionHistoryEntry,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, MessageType, SessionState, ToolName, StreamEventType,
    TokenUsage, ToolCall, ToolResult, ChatMessage, SessionContext,
    StreamEvent, IncidentReport,
)


# ══════════════════════════════════════════════════════════════════════════════
# Timestamp helpers
# ══════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _uid() -> str:
    return str(uuid.uuid4())


# ══════════════════════════════════════════════════════════════════════════════
# Layer-1 fixtures — eBPF events
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def base_meta() -> EventMetadata:
    """Minimal valid EventMetadata for use in eBPF event fixtures."""
    return EventMetadata(
        kernel_ts_ns=time.monotonic_ns(),
        wall_clock_ts=_now(),
        node_name="ip-10-0-1-42.us-east-1.compute.internal",
        pod_name="payment-svc-abc123",
        namespace="production",
        container_id="containerd://abc123",
        container_name="payment",
        pid=1234,
        tgid=1234,
        uid=0,
        gid=0,
        comm="payment-svc",
        ppid=1,
        collector_ver="1.0.0",
        severity=EventSeverity.SEVERITY_HIGH,
        labels={"env": "prod", "team": "payments"},
    )


@pytest.fixture
def capability_event(base_meta) -> CapabilityEvent:
    return CapabilityEvent(
        meta=base_meta,
        capability=LinuxCapability.CAP_NET_ADMIN,
        syscall_nr=21,
        allowed=False,
        cap_bitmask="0x100000",
        audit_serial=9999,
    )


@pytest.fixture
def oom_kill_event(base_meta) -> OomKillEvent:
    return OomKillEvent(
        meta=base_meta,
        victim_pid=5555,
        victim_comm="java",
        oom_score=950,
        victim_rss_bytes=1024 * 1024 * 512,
        total_vm_bytes=1024 * 1024 * 1024,
        total_rss_bytes=1024 * 1024 * 800,
        cgroup_path="/kubepods/besteffort/pod-abc/container-xyz",
        oom_kill_count_5m=3,
        oom_flags=0,
    )


@pytest.fixture
def tcp_retransmit_event(base_meta) -> TcpRetransmitEvent:
    return TcpRetransmitEvent(
        meta=base_meta,
        src_addr="10.0.1.42",
        src_port=54321,
        dst_addr="10.0.1.100",
        dst_port=5432,
        protocol=NetworkProtocol.PROTO_TCP,
        tcp_state=1,
        retransmit_count=12,
        rtt_us=50000,
        rto_us=200000,
        snd_cwnd=4,
        sk_backlog=128,
    )


@pytest.fixture
def ebpf_batch(capability_event, oom_kill_event, tcp_retransmit_event) -> TypedEbpfBatch:
    """Realistic 3-event batch as would be published to Kafka."""
    batch = TypedEbpfBatch(
        batch_id=_uid(),
        node_name="ip-10-0-1-42.us-east-1.compute.internal",
        collector_id=_uid(),
        batch_ts=_now(),
        capability_events=[capability_event],
        oom_kill_events=[oom_kill_event],
        tcp_retransmit_events=[tcp_retransmit_event],
        schema_ver="1.0",
    )
    batch.compute_type_counts()
    return batch


# ══════════════════════════════════════════════════════════════════════════════
# Layer-2 fixtures — topology and GNN inference
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def healthy_node() -> TopologyNode:
    return TopologyNode(
        node_id="svc-api-gw",
        name="api-gateway",
        node_class=NodeClass.NODE_CLASS_HEALTHY,
        class_confidence=0.97,
        namespace="production",
        deployment="api-gateway",
        features=NodeFeatures(
            cpu_utilization=0.35,
            memory_utilization=0.40,
            ready_replicas_ratio=1.0,
            restart_count=0.0,
            tcp_retransmit_rate=0.01,
        ),
    )


@pytest.fixture
def fault_node() -> TopologyNode:
    return TopologyNode(
        node_id="svc-payment",
        name="payment-svc",
        node_class=NodeClass.NODE_CLASS_FAULT,
        class_confidence=0.91,
        namespace="production",
        deployment="payment-svc",
        is_root_cause=True,
        features=NodeFeatures(
            cpu_utilization=0.92,
            memory_utilization=0.98,
            oom_kill_rate=0.85,
            restart_count=0.7,
            ready_replicas_ratio=0.5,
            tcp_retransmit_rate=0.4,
        ),
    )


@pytest.fixture
def attack_node() -> TopologyNode:
    return TopologyNode(
        node_id="svc-auth",
        name="auth-svc",
        node_class=NodeClass.NODE_CLASS_ATTACK,
        class_confidence=0.94,
        namespace="production",
        deployment="auth-svc",
        is_root_cause=True,
        features=NodeFeatures(
            capability_event_rate=0.95,
            syscall_anomaly_score=0.88,
            sensitive_file_rate=0.75,
            execve_rate=0.70,
            cpu_utilization=0.60,
        ),
    )


@pytest.fixture
def topology_snapshot(healthy_node, fault_node) -> TopologySnapshot:
    edge = TopologyEdge(
        edge_id="edge-gw-payment",
        source_node_id=healthy_node.node_id,
        target_node_id=fault_node.node_id,
        features=EdgeFeatures(
            request_rate=0.8,
            error_rate=0.35,
            latency_p99_ms=0.7,
        ),
        is_causal=True,
    )
    return TopologySnapshot(
        snapshot_id=_uid(),
        timestamp=_now(),
        nodes=[healthy_node, fault_node],
        edges=[edge],
        incident_type=IncidentType.INCIDENT_FAULT,
        cluster_name="prod-us-east-1",
    )


@pytest.fixture
def fault_inference(fault_node, healthy_node) -> GnnInferenceResult:
    """Realistic fault-detection GNN inference result."""
    return GnnInferenceResult(
        inference_id=_uid(),
        timestamp=_now(),
        incident_type=IncidentType.INCIDENT_FAULT,
        graph_confidence=0.88,
        root_cause_node_id=fault_node.node_id,
        root_cause_node_name=fault_node.name,
        root_cause_confidence=0.91,
        blast_radius_node_ids=[healthy_node.node_id],
        blast_radius_count=1,
        node_classifications=[fault_node, healthy_node],
        causal_chain=[
            CausalChainNode(
                node_id=fault_node.node_id,
                node_name=fault_node.name,
                node_class=NodeClass.NODE_CLASS_FAULT,
                causal_score=0.91,
                do_calculus_prob=0.87,
                explanation="OOM kills propagating latency to dependents",
                depth=0,
            )
        ],
        top_features=[
            TopFeature(
                feature_name="oom_kill_rate",
                node_name=fault_node.name,
                importance=0.82,
                value=0.85,
                threshold=0.20,
                explanation="OOM kill rate 4.25× above baseline",
            ),
            TopFeature(
                feature_name="memory_utilization",
                node_name=fault_node.name,
                importance=0.74,
                value=0.98,
                threshold=0.85,
                explanation="Memory at 98% — approaching limit",
            ),
        ],
        inference_latency_ms=41.2,
        node_count=2,
        edge_count=1,
        is_heartbeat=False,
        schema_ver="1.0",
    )


@pytest.fixture
def attack_inference(attack_node, healthy_node) -> GnnInferenceResult:
    """Realistic attack-detection GNN inference result."""
    return GnnInferenceResult(
        inference_id=_uid(),
        timestamp=_now(),
        incident_type=IncidentType.INCIDENT_ATTACK,
        graph_confidence=0.93,
        root_cause_node_id=attack_node.node_id,
        root_cause_node_name=attack_node.name,
        root_cause_confidence=0.94,
        blast_radius_node_ids=[healthy_node.node_id],
        blast_radius_count=1,
        node_classifications=[attack_node, healthy_node],
        causal_chain=[
            CausalChainNode(
                node_id=attack_node.node_id,
                node_name=attack_node.name,
                node_class=NodeClass.NODE_CLASS_ATTACK,
                causal_score=0.94,
                do_calculus_prob=0.91,
                explanation="Privilege escalation + lateral movement indicators",
                depth=0,
            )
        ],
        top_features=[
            TopFeature(
                feature_name="capability_event_rate",
                node_name=attack_node.name,
                importance=0.91,
                value=0.95,
                threshold=0.30,
                explanation="CAP_NET_ADMIN + CAP_SYS_ADMIN checked 18×/s",
            ),
        ],
        inference_latency_ms=38.7,
        node_count=2,
        edge_count=1,
        is_heartbeat=False,
        schema_ver="1.0",
    )


@pytest.fixture
def heartbeat_inference() -> GnnInferenceResult:
    """Healthy-cluster heartbeat inference (no active incident)."""
    return GnnInferenceResult(
        inference_id=_uid(),
        timestamp=_now(),
        incident_type=IncidentType.INCIDENT_NONE,
        graph_confidence=0.02,
        node_count=9,
        edge_count=14,
        is_heartbeat=True,
        schema_ver="1.0",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Layer-3 fixtures — actions, ghost results
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def low_risk_ghost() -> GhostSimulationResult:
    return GhostSimulationResult(
        risk_score=0.12,
        risk_category=RiskCategory.RISK_VERY_LOW,
        confidence=0.92,
        mttr_delta_seconds=-180.0,
        traffic_impact_pct=5.0,
        availability_impact=0.001,
        affected_pod_count=1,
        opa_approved=True,
        dry_run_succeeded=True,
        projected_status="healthy",
        sim_duration_ms=45.0,
        sim_timestamp=_now(),
    )


@pytest.fixture
def high_risk_ghost() -> GhostSimulationResult:
    return GhostSimulationResult(
        risk_score=0.72,
        risk_category=RiskCategory.RISK_HIGH,
        confidence=0.65,
        mttr_delta_seconds=300.0,
        traffic_impact_pct=45.0,
        availability_impact=0.15,
        affected_pod_count=12,
        opa_approved=False,
        opa_violations=["cpu_threshold: scale_down blocked — CPU below 20%"],
        dry_run_succeeded=True,
        projected_status="degraded",
        sim_duration_ms=88.0,
        sim_timestamp=_now(),
    )


@pytest.fixture
def restart_pod_request(fault_inference, low_risk_ghost) -> ActionRequest:
    return ActionRequest(
        request_id=_uid(),
        requested_at=_now(),
        action_name=ActionName.ACTION_RESTART_POD,
        action_label="restart_pod",
        target_node_id=fault_inference.root_cause_node_id,
        target_node_name=fault_inference.root_cause_node_name,
        target_namespace="production",
        target_resource="payment-svc-pod-abc123",
        inference_id=fault_inference.inference_id,
        trigger_class=NodeClass.NODE_CLASS_FAULT,
        trigger_confidence=fault_inference.graph_confidence,
        root_cause_node=fault_inference.root_cause_node_name,
        policy_version="1.0",
        rl_q_value=2.34,
        autonomy_mode=AutonomyMode.AUTONOMY_SUPERVISED,
        requester="rl-policy",
        ghost_result=low_risk_ghost,
    )


@pytest.fixture
def isolate_container_request(attack_inference, low_risk_ghost) -> ActionRequest:
    return ActionRequest(
        request_id=_uid(),
        requested_at=_now(),
        action_name=ActionName.ACTION_ISOLATE_CONTAINER,
        action_label="isolate_container",
        target_node_id=attack_inference.root_cause_node_id,
        target_node_name=attack_inference.root_cause_node_name,
        target_namespace="production",
        target_resource="auth-svc",
        inference_id=attack_inference.inference_id,
        trigger_class=NodeClass.NODE_CLASS_ATTACK,
        trigger_confidence=attack_inference.graph_confidence,
        root_cause_node=attack_inference.root_cause_node_name,
        policy_version="1.0",
        rl_q_value=3.12,
        autonomy_mode=AutonomyMode.AUTONOMY_FULL_AUTO,
        requester="rl-policy",
        ghost_result=low_risk_ghost,
    )


@pytest.fixture
def succeeded_action_result(restart_pod_request) -> ActionResult:
    return ActionResult(
        audit_id=_uid(),
        request=restart_pod_request,
        status=ActionStatus.STATUS_SUCCEEDED,
        message="Pod payment-svc-pod-abc123 deleted — controller will recreate",
        requested_at=_now(),
        executed_at=_now(),
        completed_at=_now(),
        execution_duration_ms=1250.0,
        k8s_resource_version="123456",
        k8s_uid=_uid(),
        verified_effect=True,
        post_action_health=0.92,
        verification_note="Pod restarted, OOM rate dropped to 0",
        autonomy_mode=AutonomyMode.AUTONOMY_SUPERVISED,
        approved_by="operator:alice",
        incident_id=_uid(),
        schema_ver="1.0",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Layer-4 fixtures — copilot sessions
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def copilot_session() -> SessionContext:
    session = SessionContext(
        session_id=_uid(),
        operator_id="operator:alice",
        operator_name="Alice Smith",
        state=SessionState.SESSION_ACTIVE,
        created_at=_now(),
    )
    session.add_message(ChatMessage(
        role=MessageRole.ROLE_USER,
        content="What is causing the high latency in payment service?",
        message_type=MessageType.MSG_TEXT,
        created_at=_now(),
    ))
    session.add_message(ChatMessage(
        role=MessageRole.ROLE_ASSISTANT,
        content="Based on the GNN analysis, the root cause is OOM pressure in payment-svc.",
        message_type=MessageType.MSG_TEXT,
        token_usage=TokenUsage(input_tokens=450, output_tokens=120),
        model="claude-sonnet-4-20250514",
        created_at=_now(),
    ))
    return session


@pytest.fixture
def incident_report(fault_inference, restart_pod_request, low_risk_ghost) -> IncidentReport:
    return IncidentReport(
        report_id=_uid(),
        detected_at=_now(),
        inference_id=fault_inference.inference_id,
        incident_type=IncidentType.INCIDENT_FAULT,
        graph_confidence=fault_inference.graph_confidence,
        root_cause_service=fault_inference.root_cause_node_name,
        root_cause_namespace="production",
        root_cause_class=NodeClass.NODE_CLASS_FAULT,
        affected_services=2,
        affected_namespaces=["production"],
        top_features=fault_inference.top_features,
        proposed_action=restart_pod_request,
        ghost_result=low_risk_ghost,
        nl_summary="OOM kills in payment-svc are causing cascading latency spikes.",
        severity="high",
        incident_id=_uid(),
        schema_ver="1.0",
    )


# ══════════════════════════════════════════════════════════════════════════════
# HTTP mock factories
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_gnn_response(fault_inference) -> dict:
    """JSON payload as returned by GET /infer on layer2-cognitive:8001."""
    return {
        "inference_id":         fault_inference.inference_id,
        "timestamp":            fault_inference.timestamp,
        "incident_type":        "FAULT",
        "graph_confidence":     fault_inference.graph_confidence,
        "root_cause_node_name": fault_inference.root_cause_node_name,
        "blast_radius_count":   fault_inference.blast_radius_count,
        "is_heartbeat":         False,
        "node_count":           fault_inference.node_count,
        "inference_latency_ms": fault_inference.inference_latency_ms,
    }


@pytest.fixture
def mock_guardian_preview_response(low_risk_ghost) -> dict:
    """JSON payload as returned by POST /actions/preview on layer3-guardian:8002."""
    return {
        "approved":          low_risk_ghost.opa_approved,
        "risk_score":        low_risk_ghost.risk_score,
        "risk_category":     "VERY_LOW",
        "confidence":        low_risk_ghost.confidence,
        "mttr_delta_seconds": low_risk_ghost.mttr_delta_seconds,
        "affected_pod_count": low_risk_ghost.affected_pod_count,
        "opa_violations":    [],
        "dry_run_succeeded": True,
        "projected_status":  "healthy",
    }


@pytest.fixture
def mock_topology_response(topology_snapshot) -> dict:
    """JSON payload as returned by GET /topology on layer2-cognitive:8001."""
    return {
        "snapshot_id":   topology_snapshot.snapshot_id,
        "timestamp":     topology_snapshot.timestamp,
        "node_count":    topology_snapshot.node_count,
        "edge_count":    topology_snapshot.edge_count,
        "incident_type": "FAULT",
        "nodes": [
            {
                "node_id":          n.node_id,
                "name":             n.name,
                "node_class":       NodeClass(n.node_class).name.replace("NODE_CLASS_", "").lower(),
                "class_confidence": n.class_confidence,
                "is_root_cause":    n.is_root_cause,
                "namespace":        n.namespace,
            }
            for n in topology_snapshot.nodes
        ],
    }


@pytest.fixture
def mock_http_client(mock_gnn_response, mock_topology_response, mock_guardian_preview_response):
    """
    AsyncMock httpx.AsyncClient that intercepts all service-to-service calls.
    Routes based on URL path prefix.
    """
    client = AsyncMock()

    async def _get(url: str, **kwargs):
        resp = AsyncMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "/topology" in url:
            resp.json.return_value = mock_topology_response
        elif "/health" in url:
            resp.json.return_value = {"status": "healthy"}
        elif "/actions/history" in url:
            resp.json.return_value = {"entries": [], "total_count": 0}
        elif "/events" in url:
            resp.json.return_value = {"events": []}
        else:
            resp.json.return_value = mock_gnn_response
        return resp

    async def _post(url: str, **kwargs):
        resp = AsyncMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "/preview" in url:
            resp.json.return_value = mock_guardian_preview_response
        elif "/execute" in url:
            resp.json.return_value = {
                "audit_id": _uid(), "status": "SUCCEEDED", "message": "Action executed"
            }
        elif "/infer" in url:
            resp.json.return_value = mock_gnn_response
        else:
            resp.json.return_value = {"ok": True}
        return resp

    client.get  = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    return client


# ══════════════════════════════════════════════════════════════════════════════
# Kafka mock
# ══════════════════════════════════════════════════════════════════════════════

class _FakeKafkaProducer:
    """In-memory Kafka producer that stores messages for assertion."""
    def __init__(self):
        self.messages: list[dict] = []

    async def send(self, topic: str, value: bytes, key: bytes | None = None, **kw):
        self.messages.append({"topic": topic, "value": value, "key": key})

    async def flush(self): pass
    async def stop(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass


class _FakeKafkaConsumer:
    """Async-iterable in-memory Kafka consumer."""
    def __init__(self, messages: list[bytes]):
        self._messages = messages
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._messages):
            raise StopAsyncIteration
        msg = MagicMock()
        msg.value = self._messages[self._idx]
        self._idx += 1
        return msg

    async def stop(self): pass


@pytest.fixture
def fake_kafka_producer():
    return _FakeKafkaProducer()


@pytest.fixture
def fake_kafka_consumer_factory():
    """Factory that creates a FakeKafkaConsumer with pre-loaded messages."""
    def _factory(messages: list[bytes]) -> _FakeKafkaConsumer:
        return _FakeKafkaConsumer(messages)
    return _factory


# ══════════════════════════════════════════════════════════════════════════════
# OPA mock
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_opa_allow_all():
    """Mock OPA client that approves all policies."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=AsyncMock(
        status_code=200,
        json=MagicMock(return_value={"result": {"allow": True, "violations": []}}),
        raise_for_status=MagicMock(),
    ))
    return client


@pytest.fixture
def mock_opa_deny_cpu():
    """Mock OPA client that denies cpu_threshold policy."""
    async def _post(url, **kwargs):
        resp = AsyncMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "cpu_threshold" in url:
            resp.json.return_value = {
                "result": {"allow": False, "violations": ["cpu_threshold: scale_down blocked"]}
            }
        else:
            resp.json.return_value = {"result": {"allow": True, "violations": []}}
        return resp

    client = AsyncMock()
    client.post = AsyncMock(side_effect=_post)
    return client


# ══════════════════════════════════════════════════════════════════════════════
# Kubernetes mock
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_k8s_client():
    """Mock kubernetes.client that succeeds for all operations."""
    k8s = MagicMock()
    k8s.CoreV1Api.return_value.delete_namespaced_pod.return_value = MagicMock(
        metadata=MagicMock(resource_version="123456", uid=_uid())
    )
    k8s.AppsV1Api.return_value.patch_namespaced_deployment_scale.return_value = MagicMock(
        spec=MagicMock(replicas=3)
    )
    k8s.NetworkingV1Api.return_value.create_namespaced_network_policy.return_value = MagicMock(
        metadata=MagicMock(name="ccdt-isolate-auth-svc", uid=_uid())
    )
    return k8s
