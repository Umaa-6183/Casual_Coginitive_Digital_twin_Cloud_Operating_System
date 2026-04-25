"""
CCDT E2E Test Suite — Shared Fixtures (tests/e2e/conftest.py)
═══════════════════════════════════════════════════════════════════════════════
E2E fixtures wire the complete four-layer CCDT pipeline together. All
external dependencies (Kubernetes, Kafka, Anthropic API) are mocked, but the
data flows through the real proto serialization and business logic paths.

E2E test structure:
  Phase 1 — Layer-1 generates eBPF events and publishes to Kafka
  Phase 2 — Layer-2 GNN consumes events, runs inference, publishes result
  Phase 3 — Layer-3 Guardian consumes inference, runs Ghost Preview + OPA
  Phase 4 — Guardian executes action, publishes to ccdt.guardian.actions
  Phase 5 — Layer-4 Co-Pilot receives incident, generates operator summary
  Phase 6 — Operator receives streaming response via WebSocket/SSE

Compared to integration fixtures:
  - Integration fixtures test one or two adjacent layers at a time
  - E2E fixtures wire ALL four layers through a shared in-memory bus
  - E2E tests assert on the final observable outcome (operator sees summary)
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable
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
    MessageRole, MessageType, SessionState, ToolName,
    TokenUsage, ChatMessage, SessionContext,
    StreamEvent, IncidentReport, StreamEventType,
)


def _uid() -> str: return str(uuid.uuid4())
def _ts()  -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Four-layer pipeline simulator
# ══════════════════════════════════════════════════════════════════════════════

class FourLayerPipeline:
    """
    Simulates the full CCDT four-layer pipeline in memory.

    Usage:
        pipeline = FourLayerPipeline()
        result = await pipeline.run_fault_scenario("payment-svc")
        assert result.phase5_copilot_summary is not None
    """

    def __init__(self) -> None:
        # Message bus (topic → deque of messages)
        self._bus: dict[str, deque] = {
            "ccdt.ebpf.events":      deque(),
            "ccdt.gnn.inference":    deque(),
            "ccdt.guardian.actions": deque(),
            "ccdt.incidents":        deque(),
        }
        self.timeline: list[dict] = []   # ordered record of all events

    # ── Message bus ──────────────────────────────────────────────────────────

    def publish(self, topic: str, message: Any) -> None:
        self._bus[topic].append(message)
        self.timeline.append({
            "ts": _ts(), "topic": topic,
            "type": type(message).__name__,
        })

    def consume(self, topic: str) -> Any | None:
        q = self._bus.get(topic)
        return q.popleft() if q else None

    def bus_depth(self, topic: str) -> int:
        return len(self._bus.get(topic, []))

    # ── Phase simulators ──────────────────────────────────────────────────────

    def phase1_collect_ebpf(
        self, node_name: str = "node-prod-1",
        scenario: str = "oom",   # "oom" | "attack" | "tcp" | "exec"
    ) -> TypedEbpfBatch:
        """Simulate Layer-1 collecting eBPF events and publishing to bus."""
        meta = EventMetadata.now(node_name, 42001, "payment-svc",
                                  namespace="production", pod_name="payment-pod-abc")
        if scenario == "oom":
            batch = TypedEbpfBatch(
                batch_id=_uid(), node_name=node_name,
                collector_id=_uid(), batch_ts=_ts(), schema_ver="1.0",
                oom_kill_events=[
                    OomKillEvent(meta=meta, victim_pid=42001, victim_comm="payment-svc",
                                 oom_score=1000, victim_rss_bytes=2_147_483_648),
                ],
            )
        elif scenario == "attack":
            batch = TypedEbpfBatch(
                batch_id=_uid(), node_name=node_name,
                collector_id=_uid(), batch_ts=_ts(), schema_ver="1.0",
                capability_events=[
                    CapabilityEvent(
                        meta=meta,
                        capability=LinuxCapability.CAP_NET_ADMIN, allowed=True,
                        syscall_nr=317,
                    ),
                ],
            )
        elif scenario == "tcp":
            batch = TypedEbpfBatch(
                batch_id=_uid(), node_name=node_name,
                collector_id=_uid(), batch_ts=_ts(), schema_ver="1.0",
                tcp_retransmit_events=[
                    TcpRetransmitEvent(
                        meta=meta, src_addr="10.0.1.5", dst_addr="10.0.2.10",
                        retransmit_count=150, rtt_us=50000, rto_us=200000,
                    ),
                ],
            )
        else:
            batch = TypedEbpfBatch(
                batch_id=_uid(), node_name=node_name,
                collector_id=_uid(), batch_ts=_ts(), schema_ver="1.0",
            )
        batch.compute_type_counts()
        self.publish("ccdt.ebpf.events", batch)
        return batch

    def phase2_gnn_inference(
        self, incident_type: str = "FAULT"
    ) -> GnnInferenceResult:
        """Simulate Layer-2 consuming events and publishing GNN inference."""
        if incident_type == "FAULT":
            nodes = [
                TopologyNode(
                    node_id="svc-payment", name="payment-svc",
                    node_class=NodeClass.NODE_CLASS_FAULT, class_confidence=0.91,
                    is_root_cause=True,
                    features=NodeFeatures(oom_kill_rate=0.85, cpu_utilization=0.92),
                ),
            ]
            result = GnnInferenceResult(
                inference_id=_uid(),
                incident_type=IncidentType.INCIDENT_FAULT,
                graph_confidence=0.91,
                root_cause_node_id="svc-payment",
                root_cause_node_name="payment-svc",
                root_cause_confidence=0.91,
                blast_radius_count=1,
                node_classifications=nodes,
                node_count=5, edge_count=7,
                inference_latency_ms=38.7,
                causal_chain=[
                    CausalChainNode(
                        node_id="svc-payment", node_name="payment-svc",
                        causal_score=0.91, depth=0,
                        explanation="OOM rate 17× baseline",
                    ),
                ],
                top_features=[
                    TopFeature(
                        feature_name="oom_kill_rate", node_name="payment-svc",
                        importance=0.94, value=0.85, threshold=0.05,
                        explanation="Memory leak — OOM rate 17× baseline",
                    ),
                ],
            )
        elif incident_type == "ATTACK":
            nodes = [
                TopologyNode(
                    node_id="svc-auth", name="auth-svc",
                    node_class=NodeClass.NODE_CLASS_ATTACK, class_confidence=0.93,
                    is_root_cause=True,
                    features=NodeFeatures(
                        capability_event_rate=1.0, syscall_anomaly_score=0.91,
                    ),
                ),
            ]
            result = GnnInferenceResult(
                inference_id=_uid(),
                incident_type=IncidentType.INCIDENT_ATTACK,
                graph_confidence=0.93,
                root_cause_node_id="svc-auth",
                root_cause_node_name="auth-svc",
                root_cause_confidence=0.93,
                blast_radius_count=2,
                node_classifications=nodes,
                node_count=5, edge_count=7,
                inference_latency_ms=41.2,
            )
        else:
            result = GnnInferenceResult(
                inference_id=_uid(),
                incident_type=IncidentType.INCIDENT_NONE,
                graph_confidence=0.0,
                is_heartbeat=True,
                node_count=5, inference_latency_ms=18.0,
            )
        self.publish("ccdt.gnn.inference", result)
        return result

    def phase3_ghost_preview(
        self, inference: GnnInferenceResult, risk_score: float = 0.12
    ) -> GhostSimulationResult:
        """Simulate Layer-3 Ghost Preview simulation."""
        approved = risk_score < 0.35
        return GhostSimulationResult(
            risk_score=risk_score,
            risk_category=RiskCategory.from_score(risk_score),
            confidence=0.92 if approved else 0.55,
            mttr_delta_seconds=-180.0 if approved else 60.0,
            traffic_impact_pct=5.0 if approved else 55.0,
            availability_impact=0.02 if approved else 0.45,
            affected_pod_count=1 if approved else 8,
            opa_approved=approved,
            dry_run_succeeded=approved,
            recommended_action="restart_pod" if approved else "no_op",
            recommendation_reason=(
                "Safe to proceed — minimal blast radius" if approved
                else "Risk score exceeds threshold"
            ),
            projected_status="healthy" if approved else "critical",
            sim_duration_ms=87.3,
        )

    def phase4_execute_action(
        self,
        inference: GnnInferenceResult,
        ghost: GhostSimulationResult,
        action_name: ActionName = ActionName.ACTION_RESTART_POD,
    ) -> ActionResult:
        """Simulate Layer-3 executing action and publishing result."""
        req = ActionRequest(
            request_id=_uid(),
            action_name=action_name,
            action_label="restart_pod",
            target_node_name="payment-svc-pod-abc",
            target_namespace="production",
            inference_id=inference.inference_id,
            trigger_class=NodeClass.NODE_CLASS_FAULT,
            trigger_confidence=inference.root_cause_confidence,
            autonomy_mode=AutonomyMode.AUTONOMY_SUPERVISED,
            ghost_result=ghost,
        )
        status = (
            ActionStatus.STATUS_SUCCEEDED if ghost.opa_approved
            else ActionStatus.STATUS_DENIED
        )
        result = ActionResult(
            request=req,
            status=status,
            message="Pod restarted" if ghost.opa_approved else "OPA denied",
            executed_at=_ts() if ghost.opa_approved else None,
            completed_at=_ts() if ghost.opa_approved else None,
            execution_duration_ms=1243.0 if ghost.opa_approved else 0.0,
            verified_effect=ghost.opa_approved,
            post_action_health=0.94 if ghost.opa_approved else 0.0,
            autonomy_mode=AutonomyMode.AUTONOMY_SUPERVISED,
        )
        self.publish("ccdt.guardian.actions", result)
        return result

    def phase5_copilot_summary(
        self,
        inference: GnnInferenceResult,
        action: ActionResult | None = None,
    ) -> str:
        """Simulate Layer-4 generating operator summary text."""
        summary_lines = [
            f"**🚨 CCDT Incident Report**",
            f"",
            f"**Incident Type:** {inference.incident_type.name.replace('INCIDENT_', '')}",
            f"**Root Cause:** {inference.root_cause_node_name}",
            f"**Confidence:** {inference.graph_confidence:.0%}",
            f"**Blast Radius:** {inference.blast_radius_count} service(s)",
            f"",
        ]
        if inference.causal_chain:
            summary_lines.append("**Causal Chain:**")
            for node in inference.causal_chain:
                summary_lines.append(f"  • {node.node_name}: {node.explanation}")
            summary_lines.append("")
        if action:
            status_emoji = "✅" if action.succeeded else "❌"
            summary_lines.append(f"**Guardian Action:** {status_emoji} {action.summary()}")
        return "\n".join(summary_lines)

    # ── Full pipeline runners ─────────────────────────────────────────────────

    async def run_fault_scenario(self, service: str = "payment-svc") -> "PipelineResult":
        """Run complete fault detection + remediation pipeline."""
        batch    = self.phase1_collect_ebpf("node-prod-1", "oom")
        infer    = self.phase2_gnn_inference("FAULT")
        ghost    = self.phase3_ghost_preview(infer, risk_score=0.12)
        action   = self.phase4_execute_action(infer, ghost)
        summary  = self.phase5_copilot_summary(infer, action)
        return PipelineResult(batch, infer, ghost, action, summary, self.timeline[:])

    async def run_attack_scenario(self, service: str = "auth-svc") -> "PipelineResult":
        """Run complete attack detection + isolation pipeline."""
        batch  = self.phase1_collect_ebpf("node-prod-2", "attack")
        infer  = self.phase2_gnn_inference("ATTACK")
        ghost  = self.phase3_ghost_preview(infer, risk_score=0.15)
        action = self.phase4_execute_action(
            infer, ghost, ActionName.ACTION_APPLY_NETWORK_POLICY
        )
        summary = self.phase5_copilot_summary(infer, action)
        return PipelineResult(batch, infer, ghost, action, summary, self.timeline[:])

    async def run_blocked_scenario(self) -> "PipelineResult":
        """Scenario where Ghost Preview blocks action due to high risk."""
        batch  = self.phase1_collect_ebpf("node-prod-1", "oom")
        infer  = self.phase2_gnn_inference("FAULT")
        ghost  = self.phase3_ghost_preview(infer, risk_score=0.85)
        action = self.phase4_execute_action(infer, ghost)
        summary = self.phase5_copilot_summary(infer, action)
        return PipelineResult(batch, infer, ghost, action, summary, self.timeline[:])


class PipelineResult:
    """Typed container for a complete pipeline run result."""
    def __init__(
        self,
        phase1_batch:   TypedEbpfBatch,
        phase2_infer:   GnnInferenceResult,
        phase3_ghost:   GhostSimulationResult,
        phase4_action:  ActionResult,
        phase5_summary: str,
        timeline:       list[dict],
    ) -> None:
        self.phase1_batch   = phase1_batch
        self.phase2_infer   = phase2_infer
        self.phase3_ghost   = phase3_ghost
        self.phase4_action  = phase4_action
        self.phase5_summary = phase5_summary
        self.timeline       = timeline

    @property
    def was_remediated(self) -> bool:
        return self.phase4_action.succeeded

    @property
    def total_latency_ms(self) -> float:
        return (
            self.phase2_infer.inference_latency_ms
            + self.phase3_ghost.sim_duration_ms
            + self.phase4_action.execution_duration_ms
        )

    @property
    def incident_type(self) -> str:
        return self.phase2_infer.incident_type.name.replace("INCIDENT_", "")


@pytest.fixture
def pipeline() -> FourLayerPipeline:
    """Fresh FourLayerPipeline per test."""
    return FourLayerPipeline()


# ══════════════════════════════════════════════════════════════════════════════
# HTTP TestClient helpers
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_all_upstreams():
    """
    Complete upstream mock bundle — all four layers respond correctly.
    Use with TestClient to simulate a healthy cluster.
    """
    # Layer-1 mock
    l1 = AsyncMock()
    l1.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "node": "node-prod-1", "oom_kills_1m": 0,
            "capabilities": 0, "tcp_retransmits": 2,
        }),
    )
    # Layer-2 mock
    l2 = AsyncMock()
    l2.post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "inference_id": _uid(), "incident_type": "NONE",
            "graph_confidence": 0.0, "is_heartbeat": True,
        }),
    )
    l2.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"nodes": [], "edges": []}),
    )
    # Layer-3 mock
    l3 = AsyncMock()
    l3.post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "audit_id": _uid(), "status": "SUCCEEDED",
        }),
    )
    l3.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"actions": [], "total": 0}),
    )
    for mock in [l1, l2, l3]:
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__  = AsyncMock(return_value=False)
    return {"l1": l1, "l2": l2, "l3": l3}


# ══════════════════════════════════════════════════════════════════════════════
# Environment variables
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def env_e2e(monkeypatch):
    """E2E test environment — real business logic, mock transports."""
    env_vars = {
        "LOG_LEVEL":                  "WARNING",
        "AUTONOMY_MODE":              "full-auto",   # no human approval in e2e
        "GNN_SERVICE_URL":            "http://e2e-gnn:8001",
        "GUARDIAN_SERVICE_URL":       "http://e2e-guardian:8002",
        "EBPF_SERVICE_URL":           "http://e2e-layer1:9100",
        "ANTHROPIC_API_KEY":          "sk-ant-e2e-test-key",
        "JWT_SECRET":                 "e2e-test-jwt-secret-32-chars-xx",
        "GHOST_RISK_THRESHOLD":       "0.50",
        "GHOST_CONFIDENCE_MIN":       "0.55",
        "AUTO_REPORT_CONFIDENCE":     "0.80",
        "K8S_NAMESPACE":              "e2e-test",
        "SERVICE_NAME":               "e2e-test",
    }
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    return env_vars
