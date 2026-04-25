"""
CCDT Unit Test Suite — Shared Fixtures (tests/unit/conftest.py)
═══════════════════════════════════════════════════════════════════════════════
Unit-scoped fixtures: lightweight, network-free, sub-5ms setup.
All heavy objects (models, Kafka, K8s clients) are replaced with MagicMock.

Hierarchy:
  tests/conftest.py          ← top-level; proto objects, data factories
  tests/unit/conftest.py     ← this file; service layer mocks
  tests/unit/test_*.py       ← individual test files

Fixture categories
──────────────────
  Layer-2 GNN mocks    Pretrained checkpoint stubs, inference pipeline mocks
  Layer-3 Guardian     RL agent, Ghost Preview, OPA, K8s executor mocks
  Layer-4 Co-Pilot     Anthropic client, session store, context builder mocks
  API Gateway          FastAPI TestClient, JWT helper, rate-limit bypass
  Shared               tmp directories, clock manipulation helpers
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, NodeFeatures, NodeType,
    TopologyNode, TopologyEdge, GnnInferenceResult, TopologySnapshot,
    CausalChainNode, TopFeature, CounterfactualResult,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    ScaleParameters, RollbackParameters, GhostSimulationResult,
    ActionRequest, ActionResult, ActionHistoryEntry,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, MessageType, SessionState, ToolName, StreamEventType,
    TokenUsage, ToolCall, ToolResult, ChatMessage, SessionContext,
    StreamEvent, IncidentReport, FinetuningExample,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _uid() -> str:
    return str(uuid.uuid4())


def _ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Layer-2 — GNN mocks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_torch():
    """Patch torch so GNN model tests run without GPU or torch install."""
    with patch.dict("sys.modules", {
        "torch":                     MagicMock(),
        "torch.nn":                  MagicMock(),
        "torch.nn.functional":       MagicMock(),
        "torch_geometric":           MagicMock(),
        "torch_geometric.nn":        MagicMock(),
        "torch_geometric.utils":     MagicMock(),
        "torch_geometric.data":      MagicMock(),
    }):
        yield


@pytest.fixture
def gnn_inference_healthy() -> GnnInferenceResult:
    """Healthy heartbeat GNN inference — no incident, all nodes green."""
    return GnnInferenceResult(
        inference_id=_uid(),
        incident_type=IncidentType.INCIDENT_NONE,
        graph_confidence=0.0,
        is_heartbeat=True,
        node_count=8,
        edge_count=12,
        inference_latency_ms=22.4,
    )


@pytest.fixture
def gnn_inference_fault() -> GnnInferenceResult:
    """Active FAULT inference — OOM cascade on payment-svc."""
    nodes = [
        TopologyNode(
            node_id="svc-payment", name="payment-svc",
            node_class=NodeClass.NODE_CLASS_FAULT,
            class_confidence=0.91,
            is_root_cause=True,
            features=NodeFeatures(
                oom_kill_rate=0.85,
                cpu_utilization=0.92,
                restart_count=5,
                memory_utilization=0.97,
            ),
        ),
        TopologyNode(
            node_id="svc-api",   name="api-gateway",
            node_class=NodeClass.NODE_CLASS_HEALTHY,
            class_confidence=0.88,
        ),
        TopologyNode(
            node_id="svc-db",    name="postgres-primary",
            node_class=NodeClass.NODE_CLASS_HEALTHY,
            class_confidence=0.94,
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
        node_count=3,
        edge_count=4,
        inference_latency_ms=38.7,
        causal_chain=[
            CausalChainNode(
                node_id="svc-payment", node_name="payment-svc",
                causal_score=0.91, explanation="OOM rate 0.85/s — 17× baseline",
                depth=0,
            ),
        ],
        top_features=[
            TopFeature(
                feature_name="oom_kill_rate", node_name="payment-svc",
                importance=0.94, value=0.85, threshold=0.05,
                explanation="OOM kills 17× baseline — likely memory leak",
            ),
        ],
    )


@pytest.fixture
def gnn_inference_attack() -> GnnInferenceResult:
    """Active ATTACK inference — lateral movement from auth-svc."""
    nodes = [
        TopologyNode(
            node_id="svc-auth",     name="auth-svc",
            node_class=NodeClass.NODE_CLASS_ATTACK,
            class_confidence=0.93,
            is_root_cause=True,
            features=NodeFeatures(
                capability_event_rate=1.0,
                syscall_anomaly_score=0.91,
                sensitive_file_rate=0.78,
            ),
        ),
        TopologyNode(
            node_id="svc-secrets",  name="secrets-store",
            node_class=NodeClass.NODE_CLASS_ATTACK,
            class_confidence=0.72,
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
        node_count=2,
        edge_count=3,
        inference_latency_ms=41.2,
    )


@pytest.fixture
def mock_gnn_http_client(gnn_inference_fault):
    """AsyncMock httpx.AsyncClient that returns fault inference from /infer."""
    client = AsyncMock()
    client.post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "inference_id":        gnn_inference_fault.inference_id,
            "incident_type":       "FAULT",
            "graph_confidence":    0.91,
            "root_cause_node_name": "payment-svc",
            "blast_radius_count":  1,
            "inference_latency_ms": 38.7,
            "is_heartbeat":        False,
        }),
    )
    client.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"nodes": [], "edges": []}),
    )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    return client


# ══════════════════════════════════════════════════════════════════════════════
# Layer-3 — Guardian mocks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def ghost_approved() -> GhostSimulationResult:
    """Ghost Preview that passes all risk gates."""
    return GhostSimulationResult(
        risk_score=0.12,
        risk_category=RiskCategory.RISK_VERY_LOW,
        confidence=0.92,
        mttr_delta_seconds=-180.0,
        traffic_impact_pct=5.0,
        availability_impact=0.02,
        affected_pod_count=1,
        opa_approved=True,
        dry_run_succeeded=True,
        recommended_action="restart_pod",
        recommendation_reason="OOM rate will drop to baseline after restart",
        projected_status="healthy",
        sim_duration_ms=87.3,
    )


@pytest.fixture
def ghost_denied_high_risk() -> GhostSimulationResult:
    """Ghost Preview that FAILS risk gate — too high risk."""
    return GhostSimulationResult(
        risk_score=0.82,
        risk_category=RiskCategory.RISK_VERY_HIGH,
        confidence=0.60,
        traffic_impact_pct=65.0,
        availability_impact=0.40,
        affected_pod_count=12,
        opa_approved=False,
        opa_violations=["policy.blast_radius_exceeds_threshold"],
        dry_run_succeeded=False,
        dry_run_error="Would affect >50% of pods in namespace",
        recommended_action="no_op",
        recommendation_reason="Risk score 0.82 exceeds threshold 0.35",
        sim_duration_ms=91.1,
    )


@pytest.fixture
def action_request_restart(gnn_inference_fault, ghost_approved) -> ActionRequest:
    """Fully populated ActionRequest for a restart_pod action."""
    return ActionRequest(
        request_id=_uid(),
        action_name=ActionName.ACTION_RESTART_POD,
        action_label="restart_pod",
        target_node_id="svc-payment",
        target_node_name="payment-svc-pod-7f9d8b-xkv2p",
        target_namespace="production",
        target_resource="pod/payment-svc-pod-7f9d8b-xkv2p",
        inference_id=gnn_inference_fault.inference_id,
        trigger_class=NodeClass.NODE_CLASS_FAULT,
        trigger_confidence=0.91,
        root_cause_node="payment-svc",
        rl_q_value=0.87,
        autonomy_mode=AutonomyMode.AUTONOMY_SUPERVISED,
        ghost_result=ghost_approved,
    )


@pytest.fixture
def action_result_success(action_request_restart) -> ActionResult:
    """Successful ActionResult after restart_pod executes."""
    return ActionResult(
        request=action_request_restart,
        status=ActionStatus.STATUS_SUCCEEDED,
        message="Pod payment-svc-pod-7f9d8b-xkv2p deleted; replacement scheduled",
        executed_at=_ts(),
        completed_at=_ts(),
        execution_duration_ms=1243.0,
        k8s_resource_version="42891",
        verified_effect=True,
        post_action_health=0.94,
        verification_note="OOM rate dropped from 0.85/s to 0.02/s after 60s",
        was_rolled_back=False,
        autonomy_mode=AutonomyMode.AUTONOMY_SUPERVISED,
        incident_id=_uid(),
    )


@pytest.fixture
def mock_opa_client():
    """Async HTTP client mock that always returns OPA allow=True."""
    client = AsyncMock()
    client.post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "result": {
                "allow": True,
                "violations": [],
                "policy_version": "1.0.0",
            }
        }),
    )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    return client


@pytest.fixture
def mock_k8s_client():
    """Kubernetes API client stub — all operations succeed."""
    client = MagicMock()
    # Pod operations
    client.delete_namespaced_pod.return_value = MagicMock(
        metadata=MagicMock(resource_version="42891")
    )
    client.list_namespaced_pod.return_value = MagicMock(items=[])
    # Deployment operations
    client.patch_namespaced_deployment.return_value = MagicMock(
        metadata=MagicMock(resource_version="42892")
    )
    client.read_namespaced_deployment.return_value = MagicMock(
        spec=MagicMock(replicas=3),
        metadata=MagicMock(resource_version="42890"),
    )
    # Apps v1
    client.apps_v1 = MagicMock()
    client.apps_v1.read_namespaced_deployment = client.read_namespaced_deployment
    client.apps_v1.patch_namespaced_deployment = client.patch_namespaced_deployment
    return client


@pytest.fixture
def mock_rl_agent():
    """Stubbed RL agent that deterministically returns restart_pod (action 5)."""
    agent = MagicMock()
    agent.predict.return_value = (5, None)   # action_id=5 = restart_pod, state=None
    agent.q_values = MagicMock(return_value=[
        0.1, 0.2, 0.1, 0.1, 0.2, 0.87, 0.3, 0.2,
        0.1, 0.1, 0.1, 0.3, 0.2, 0.1, 0.05,
    ])
    return agent


# ══════════════════════════════════════════════════════════════════════════════
# Layer-4 — Co-Pilot mocks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def claude_session() -> SessionContext:
    """Fresh Claude session for alice, pre-populated with 2 turns."""
    session = SessionContext(
        session_id=_uid(),
        operator_id="alice",
        operator_name="Alice Smith",
    )
    session.add_message(ChatMessage(
        role=MessageRole.ROLE_USER,
        content="What is wrong with payment-svc?",
    ))
    session.add_message(ChatMessage(
        role=MessageRole.ROLE_ASSISTANT,
        content=(
            "Payment-svc is experiencing an OOM cascade. The GNN reports "
            "FAULT confidence 91%. Root cause: memory leak causing 17× OOM rate."
        ),
        token_usage=TokenUsage(input_tokens=800, output_tokens=120),
    ))
    return session


@pytest.fixture
def mock_anthropic_client():
    """Mock Anthropic client for sync + streaming responses."""
    client = MagicMock()

    # Non-streaming response
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = (
        "Root cause analysis: payment-svc memory leak detected. "
        "Recommend restart_pod to clear leaked memory."
    )
    mock_msg = MagicMock()
    mock_msg.content = [content_block]
    mock_msg.stop_reason = "end_turn"
    mock_msg.usage = MagicMock(
        input_tokens=1200, output_tokens=180,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    client.messages.create.return_value = mock_msg

    # Streaming context manager
    delta_block = MagicMock()
    delta_block.type = "content_block_delta"
    delta_block.delta = MagicMock(text="Root cause: memory leak.")

    message_done = MagicMock()
    message_done.type = "message_stop"
    message_done.message = mock_msg

    stream_ctx = MagicMock()
    stream_ctx.__enter__ = MagicMock(return_value=iter([delta_block, message_done]))
    stream_ctx.__exit__  = MagicMock(return_value=False)
    client.messages.stream.return_value = stream_ctx

    return client


@pytest.fixture
def mock_context_builder(gnn_inference_fault):
    """ClusterContextBuilder that returns a prebuilt context string."""
    builder = AsyncMock()
    builder.build.return_value = f"""
[INCIDENT OVERVIEW]
Type:         FAULT
Severity:     HIGH
Root cause:   payment-svc (FAULT 91%)
Blast radius: 1 node
Elapsed:      00:02:15

[CAUSAL GNN — confidence 91%]
● payment-svc  FAULT  91%  ★ ROOT CAUSE
  Top feature: oom_kill_rate=0.85 (17× baseline)
● api-gateway  HEALTHY 88%

[GUARDIAN STATUS]
Autonomy mode: supervised
Pending actions: 0
Recent actions:  0
""".strip()
    builder.build_incident_report.return_value = "Incident report: OOM cascade on payment-svc"
    return builder


# ══════════════════════════════════════════════════════════════════════════════
# API Gateway mocks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def valid_jwt_token() -> str:
    """A syntactically valid but unsigned JWT for testing auth middleware."""
    import base64
    header  = base64.urlsafe_b64encode(
        b'{"alg":"HS256","typ":"JWT"}'
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        b'{"sub":"alice","exp":9999999999,"iat":1700000000,"role":"operator"}'
    ).rstrip(b"=").decode()
    sig     = "TESTSIGNATURE"
    return f"{header}.{payload}.{sig}"


@pytest.fixture
def mock_upstream_services(mock_gnn_http_client, mock_opa_client):
    """Combined upstream mock covering GNN, Guardian, and Layer-1 endpoints."""
    guardian_client = AsyncMock()
    guardian_client.post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "audit_id": str(uuid.uuid4()),
            "status":   "SUCCEEDED",
            "message":  "restart_pod executed",
        }),
    )
    guardian_client.__aenter__ = AsyncMock(return_value=guardian_client)
    guardian_client.__aexit__  = AsyncMock(return_value=False)
    return {
        "gnn":      mock_gnn_http_client,
        "guardian": guardian_client,
        "opa":      mock_opa_client,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_checkpoint_dir(tmp_path) -> Path:
    """Temp directory pre-populated with stub model checkpoint files."""
    ckpt = tmp_path / "checkpoints"
    ckpt.mkdir()
    # Create stub files so existence checks pass
    (ckpt / "guardian_ppo_final.zip").write_bytes(b"\x50\x4b\x03\x04" + b"\x00" * 256)
    (ckpt / "guardian_ppo_final_vecnormalize.pkl").write_bytes(b"\x80\x05" + b"\x00" * 64)
    (ckpt / "gnn_checkpoint.pt").write_bytes(b"\x80\x02" + b"\x00" * 128)
    return ckpt


@pytest.fixture
def freeze_time(monkeypatch):
    """
    Freeze time.monotonic() and time.time() to a fixed value.

    Usage:
        def test_ttl(freeze_time):
            freeze_time(1000.0)
            # ... assertions ...
            freeze_time(1010.0)   # advance by 10s
    """
    _t = [1_700_000_000.0]

    monkeypatch.setattr("time.monotonic", lambda: _t[0])
    monkeypatch.setattr("time.time",      lambda: _t[0])

    def _advance(new_t: float) -> None:
        _t[0] = new_t

    return _advance


@pytest.fixture
def env_unit(monkeypatch):
    """
    Environment variables suitable for unit testing — no real service URLs,
    short timeouts, debug logging.
    """
    env_vars = {
        "LOG_LEVEL":                  "DEBUG",
        "AUTONOMY_MODE":              "supervised",
        "GNN_SERVICE_URL":            "http://mock-gnn:8001",
        "GUARDIAN_SERVICE_URL":       "http://mock-guardian:8002",
        "EBPF_SERVICE_URL":           "http://mock-layer1:9100",
        "OPA_URL":                    "http://mock-opa:8181",
        "KAFKA_BOOTSTRAP_SERVERS":    "mock-kafka:9092",
        "ANTHROPIC_API_KEY":          "sk-ant-unit-test-key",
        "JWT_SECRET":                 "unit-test-jwt-secret-32-chars-xx",
        "GHOST_RISK_THRESHOLD":       "0.35",
        "GHOST_CONFIDENCE_MIN":       "0.70",
        "AUTO_REPORT_CONFIDENCE":     "0.85",
        "K8S_NAMESPACE":              "test-namespace",
        "SERVICE_NAME":               "unit-test",
    }
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    return env_vars
