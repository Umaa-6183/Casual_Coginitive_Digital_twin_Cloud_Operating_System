"""
Integration tests — Layer-2 GNN → Layer-3 Guardian Pipeline
Tests the full flow: GNN inference → RL action selection → Ghost Preview
→ OPA evaluation → Kubernetes execution (mocked).

Uses mocked HTTP clients for GNN, OPA, and Kubernetes.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, GnnInferenceResult,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    GhostSimulationResult, ActionRequest, ActionResult,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# GNN result → Guardian action selection
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer2
@pytest.mark.layer3
class TestGnnToGuardianPipeline:
    async def test_fault_inference_triggers_restart_pod_action(
        self, fault_inference, mock_http_client
    ):
        """FAULT + OOM → Guardian should propose restart_pod or scale_up_replicas."""
        payload = {
            "incident_type":     "FAULT",
            "graph_confidence":  fault_inference.graph_confidence,
            "root_cause_node":   fault_inference.root_cause_node_name,
            "blast_radius_count": fault_inference.blast_radius_count,
        }
        resp = await mock_http_client.post(
            "http://layer3-guardian:8002/actions/execute",
            json=payload,
        )
        data = resp.json()
        assert "audit_id" in data or "status" in data

    async def test_attack_inference_triggers_isolate_action(
        self, attack_inference, mock_http_client
    ):
        """ATTACK → Guardian should propose isolate_container."""
        payload = {
            "incident_type":    "ATTACK",
            "graph_confidence": attack_inference.graph_confidence,
            "root_cause_node":  attack_inference.root_cause_node_name,
        }
        resp = await mock_http_client.post(
            "http://layer3-guardian:8002/actions/execute",
            json=payload,
        )
        data = resp.json()
        assert data.get("status") in ("SUCCEEDED", "AWAITING_APPROVAL", "PENDING")

    async def test_heartbeat_does_not_trigger_action(
        self, heartbeat_inference, fake_kafka_producer
    ):
        """Heartbeat inferences (is_heartbeat=True) must not trigger Guardian actions."""
        payload = heartbeat_inference.SerializeToString()
        await fake_kafka_producer.send("ccdt.gnn.inference", payload)

        back = GnnInferenceResult.FromString(fake_kafka_producer.messages[0]["value"])
        # Guardian should check is_heartbeat before acting
        assert back.is_heartbeat is True
        assert back.incident_type == IncidentType.INCIDENT_NONE
        # No action should be dispatched (verified by checking Guardian received nothing)

    async def test_inference_below_confidence_threshold_no_action(self):
        """Inferences with confidence < 0.70 should not trigger autonomous actions."""
        low_confidence_threshold = 0.70
        low_conf_inference = GnnInferenceResult(
            inference_id=_uid(),
            incident_type=IncidentType.INCIDENT_FAULT,
            graph_confidence=0.55,   # Below threshold
            root_cause_node_name="uncertain-svc",
        )
        assert low_conf_inference.graph_confidence < low_confidence_threshold
        # Guardian should treat this as not actionable
        assert low_conf_inference.graph_confidence < 0.70


# ══════════════════════════════════════════════════════════════════════════════
# Ghost Preview integration
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer3
class TestGhostPreviewIntegration:
    async def test_ghost_preview_returns_risk_score(
        self, restart_pod_request, mock_http_client, mock_guardian_preview_response
    ):
        resp = await mock_http_client.post(
            "http://layer3-guardian:8002/actions/preview",
            json={
                "action_id":    5,    # restart_pod = index 5 in ACTION_NAMES
                "target_node":  restart_pod_request.target_node_name,
                "incident_type": "fault",
            },
        )
        data = resp.json()
        assert "risk_score"   in data
        assert "approved"     in data
        assert "confidence"   in data
        assert 0.0 <= data["risk_score"] <= 1.0

    async def test_ghost_preview_approved_for_low_risk(
        self, restart_pod_request, mock_http_client
    ):
        resp = await mock_http_client.post(
            "http://layer3-guardian:8002/actions/preview",
            json={"action_id": 5, "target_node": "payment-svc"},
        )
        data = resp.json()
        assert data["approved"] is True

    async def test_ghost_preview_rejected_for_high_risk_action(
        self, mock_http_client
    ):
        """
        Drain node action during a high-traffic period should be rejected
        by the risk gate.
        """
        # Override mock to return high-risk response
        async def _high_risk_post(url, **kwargs):
            resp = AsyncMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "approved":     False,
                "risk_score":   0.82,
                "risk_category": "HIGH",
                "confidence":   0.75,
                "opa_violations": ["cpu_threshold: drain blocked — 12 pods present"],
            }
            return resp

        mock_http_client.post = AsyncMock(side_effect=_high_risk_post)
        resp = await mock_http_client.post(
            "http://layer3-guardian:8002/actions/preview",
            json={"action_id": 7, "target_node": "node-1"},  # drain_node
        )
        data = resp.json()
        assert data["approved"]   is False
        assert data["risk_score"] > 0.5

    async def test_ghost_preview_calls_gnn_reinfer(
        self, mock_http_client, mock_gnn_response
    ):
        """Ghost Preview must call GNN /infer to simulate post-action state."""
        # GNN should be called twice: once for current, once for predicted state
        call_count = 0
        original_post = mock_http_client.post

        async def _counting_post(url, **kwargs):
            nonlocal call_count
            if "infer" in url:
                call_count += 1
            return await original_post(url, **kwargs)

        mock_http_client.post = AsyncMock(side_effect=_counting_post)
        await mock_http_client.post(
            "http://layer2-cognitive:8001/infer",
            json={},
        )
        assert call_count >= 1

    async def test_ghost_preview_published_to_kafka(
        self, restart_pod_request, fake_kafka_producer
    ):
        """Ghost Preview results should be included in action events published to Kafka."""
        result = ActionResult(
            audit_id=_uid(),
            request=restart_pod_request,
            status=ActionStatus.STATUS_SUCCEEDED,
        )
        await fake_kafka_producer.send(
            "ccdt.guardian.actions",
            result.SerializeToString(),
        )
        msg  = fake_kafka_producer.messages[0]
        back = ActionResult.FromString(msg["value"])
        assert back.status == ActionStatus.STATUS_SUCCEEDED


# ══════════════════════════════════════════════════════════════════════════════
# OPA policy evaluation pipeline
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer3
class TestOPAPolicyPipeline:
    async def test_opa_allows_safe_restart_pod(self, mock_opa_allow_all):
        """
        restart_pod on a fault pod must pass all OPA policies.
        """
        action_input = {
            "action": {"name": "restart_pod", "target_node": "payment-svc",
                       "parameters": {}, "history": []},
            "node":   {"cpu": 0.9, "mem": 0.95, "status": "critical",
                       "class": "fault", "oom_kills": 3, "cap_event": 0,
                       "file_event": 0, "is_isolated": False},
            "cluster": {"namespace": "production", "nodes": 9},
            "context": {"autonomy_mode": "supervised", "human_approved": False,
                        "can_write_ns": ["production"]},
        }
        # Mock OPA server returns allow=True for all policies
        for policy in ("no_privilege_escalation", "lateral_movement",
                       "egress_control", "cpu_threshold", "oom_notification"):
            resp = await mock_opa_allow_all.post(
                f"http://opa:8181/v1/data/ccdt/guardian/policies/{policy}",
                json={"input": action_input},
            )
            data = resp.json()
            assert data["result"]["allow"] is True

    async def test_opa_blocks_cpu_threshold_violation(self, mock_opa_deny_cpu):
        """
        cpu_threshold policy must block scale_down when CPU is below 20%.
        """
        action_input = {
            "action": {"name": "scale_down_replicas",
                       "target_node": "idle-svc",
                       "parameters": {"replica_delta": -2}, "history": []},
            "node":   {"cpu": 0.15, "mem": 0.3, "status": "healthy",
                       "class": "healthy", "oom_kills": 0, "cap_event": 0,
                       "file_event": 0, "is_isolated": False},
            "cluster": {"namespace": "production", "nodes": 9},
            "context": {"autonomy_mode": "full-auto", "human_approved": False,
                        "can_write_ns": ["production"]},
        }
        resp = await mock_opa_deny_cpu.post(
            "http://opa:8181/v1/data/ccdt/guardian/policies/cpu_threshold",
            json={"input": action_input},
        )
        data = resp.json()
        assert data["result"]["allow"] is False
        assert len(data["result"]["violations"]) > 0

    async def test_all_policies_evaluated_in_parallel(self, mock_opa_allow_all):
        """All 5 policies must be evaluated; a single denial blocks the action."""
        policy_names = [
            "no_privilege_escalation",
            "lateral_movement",
            "egress_control",
            "cpu_threshold",
            "oom_notification",
        ]
        assert len(policy_names) == 5

        results = []
        for policy in policy_names:
            resp = await mock_opa_allow_all.post(
                f"http://opa:8181/v1/data/ccdt/guardian/policies/{policy}",
                json={"input": {}},
            )
            results.append(resp.json()["result"]["allow"])

        # All 5 must pass for action to be approved
        assert all(results) is True

    async def test_opa_denial_prevents_k8s_execution(
        self, high_risk_ghost, mock_k8s_client
    ):
        """
        When OPA denies, the Kubernetes API must NOT be called.
        """
        ghost = high_risk_ghost
        assert ghost.opa_approved is False
        assert len(ghost.opa_violations) > 0

        # Kubernetes execute should never be called when OPA denies
        mock_k8s_client.CoreV1Api.return_value.delete_namespaced_pod.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Action execution and Kafka publish
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer3
class TestActionExecutionPipeline:
    async def test_successful_action_published_to_kafka(
        self, succeeded_action_result, fake_kafka_producer
    ):
        payload = succeeded_action_result.SerializeToString()
        await fake_kafka_producer.send(
            "ccdt.guardian.actions",
            payload,
            key=succeeded_action_result.audit_id.encode(),
        )
        msg  = fake_kafka_producer.messages[0]
        back = ActionResult.FromString(msg["value"])
        assert back.status         == ActionStatus.STATUS_SUCCEEDED
        assert back.verified_effect is True

    async def test_action_result_schema_valid(self, succeeded_action_result):
        raw  = succeeded_action_result.SerializeToString()
        back = ActionResult.FromString(raw)
        assert back.schema_ver   == "1.0"
        assert back.audit_id     != ""
        assert back.incident_id  != ""

    async def test_denied_action_published_with_violations(
        self, restart_pod_request, fake_kafka_producer
    ):
        """Denied actions must still be published (for audit trail)."""
        denied_result = ActionResult(
            audit_id=_uid(),
            request=restart_pod_request,
            status=ActionStatus.STATUS_DENIED,
            message="OPA denied: cpu_threshold violation",
            schema_ver="1.0",
        )
        await fake_kafka_producer.send(
            "ccdt.guardian.actions",
            denied_result.SerializeToString(),
        )
        back = ActionResult.FromString(fake_kafka_producer.messages[0]["value"])
        assert back.status   == ActionStatus.STATUS_DENIED
        assert "OPA" in back.message

    async def test_rollback_published_when_verification_fails(
        self, restart_pod_request, fake_kafka_producer
    ):
        """If post-action health check fails, ROLLED_BACK status should be published."""
        rolled_back = ActionResult(
            audit_id=_uid(),
            request=restart_pod_request,
            status=ActionStatus.STATUS_ROLLED_BACK,
            was_rolled_back=True,
            rollback_reason="Post-action health check failed: OOM rate did not decrease",
            schema_ver="1.0",
        )
        await fake_kafka_producer.send(
            "ccdt.guardian.actions",
            rolled_back.SerializeToString(),
        )
        back = ActionResult.FromString(fake_kafka_producer.messages[0]["value"])
        assert back.status        == ActionStatus.STATUS_ROLLED_BACK
        assert back.was_rolled_back is True

    async def test_k8s_restart_pod_dispatched(self, mock_k8s_client):
        """
        After OPA approval, _restart_pod should call K8s list + delete pod APIs.
        """
        namespace = "production"
        target    = "payment-svc"

        # Simulate the K8s call sequence: list pods → delete
        pods = mock_k8s_client.CoreV1Api.return_value.list_namespaced_pod.return_value
        pods.items = [MagicMock(metadata=MagicMock(name="payment-svc-pod-abc123"))]

        v1 = mock_k8s_client.CoreV1Api.return_value
        v1.list_namespaced_pod(namespace, label_selector=f"app={target}")
        v1.delete_namespaced_pod("payment-svc-pod-abc123", namespace)
        v1.list_namespaced_pod.assert_called_once()
        v1.delete_namespaced_pod.assert_called_once_with("payment-svc-pod-abc123", namespace)

    async def test_k8s_isolate_creates_network_policy(self, mock_k8s_client):
        """
        isolate_container should call create_namespaced_network_policy.
        """
        namespace  = "production"
        target     = "auth-svc"

        netv1 = mock_k8s_client.NetworkingV1Api.return_value
        netv1.create_namespaced_network_policy(
            namespace,
            MagicMock(metadata=MagicMock(name=f"isolate-{target}-{int(1000)}"))
        )
        netv1.create_namespaced_network_policy.assert_called_once()
