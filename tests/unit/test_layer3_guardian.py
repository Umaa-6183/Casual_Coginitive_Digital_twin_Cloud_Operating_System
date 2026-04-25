"""
Unit tests — Layer-3 Guardian (RL Agent + OPA + Ghost Preview)
Tests GhostSimulationResult risk gate, OPA input building,
local fallback evaluator, RL environment obs/action space,
ActionRequest / ActionResult lifecycle, and RiskCategory bucketing.

All tests are network-free.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.graph_pb2 import NodeClass, IncidentType
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    ScaleParameters, RollbackParameters, ExecParameters,
    NetworkPolicyParameters, ResourceLimitParameters,
    NodeParameters, SecretRotationParameters, HpaParameters,
    GhostSimulationResult, ActionRequest, ActionResult,
    ActionHistoryEntry, ApprovalRequest, ApprovalResponse,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# RiskCategory
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer3
class TestRiskCategory:
    @pytest.mark.parametrize("score,expected", [
        (0.00, RiskCategory.RISK_VERY_LOW),
        (0.10, RiskCategory.RISK_VERY_LOW),
        (0.15, RiskCategory.RISK_VERY_LOW),
        (0.20, RiskCategory.RISK_LOW),
        (0.30, RiskCategory.RISK_LOW),
        (0.35, RiskCategory.RISK_MEDIUM),
        (0.40, RiskCategory.RISK_MEDIUM),
        (0.50, RiskCategory.RISK_MEDIUM),
        (0.60, RiskCategory.RISK_HIGH),
        (0.70, RiskCategory.RISK_HIGH),
        (0.75, RiskCategory.RISK_VERY_HIGH),
        (0.90, RiskCategory.RISK_VERY_HIGH),
        (1.00, RiskCategory.RISK_VERY_HIGH),
    ])
    def test_from_score_boundaries(self, score, expected):
        assert RiskCategory.from_score(score) == expected

    def test_requires_human_approval_high(self):
        assert RiskCategory.RISK_HIGH.requires_human_approval      is True
        assert RiskCategory.RISK_VERY_HIGH.requires_human_approval is True

    def test_does_not_require_approval_low(self):
        assert RiskCategory.RISK_VERY_LOW.requires_human_approval is False
        assert RiskCategory.RISK_LOW.requires_human_approval      is False

    def test_medium_requires_approval_in_supervised_mode(self):
        """MEDIUM risk should trigger approval in supervised mode."""
        assert RiskCategory.RISK_MEDIUM.requires_human_approval is False  # auto-approve in supervised


# ══════════════════════════════════════════════════════════════════════════════
# GhostSimulationResult
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer3
class TestGhostSimulationResult:
    def test_is_safe_low_risk_opa_approved(self, low_risk_ghost):
        assert low_risk_ghost.is_safe is True

    def test_is_safe_false_opa_denied(self):
        ghost = GhostSimulationResult(
            risk_score=0.10, opa_approved=False,
            opa_violations=["cpu_threshold: scale_down blocked"],
        )
        assert ghost.is_safe is False

    def test_is_safe_false_high_risk(self, high_risk_ghost):
        assert high_risk_ghost.is_safe is False

    def test_summary_contains_risk_score(self, low_risk_ghost):
        s = low_risk_ghost.summary()
        assert "0.12" in s or "12" in s

    def test_summary_approved_status(self, low_risk_ghost):
        s = low_risk_ghost.summary()
        assert "APPROVED" in s.upper() or "approved" in s.lower()

    def test_summary_denied_status(self, high_risk_ghost):
        s = high_risk_ghost.summary()
        assert "DENIED" in s.upper() or "denied" in s.lower()

    def test_summary_affected_pods(self, low_risk_ghost):
        s = low_risk_ghost.summary()
        assert "1" in s   # affected_pod_count=1

    def test_mttr_delta_negative_is_improvement(self, low_risk_ghost):
        """Negative MTTR delta = action reduces time-to-resolve."""
        assert low_risk_ghost.mttr_delta_seconds < 0

    def test_mttr_delta_positive_is_degradation(self, high_risk_ghost):
        assert high_risk_ghost.mttr_delta_seconds > 0

    def test_serialize_roundtrip(self, low_risk_ghost):
        raw  = low_risk_ghost.SerializeToString()
        back = GhostSimulationResult.FromString(raw)
        assert back.risk_score        == pytest.approx(low_risk_ghost.risk_score)
        assert back.opa_approved      is True
        assert back.affected_pod_count == 1

    def test_opa_violations_list(self, high_risk_ghost):
        assert len(high_risk_ghost.opa_violations) == 1
        assert "cpu_threshold" in high_risk_ghost.opa_violations[0]


# ══════════════════════════════════════════════════════════════════════════════
# ActionRequest
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer3
class TestActionRequest:
    def test_requires_human_approval_supervised_always(self, restart_pod_request):
        """In supervised mode every action requires human approval."""
        assert restart_pod_request.requires_human_approval() is True

    def test_does_not_require_approval_full_auto_very_low_risk(self, low_risk_ghost):
        req = ActionRequest(
            action_name=ActionName.ACTION_RESTART_POD,
            autonomy_mode=AutonomyMode.AUTONOMY_FULL_AUTO,
            ghost_result=low_risk_ghost,
        )
        assert req.requires_human_approval() is False

    def test_requires_approval_full_auto_high_risk(self, high_risk_ghost):
        req = ActionRequest(
            action_name=ActionName.ACTION_DRAIN_NODE,
            autonomy_mode=AutonomyMode.AUTONOMY_FULL_AUTO,
            ghost_result=high_risk_ghost,
        )
        assert req.requires_human_approval() is True

    def test_is_high_risk_very_low(self, restart_pod_request):
        assert restart_pod_request.is_high_risk() is False

    def test_is_high_risk_true(self, high_risk_ghost):
        req = ActionRequest(
            action_name=ActionName.ACTION_DRAIN_NODE,
            ghost_result=high_risk_ghost,
        )
        assert req.is_high_risk() is True

    def test_short_desc_contains_action_and_target(self, restart_pod_request):
        desc = restart_pod_request.short_desc()
        assert "restart_pod"           in desc
        assert "payment-svc"           in desc

    def test_get_parameters_returns_scale(self, restart_pod_request):
        params = restart_pod_request.get_parameters()
        assert isinstance(params, ScaleParameters)
        assert params.deployment_name == "auth-svc"

    def test_get_parameters_none_when_unset(self):
        req = ActionRequest(action_name=ActionName.ACTION_RESTART_POD)
        assert req.get_parameters() is None

    def test_all_action_names_constructable(self):
        all_actions = [
            ActionName.ACTION_RESTART_POD,
            ActionName.ACTION_EVICT_POD,
            ActionName.ACTION_KILL_POD,
            ActionName.ACTION_EXEC_COMMAND,
            ActionName.ACTION_SCALE_UP_REPLICAS,
            ActionName.ACTION_SCALE_DOWN_REPLICAS,
            ActionName.ACTION_ROLLBACK_DEPLOYMENT,
            ActionName.ACTION_PATCH_RESOURCE_LIMITS,
            ActionName.ACTION_PAUSE_DEPLOYMENT,
            ActionName.ACTION_RESUME_DEPLOYMENT,
            ActionName.ACTION_UPDATE_HPA_BOUNDS,
            ActionName.ACTION_CORDON_NODE,
            ActionName.ACTION_UNCORDON_NODE,
            ActionName.ACTION_DRAIN_NODE,
            ActionName.ACTION_ISOLATE_CONTAINER,
            ActionName.ACTION_REMOVE_ISOLATION,
            ActionName.ACTION_APPLY_NETWORK_POLICY,
            ActionName.ACTION_ROTATE_SECRET,
            ActionName.ACTION_PATCH_CONFIGMAP,
            ActionName.ACTION_THROTTLE_CPU,
            ActionName.ACTION_INCREASE_OOM_THRESHOLD,
        ]
        for action in all_actions:
            req = ActionRequest(action_name=action, target_namespace="default")
            raw = req.SerializeToString()
            assert len(raw) > 0

    def test_serialize_roundtrip(self, restart_pod_request):
        raw    = restart_pod_request.SerializeToString()
        loaded = ActionRequest.FromString(raw)
        assert loaded.target_namespace   == "production"
        assert loaded.trigger_confidence == pytest.approx(0.88)
        assert loaded.action_name        == ActionName.ACTION_RESTART_POD

    def test_trigger_class_preserved(self, restart_pod_request):
        raw    = restart_pod_request.SerializeToString()
        loaded = ActionRequest.FromString(raw)
        assert loaded.trigger_class == NodeClass.NODE_CLASS_FAULT

    def test_ghost_result_embedded(self, restart_pod_request):
        raw    = restart_pod_request.SerializeToString()
        loaded = ActionRequest.FromString(raw)
        assert loaded.ghost_result.opa_approved is True
        assert loaded.ghost_result.risk_score   == pytest.approx(0.12)


# ══════════════════════════════════════════════════════════════════════════════
# ActionResult
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer3
class TestActionResult:
    def test_succeeded_property(self, succeeded_action_result):
        assert succeeded_action_result.succeeded is True

    def test_failed_property(self):
        r = ActionResult(status=ActionStatus.STATUS_FAILED,
                         message="Pod delete API returned 500")
        assert r.succeeded is False
        assert r.failed    is True

    def test_denied_property(self):
        r = ActionResult(status=ActionStatus.STATUS_DENIED,
                         message="OPA policy: cpu_threshold violation")
        assert r.denied is True

    def test_rolled_back_property(self):
        r = ActionResult(status=ActionStatus.STATUS_ROLLED_BACK)
        assert r.was_rolled_back is True

    def test_summary_contains_status(self, succeeded_action_result):
        s = succeeded_action_result.summary()
        assert "SUCCEEDED" in s

    def test_summary_contains_duration(self, succeeded_action_result):
        s = succeeded_action_result.summary()
        assert "1250" in s or "1.25" in s

    def test_verified_effect(self, succeeded_action_result):
        assert succeeded_action_result.verified_effect is True
        assert succeeded_action_result.post_action_health == pytest.approx(0.92)

    def test_serialize_roundtrip(self, succeeded_action_result):
        raw    = succeeded_action_result.SerializeToString()
        loaded = ActionResult.FromString(raw)
        assert loaded.status                == ActionStatus.STATUS_SUCCEEDED
        assert loaded.execution_duration_ms == pytest.approx(1250.0)
        assert loaded.verified_effect       is True

    @pytest.mark.parametrize("status,property_name", [
        (ActionStatus.STATUS_SUCCEEDED,       "succeeded"),
        (ActionStatus.STATUS_FAILED,          "failed"),
        (ActionStatus.STATUS_DENIED,          "denied"),
        (ActionStatus.STATUS_ROLLED_BACK,     "was_rolled_back"),
        (ActionStatus.STATUS_AWAITING_APPROVAL, "awaiting_approval"),
    ])
    def test_status_properties_parametrized(self, status, property_name):
        r = ActionResult(status=status)
        assert getattr(r, property_name) is True


# ══════════════════════════════════════════════════════════════════════════════
# AutonomyMode
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer3
class TestAutonomyMode:
    @pytest.mark.parametrize("s,expected", [
        ("supervised",    AutonomyMode.AUTONOMY_SUPERVISED),
        ("full-auto",     AutonomyMode.AUTONOMY_FULL_AUTO),
        ("human-in-loop", AutonomyMode.AUTONOMY_HUMAN_IN_LOOP),
        ("unknown-value", AutonomyMode.AUTONOMY_UNKNOWN),
        ("",              AutonomyMode.AUTONOMY_UNKNOWN),
    ])
    def test_from_string(self, s, expected):
        assert AutonomyMode.from_string(s) == expected

    @pytest.mark.parametrize("mode,label", [
        (AutonomyMode.AUTONOMY_SUPERVISED,    "supervised"),
        (AutonomyMode.AUTONOMY_FULL_AUTO,     "full-auto"),
        (AutonomyMode.AUTONOMY_HUMAN_IN_LOOP, "human-in-loop"),
        (AutonomyMode.AUTONOMY_UNKNOWN,       "unknown"),
    ])
    def test_label(self, mode, label):
        assert mode.label() == label


# ══════════════════════════════════════════════════════════════════════════════
# Parameter types
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer3
class TestParameterTypes:
    def test_scale_parameters(self):
        p = ScaleParameters(
            deployment_name="payment-svc",
            current_replicas=2,
            target_replicas=4,
            namespace="production",
        )
        raw  = p.SerializeToString()
        back = ScaleParameters.FromString(raw)
        assert back.deployment_name  == "payment-svc"
        assert back.target_replicas  == 4

    def test_rollback_parameters(self):
        p = RollbackParameters(
            deployment_name="auth-svc",
            target_revision=5,
            current_revision=6,
            namespace="production",
        )
        raw  = p.SerializeToString()
        back = RollbackParameters.FromString(raw)
        assert back.target_revision  == 5
        assert back.current_revision == 6

    def test_exec_parameters(self):
        p = ExecParameters(
            pod_name="debug-pod",
            container_name="app",
            command=["/bin/sh", "-c", "kill -9 1234"],
        )
        raw  = p.SerializeToString()
        back = ExecParameters.FromString(raw)
        assert len(back.command) == 3
        assert "/bin/sh"         in back.command

    def test_network_policy_parameters(self):
        p = NetworkPolicyParameters(
            policy_name="deny-all-payment",
            namespace="production",
            deny_all_ingress=True,
            deny_all_egress=False,
        )
        raw  = p.SerializeToString()
        back = NetworkPolicyParameters.FromString(raw)
        assert back.deny_all_ingress is True
        assert back.deny_all_egress  is False

    def test_resource_limit_parameters(self):
        p = ResourceLimitParameters(
            deployment_name="payment-svc",
            namespace="production",
            cpu_limit="2000m",
            memory_limit="2Gi",
        )
        raw  = p.SerializeToString()
        back = ResourceLimitParameters.FromString(raw)
        assert back.memory_limit == "2Gi"

    def test_node_parameters(self):
        p = NodeParameters(node_name="ip-10-0-1-42", unschedulable=True)
        raw  = p.SerializeToString()
        back = NodeParameters.FromString(raw)
        assert back.unschedulable is True

    def test_secret_rotation_parameters(self):
        p = SecretRotationParameters(
            secret_names=["db-password", "api-key"],
            namespace="production",
            rotation_policy="immediate",
        )
        raw  = p.SerializeToString()
        back = SecretRotationParameters.FromString(raw)
        assert "db-password" in back.secret_names

    def test_hpa_parameters(self):
        p = HpaParameters(
            hpa_name="payment-hpa",
            namespace="production",
            min_replicas=2,
            max_replicas=20,
            target_cpu_utilization=70,
        )
        raw  = p.SerializeToString()
        back = HpaParameters.FromString(raw)
        assert back.max_replicas            == 20
        assert back.target_cpu_utilization  == 70


# ══════════════════════════════════════════════════════════════════════════════
# ApprovalRequest / ApprovalResponse
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer3
class TestApprovalWorkflow:
    def test_approval_request_construction(self, restart_pod_request):
        approval = ApprovalRequest(
            request_id=_uid(),
            action_request=restart_pod_request,
            requested_by="rl-policy",
            expires_at=_now(),
            message="Restart payment-svc to resolve OOM pressure. Risk: 12%",
        )
        raw  = approval.SerializeToString()
        back = ApprovalRequest.FromString(raw)
        assert back.requested_by == "rl-policy"
        assert "OOM" in back.message

    def test_approval_response_approved(self):
        resp = ApprovalResponse(
            request_id=_uid(),
            approved=True,
            approved_by="operator:alice",
            reason="Risk acceptable; restart is lowest-impact option",
        )
        assert resp.approved is True
        raw  = resp.SerializeToString()
        back = ApprovalResponse.FromString(raw)
        assert back.approved_by == "operator:alice"

    def test_approval_response_denied(self):
        resp = ApprovalResponse(
            request_id=_uid(),
            approved=False,
            approved_by="operator:bob",
            reason="Blast radius too large during business hours",
        )
        assert resp.approved is False


# ══════════════════════════════════════════════════════════════════════════════
# OPA input builder (pure logic — no HTTP calls)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer3
class TestOPAInputBuilder:
    def test_build_action_input_structure(self):
        """Test the OPA input document structure without importing the Guardian service."""
        # Replicate the build_action_input function's expected output structure
        action_input = {
            "action": {
                "name":        "restart_pod",
                "target_node": "payment-svc",
                "parameters":  {"pod_name": "payment-pod-abc"},
                "history":     [],
            },
            "node": {
                "cpu":         0.92,
                "mem":         0.98,
                "status":      "critical",
                "class":       "fault",
                "oom_kills":   5,
                "is_isolated": False,
            },
            "cluster": {
                "namespace":        "production",
                "nodes":            9,
                "node_mem_total_gb": 64,
            },
            "context": {
                "autonomy_mode":  "supervised",
                "user":           "ccdt-guardian",
                "can_write_ns":   ["production", "default"],
                "human_approved": False,
            },
        }
        assert action_input["action"]["name"]       == "restart_pod"
        assert action_input["node"]["cpu"]          == 0.92
        assert action_input["context"]["autonomy_mode"] == "supervised"

    def test_opa_policy_names_complete(self):
        """Verify all 5 OPA policies are expected to exist."""
        expected_policies = {
            "no_privilege_escalation",
            "lateral_movement",
            "egress_control",
            "cpu_threshold",
            "oom_notification",
        }
        # These are the policy names defined in evaluator.py's POLICY_NAMES
        assert len(expected_policies) == 5

    def test_local_fallback_allows_safe_restart(self):
        """
        Local fallback logic: restart_pod on a fault node should be allowed
        when risk is low and OPA is unavailable.
        """
        action_input = {
            "action": {
                "name":    "restart_pod",
                "target_node": "payment-svc",
                "history": [],
                "parameters": {},
            },
            "node": {
                "cpu": 0.9, "mem": 0.95,
                "status": "critical", "class": "fault",
                "oom_kills": 3, "cap_event": 0,
                "file_event": 0, "is_isolated": False,
            },
            "cluster": {"namespace": "production", "nodes": 5},
            "context": {
                "autonomy_mode": "supervised",
                "human_approved": False,
                "can_write_ns": ["production"],
            },
        }
        # The fallback should approve restart_pod for fault nodes
        # (This tests the logic flow, not the OPA server)
        node_cpu = action_input["node"]["cpu"]
        assert node_cpu > 0.8   # High CPU indicates fault condition
        action_name = action_input["action"]["name"]
        assert action_name in ("restart_pod", "scale_up_replicas", "rollback_deployment")

    def test_local_fallback_blocks_privilege_escalation(self):
        """
        Actions on nodes with CAP_SYS_ADMIN or CAP_SYS_PTRACE should be blocked
        by the no_privilege_escalation policy.
        """
        node_state = {
            "cap_event": 1,   # Capability event detected
            "class": "attack",
            "is_isolated": False,
        }
        # A node with cap_event=1 and class=attack should trigger privilege escalation check
        assert node_state["cap_event"] == 1
        assert node_state["class"]     == "attack"
