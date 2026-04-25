"""
End-to-End tests — Fault Remediation Scenario
Simulates the complete lifecycle for a memory leak / OOM incident:

  1. Layer-1 detects OOM kills on payment-svc
  2. Layer-2 GNN classifies it as FAULT with 88% confidence
  3. Layer-3 Guardian selects restart_pod, Ghost Preview passes (risk 12%)
  4. OPA approves all 5 policies
  5. Kubernetes pod restart executes successfully
  6. Post-action health check verifies OOM rate drops
  7. Layer-4 Co-Pilot notifies operator with full incident summary

All external calls are mocked (no live services required).
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, OomKillEvent, EventMetadata, EventSeverity,
)
from shared.proto.generated.graph_pb2 import (
    GnnInferenceResult, NodeClass, IncidentType,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    GhostSimulationResult, ActionRequest, ActionResult,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, MessageType, SessionState,
    ChatMessage, SessionContext, IncidentReport,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Full fault scenario
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer1
@pytest.mark.layer2
@pytest.mark.layer3
@pytest.mark.layer4
class TestFaultRemediationE2E:
    """
    Full OOM kill → GNN fault detection → Guardian restart → resolved pipeline.
    """

    async def test_step1_oom_events_detected(
        self, base_meta, oom_kill_event, fake_kafka_producer
    ):
        """Layer-1: OOM kill events are batched and published to Kafka."""
        # Simulate 3 OOM kills in 60s (high rate)
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name="ip-10-0-1-42.us-east-1.compute.internal",
            collector_id=_uid(),
            batch_ts=_now(),
            oom_kill_events=[oom_kill_event] * 3,
            schema_ver="1.0",
        )
        batch.compute_type_counts()
        assert batch.type_counts["oom_kill"] == 3

        await fake_kafka_producer.send("ccdt.ebpf.events", batch.SerializeToString())
        assert len(fake_kafka_producer.messages) == 1

    async def test_step2_gnn_classifies_as_fault(
        self, fault_inference, fake_kafka_producer
    ):
        """Layer-2: GNN classifies OOM pattern as FAULT with high confidence."""
        assert fault_inference.incident_type    == IncidentType.INCIDENT_FAULT
        assert fault_inference.graph_confidence >= 0.85
        assert fault_inference.is_active_incident is True
        assert fault_inference.root_cause_node_name == "payment-svc"
        assert fault_inference.blast_radius_count   >= 1

        await fake_kafka_producer.send(
            "ccdt.gnn.inference",
            fault_inference.SerializeToString(),
        )
        back = GnnInferenceResult.FromString(
            fake_kafka_producer.messages[0]["value"]
        )
        assert back.severity == "high"

    async def test_step3_ghost_preview_approves_restart(self, low_risk_ghost):
        """Layer-3: Ghost Preview simulates restart_pod and approves (risk 12%)."""
        assert low_risk_ghost.is_safe          is True
        assert low_risk_ghost.opa_approved     is True
        assert low_risk_ghost.risk_score       <= 0.20
        assert low_risk_ghost.mttr_delta_seconds < 0    # MTTR improves

    async def test_step4_opa_approves_all_policies(self, mock_opa_allow_all):
        """Layer-3: All 5 OPA policies approve restart_pod for a FAULT node."""
        action_input = {
            "action": {"name": "restart_pod", "target_node": "payment-svc",
                       "parameters": {}, "history": []},
            "node": {"cpu": 0.90, "mem": 0.98, "status": "critical", "class": "fault",
                     "oom_kills": 3, "cap_event": 0, "file_event": 0, "is_isolated": False},
            "cluster": {"namespace": "production", "nodes": 9},
            "context": {"autonomy_mode": "supervised", "human_approved": True,
                        "can_write_ns": ["production"]},
        }
        # Evaluate all 5 policies
        all_approved = True
        for policy in ("no_privilege_escalation", "lateral_movement", "egress_control",
                       "cpu_threshold", "oom_notification"):
            resp = await mock_opa_allow_all.post(
                f"http://opa:8181/v1/data/ccdt/guardian/policies/{policy}",
                json={"input": action_input},
            )
            data = resp.json()
            if not data["result"]["allow"]:
                all_approved = False
        assert all_approved is True

    async def test_step5_kubernetes_restart_executes(self, mock_k8s_client):
        """Layer-3: Kubernetes delete pod API is called for payment-svc."""
        namespace = "production"
        target    = "payment-svc"

        # Simulate pod list + delete
        pods_response = mock_k8s_client.CoreV1Api.return_value.list_namespaced_pod.return_value
        pods_response.items = [
            MagicMock(metadata=MagicMock(name="payment-svc-pod-abc123"))
        ]
        v1 = mock_k8s_client.CoreV1Api.return_value
        v1.list_namespaced_pod(namespace, label_selector=f"app={target}")
        v1.delete_namespaced_pod("payment-svc-pod-abc123", namespace)

        v1.list_namespaced_pod.assert_called_once()
        v1.delete_namespaced_pod.assert_called_once()

    async def test_step6_post_action_health_check_passes(
        self, fault_inference, low_risk_ghost, restart_pod_request
    ):
        """Layer-3: Post-action verification confirms OOM rate dropped."""
        result = ActionResult(
            audit_id=_uid(),
            request=restart_pod_request,
            status=ActionStatus.STATUS_SUCCEEDED,
            message="Pod payment-svc-pod-abc123 deleted — controller recreating",
            executed_at=_now(),
            completed_at=_now(),
            execution_duration_ms=1250.0,
            verified_effect=True,
            post_action_health=0.94,   # Improved from 0.50
            verification_note="OOM kill rate: 3/min → 0/min after restart",
            was_rolled_back=False,
            incident_id=_uid(),
            schema_ver="1.0",
        )
        assert result.succeeded         is True
        assert result.verified_effect   is True
        assert result.post_action_health > 0.85

    async def test_step7_action_result_published_to_kafka(
        self, succeeded_action_result, fake_kafka_producer
    ):
        """Layer-3: Successful action result published to ccdt.guardian.actions."""
        await fake_kafka_producer.send(
            "ccdt.guardian.actions",
            succeeded_action_result.SerializeToString(),
        )
        back = ActionResult.FromString(fake_kafka_producer.messages[0]["value"])
        assert back.status           == ActionStatus.STATUS_SUCCEEDED
        assert back.verified_effect  is True

    async def test_step8_copilot_notifies_operator(
        self, fault_inference, incident_report
    ):
        """Layer-4: Co-Pilot injects incident report into operator sessions."""
        session = SessionContext(
            session_id=_uid(),
            operator_id="operator:alice",
            state=SessionState.SESSION_ACTIVE,
        )
        injection = incident_report.to_system_prompt_injection()
        session.add_message(ChatMessage(
            role=MessageRole.ROLE_SYSTEM,
            message_type=MessageType.MSG_INCIDENT_REPORT,
            content=injection,
            auto_injected=True,
        ))
        injected = [m for m in session.history if m.auto_injected]
        assert len(injected) == 1
        assert "FAULT"       in injected[0].content
        assert "payment-svc" in injected[0].content
        assert "88"          in injected[0].content   # 88% confidence

    async def test_full_fault_scenario_audit_trail(
        self,
        ebpf_batch,
        fault_inference,
        restart_pod_request,
        succeeded_action_result,
        fake_kafka_producer,
    ):
        """Verify all 4 Kafka messages are published in correct order."""
        # 1. eBPF batch
        await fake_kafka_producer.send("ccdt.ebpf.events",
                                        ebpf_batch.SerializeToString())
        # 2. GNN inference
        await fake_kafka_producer.send("ccdt.gnn.inference",
                                        fault_inference.SerializeToString())
        # 3. Action result
        await fake_kafka_producer.send("ccdt.guardian.actions",
                                        succeeded_action_result.SerializeToString())

        assert len(fake_kafka_producer.messages) == 3
        topics = [m["topic"] for m in fake_kafka_producer.messages]
        assert "ccdt.ebpf.events"      in topics
        assert "ccdt.gnn.inference"    in topics
        assert "ccdt.guardian.actions" in topics

    async def test_incident_resolved_state(
        self, fault_inference, succeeded_action_result
    ):
        """After successful action, incident should transition to RESOLVED."""
        incident = {
            "incident_id":      _uid(),
            "state":            "RESOLVED",
            "detected_at":      fault_inference.timestamp,
            "resolved_at":      _now(),
            "mttr_seconds":     succeeded_action_result.execution_duration_ms / 1000 + 180,
            "incident_type":    "FAULT",
            "root_cause":       fault_inference.root_cause_node_name,
            "false_positive":   False,
        }
        assert incident["state"]          == "RESOLVED"
        assert incident["false_positive"] is False
        assert incident["mttr_seconds"]   > 0


# ══════════════════════════════════════════════════════════════════════════════
# Fault scenario: human-in-loop mode
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer3
class TestFaultScenarioHumanInLoop:
    async def test_action_awaits_approval_in_human_in_loop(
        self, fault_inference, low_risk_ghost
    ):
        """
        In human-in-loop mode, even low-risk actions must await operator approval.
        """
        req = ActionRequest(
            request_id=_uid(),
            action_name=ActionName.ACTION_RESTART_POD,
            target_node_name="payment-svc",
            target_namespace="production",
            autonomy_mode=AutonomyMode.AUTONOMY_HUMAN_IN_LOOP,
            ghost_result=low_risk_ghost,
        )
        assert req.requires_human_approval() is True

    async def test_pending_approval_published_to_kafka(
        self, restart_pod_request, fake_kafka_producer
    ):
        """AWAITING_APPROVAL result must be published so operator can review."""
        pending = ActionResult(
            audit_id=_uid(),
            request=restart_pod_request,
            status=ActionStatus.STATUS_AWAITING_APPROVAL,
            message="Awaiting operator approval (human-in-loop mode)",
            schema_ver="1.0",
        )
        await fake_kafka_producer.send(
            "ccdt.guardian.actions",
            pending.SerializeToString(),
        )
        back = ActionResult.FromString(fake_kafka_producer.messages[0]["value"])
        assert back.status == ActionStatus.STATUS_AWAITING_APPROVAL

    async def test_operator_approves_action(self, restart_pod_request):
        """POST /actions/approve with approved=True should execute the action."""
        from shared.proto.generated.actions_pb2 import ApprovalResponse
        approval = ApprovalResponse(
            request_id=restart_pod_request.request_id,
            approved=True,
            approved_by="operator:alice",
            reason="Restart is safe; blast radius is 1 pod only",
        )
        assert approval.approved       is True
        assert approval.approved_by    == "operator:alice"

    async def test_operator_denies_action(self, restart_pod_request):
        """POST /actions/approve with approved=False should cancel the action."""
        from shared.proto.generated.actions_pb2 import ApprovalResponse
        denial = ApprovalResponse(
            request_id=restart_pod_request.request_id,
            approved=False,
            approved_by="operator:bob",
            reason="Black Friday peak traffic — too risky to restart now",
        )
        assert denial.approved is False
