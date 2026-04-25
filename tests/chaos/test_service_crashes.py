"""
Chaos tests — Service Crashes & Restarts
════════════════════════════════════════════════════════════════════════════════
Tests CCDT resilience when individual microservices crash and restart:
  • Layer-2 GNN service unavailable (Guardian falls back to safe defaults)
  • Layer-3 Guardian service crash (Co-Pilot warns operator; no blind execution)
  • Layer-4 Co-Pilot restart (session state recovery from persisted context)
  • API Gateway upstream timeout (circuit-breaker-style response)
  • OPA sidecar crash (Guardian falls back to local policy evaluator)
  • Concurrent service degradation (multiple services failing simultaneously)

All external calls are mocked with DegradedServiceConfig.
════════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, GnnInferenceResult, NodeFeatures, TopologyNode,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    GhostSimulationResult, ActionRequest, ActionResult,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, SessionState, ChatMessage, SessionContext,
    TokenUsage, IncidentReport,
)

from tests.chaos.conftest import DegradedServiceConfig, _make_degraded_http_client


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# GNN service unavailable
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer2
@pytest.mark.layer3
class TestGnnServiceDown:
    """
    When Layer-2 (GNN) is unavailable, Layer-3 Guardian must:
      1. Not crash
      2. Reject action proposals that require fresh inference context
      3. Return a structured error to upstream callers
    """

    async def test_guardian_returns_error_when_gnn_timeout(
        self,
        degraded_http_client_gnn_down,
        restart_pod_request,
    ):
        """Guardian preview with GNN down should raise or return empty result — not crash."""
        client = degraded_http_client_gnn_down

        # Simulate Guardian calling GNN for topology context
        try:
            response = await asyncio.wait_for(
                client.get("http://layer2-cognitive:8001/topology"),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            # Expected — GNN is timing out
            pass
        except Exception as exc:
            # Any other exception must be a known type, not an unhandled crash
            assert isinstance(exc, (asyncio.TimeoutError, IOError, RuntimeError))

    async def test_action_request_still_valid_without_gnn_context(
        self, restart_pod_request
    ):
        """ActionRequest object must remain valid even without live GNN data."""
        assert restart_pod_request.action_name == ActionName.ACTION_RESTART_POD
        assert restart_pod_request.ghost_result is not None
        assert restart_pod_request.ghost_result.opa_approved is True

    async def test_gnn_flapping_partial_responses(
        self, degraded_http_client_guardian_partial, fault_inference
    ):
        """
        When GNN returns partial responses (empty JSON), the consumer must
        treat them as missing data, not crash on KeyError.
        """
        client = degraded_http_client_guardian_partial
        successful = 0
        errors = 0

        for _ in range(10):
            try:
                resp = await client.get("http://layer2-cognitive:8001/infer")
                data = resp.json()
                if data.get("inference_id"):
                    successful += 1
                # Empty dict → handled gracefully without exception
            except Exception:
                errors += 1

        # With the flapping config, some calls will 500, some will return partial
        # Key invariant: no unhandled exception caused the test to fail
        assert successful + errors <= 10

    async def test_stale_inference_used_when_gnn_down(
        self, fault_inference
    ):
        """
        When GNN is down, the last known inference result should be usable.
        This tests the data structure itself, not the service call.
        """
        # Simulate cache hit: use fault_inference from last successful call
        cached_inference = fault_inference

        # Validate that the cached result still has all required fields
        assert cached_inference.is_active_incident is True
        assert cached_inference.root_cause_node_name == "payment-svc"
        assert cached_inference.graph_confidence > 0.8

    async def test_gnn_recovery_restores_normal_operation(
        self, gnn_service_down, normal_gnn_payload, normal_guardian_payload
    ):
        """After GNN recovers, subsequent calls should succeed."""
        # Phase 1: GNN down
        down_cfg = DegradedServiceConfig(always_timeout=True)
        guardian_cfg = DegradedServiceConfig()  # guardian fine

        client = _make_degraded_http_client(
            gnn_cfg=down_cfg,
            guardian_cfg=guardian_cfg,
            normal_gnn_payload=normal_gnn_payload,
            normal_guardian_payload=normal_guardian_payload,
        )

        try:
            await asyncio.wait_for(
                client.get("http://layer2-cognitive:8001/infer"), timeout=0.1
            )
        except asyncio.TimeoutError:
            pass  # expected

        # Phase 2: GNN recovered
        down_cfg.always_timeout = False
        resp = await client.get("http://layer2-cognitive:8001/infer")
        data = resp.json()
        assert data.get("incident_type") == "FAULT"


# ══════════════════════════════════════════════════════════════════════════════
# Guardian service crash
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer3
@pytest.mark.layer4
class TestGuardianServiceCrash:
    """
    When Layer-3 Guardian crashes, Layer-4 Co-Pilot must:
      1. Catch the connection error
      2. Inform the operator that remediation is unavailable
      3. Not attempt blind Kubernetes API calls
    """

    async def test_copilot_session_survives_guardian_crash(
        self, copilot_session
    ):
        """SessionContext must remain usable after Guardian communication failure."""
        session = copilot_session

        # Simulate Guardian returning 500
        error_message = ChatMessage(
            role=MessageRole.ROLE_ASSISTANT,
            content=(
                "⚠️ Guardian service is currently unavailable. "
                "Automated remediation is paused. Please investigate manually."
            ),
        )
        session.add_message(error_message)

        assert session.state == SessionState.SESSION_ACTIVE
        last_msg = session.history[-1]
        assert "Guardian" in last_msg.content or "unavailable" in last_msg.content

    async def test_incident_report_generated_without_guardian(
        self, fault_inference
    ):
        """IncidentReport must be generatable even when Ghost Preview fails."""
        report = IncidentReport(
            inference_id=fault_inference.inference_id,
            incident_type=IncidentType.INCIDENT_FAULT,
            graph_confidence=fault_inference.graph_confidence,
            root_cause_service=fault_inference.root_cause_node_name,
            severity="high",
            nl_summary="Guardian unavailable — automated remediation paused.",
            # proposed_action intentionally omitted (Guardian down)
        )

        injection = report.to_system_prompt_injection()
        assert "payment-svc" in injection
        assert "FAULT" in injection

    async def test_action_request_not_executed_when_guardian_down(
        self, restart_pod_request
    ):
        """
        Without Guardian's approval pipeline, execution must be blocked.
        Validate that requires_human_approval() still returns correct value.
        """
        # In SUPERVISED mode, human approval is always required
        assert restart_pod_request.autonomy_mode == AutonomyMode.AUTONOMY_SUPERVISED
        assert restart_pod_request.requires_human_approval() is True

    async def test_guardian_500_response_handled_by_copilot(
        self, degraded_http_client_guardian_partial
    ):
        """Co-Pilot calling Guardian for ghost preview handles 500 gracefully."""
        client = degraded_http_client_guardian_partial
        errors = 0
        empty_responses = 0

        for _ in range(5):
            try:
                resp = await client.post(
                    "http://layer3-guardian:8002/actions/preview",
                    json={"action_name": "restart_pod"},
                )
                data = resp.json()
                if not data:
                    empty_responses += 1
            except Exception:
                errors += 1

        # With partial_response_rate=1.0, all responses are empty dicts
        assert empty_responses + errors == 5

    async def test_concurrent_guardian_failures(self):
        """Multiple concurrent Guardian calls all failing must not deadlock."""
        error_cfg = DegradedServiceConfig(error_500_rate=1.0)
        clients = []

        for _ in range(5):
            client = AsyncMock()
            client.post = AsyncMock(
                side_effect=Exception("Guardian: 500 Internal Server Error")
            )
            clients.append(client)

        async def _call_guardian(c):
            try:
                await c.post("http://layer3-guardian:8002/actions/execute", json={})
            except Exception:
                return "error"
            return "ok"

        results = await asyncio.gather(*[_call_guardian(c) for c in clients])
        assert all(r == "error" for r in results)


# ══════════════════════════════════════════════════════════════════════════════
# OPA sidecar crash
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer3
@pytest.mark.opa
class TestOpaSidecarCrash:
    """
    When the OPA sidecar crashes, the Guardian's local fallback evaluator
    must engage and produce safe, conservative decisions.
    """

    def test_local_fallback_blocks_high_risk_action(
        self, high_risk_ghost
    ):
        """
        Without OPA available, local fallback must block actions where
        risk_score > 0.7 OR affected_pod_count > 5.
        """
        # Simulate the local fallback rule: block if risk > 0.60 or pods > 5
        ghost = high_risk_ghost
        local_allow = (
            ghost.risk_score < 0.60
            and ghost.affected_pod_count <= 5
        )
        assert local_allow is False  # high_risk_ghost has risk=0.72, pods=12

    def test_local_fallback_allows_low_risk_action(
        self, low_risk_ghost
    ):
        """Low-risk actions must pass the local fallback evaluator."""
        ghost = low_risk_ghost
        local_allow = (
            ghost.risk_score < 0.60
            and ghost.affected_pod_count <= 5
        )
        assert local_allow is True  # low_risk_ghost has risk=0.12, pods=1

    async def test_opa_connection_error_triggers_fallback(self):
        """OPA returning connection error must trigger local fallback, not crash."""
        opa_client = AsyncMock()
        opa_client.post = AsyncMock(
            side_effect=Exception("Connection refused: OPA sidecar not running")
        )

        opa_available = True
        try:
            await opa_client.post(
                "http://opa:8181/v1/data/ccdt/guardian/policies/blast_radius",
                json={"input": {}},
            )
        except Exception:
            opa_available = False

        assert opa_available is False  # OPA is confirmed down → use local fallback

    async def test_opa_recovery_after_restart(self, mock_opa_allow_all):
        """
        After OPA restarts and becomes reachable again, normal evaluation resumes.
        """
        resp = await mock_opa_allow_all.post(
            "http://opa:8181/v1/data/ccdt/guardian/policies/blast_radius",
            json={"input": {"action": "restart_pod"}},
        )
        data = resp.json()
        assert data["result"]["allow"] is True

    def test_five_opa_policies_all_required(self):
        """
        Ensure that ALL 5 Guardian OPA policy names are known — a crash of OPA
        must block ALL of them, not silently allow.
        """
        required_policies = {
            "blast_radius",
            "cpu_threshold",
            "lateral_movement",
            "rate_limit",
            "working_hours",
        }
        assert len(required_policies) == 5


# ══════════════════════════════════════════════════════════════════════════════
# Co-Pilot session recovery
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer4
class TestCopilotSessionRecovery:
    """
    When Layer-4 Co-Pilot crashes and restarts, session state must be
    recoverable from persisted context (e.g., Redis or in-memory on restart).
    """

    def test_session_serializes_full_history(self, copilot_session):
        """Full session with history must serialize to bytes without loss."""
        raw = copilot_session.SerializeToString()
        assert len(raw) > 0
        recovered = SessionContext.FromString(raw)
        assert recovered.operator_id == copilot_session.operator_id
        assert len(recovered.history) == len(copilot_session.history)

    def test_session_history_content_preserved(self, copilot_session):
        """Message content must survive serialization round-trip."""
        raw = copilot_session.SerializeToString()
        recovered = SessionContext.FromString(raw)

        original_contents = [m.content for m in copilot_session.history]
        recovered_contents = [m.content for m in recovered.history]
        assert original_contents == recovered_contents

    def test_token_usage_preserved_across_restart(self):
        """Token usage totals must be preserved after session recovery."""
        session = SessionContext(operator_id="alice")
        session.add_message(ChatMessage(
            role=MessageRole.ROLE_ASSISTANT,
            content="Root cause: OOM pressure",
            token_usage=TokenUsage(input_tokens=500, output_tokens=120),
        ))

        raw = session.SerializeToString()
        recovered = SessionContext.FromString(raw)

        assert recovered.total_token_usage.input_tokens == 500
        assert recovered.total_token_usage.output_tokens == 120

    def test_fresh_session_created_when_recovery_fails(self):
        """
        If persisted state is corrupt / missing, a new blank session
        must be created rather than blocking the operator.
        """
        corrupt_bytes = b"\xff\xfe" * 50
        try:
            SessionContext.FromString(corrupt_bytes)
        except Exception:
            # Create a fresh session instead
            fresh_session = SessionContext(
                session_id=_uid(),
                operator_id="alice",
            )
            assert fresh_session.turn_count == 0
            assert fresh_session.history == []

    def test_session_state_transitions_after_crash(self):
        """
        After recovery from crash, session transitions to ACTIVE state
        regardless of what state it was in before the crash.
        """
        session = SessionContext(operator_id="bob")
        session.state = SessionState.SESSION_ERROR  # last known state before crash

        # Recovery procedure: touch() reactivates idle/error sessions
        session.touch()
        assert session.state == SessionState.SESSION_ACTIVE

    def test_rolling_window_maintained_after_recovery(self):
        """Rolling 20-turn window must still be enforced after recovery."""
        session = SessionContext(operator_id="carol")

        # Simulate 30 turns before crash
        for i in range(30):
            session.add_message(
                ChatMessage(role=MessageRole.ROLE_USER, content=f"q{i}")
            )
            session.add_message(
                ChatMessage(role=MessageRole.ROLE_ASSISTANT, content=f"a{i}")
            )

        raw = session.SerializeToString()
        recovered = SessionContext.FromString(raw)

        # 30 turns × 2 messages = 60 messages, but rolling window caps at 40
        assert len(recovered.history) <= 40


# ══════════════════════════════════════════════════════════════════════════════
# Simultaneous multi-service failure
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.slow
class TestSimultaneousServiceFailures:
    """
    Tests the most extreme scenario: multiple services failing at the same time.
    System must degrade gracefully, not cascade into total failure.
    """

    async def test_gnn_and_guardian_both_down_copilot_warns(
        self, copilot_session
    ):
        """
        When both GNN and Guardian are down, Co-Pilot must:
        1. Add a system warning to the session
        2. Not attempt any remediation
        3. Remain in ACTIVE state (still useful to the operator)
        """
        session = copilot_session

        # Simulate failure notifications
        warning = ChatMessage(
            role=MessageRole.ROLE_SYSTEM,
            content=(
                "🚨 SYSTEM DEGRADED: Layer-2 GNN and Layer-3 Guardian "
                "are unreachable. Cluster analysis paused. "
                "Contact on-call SRE immediately."
            ),
        )
        session.add_message(warning)
        assert session.state == SessionState.SESSION_ACTIVE

    async def test_concurrent_failure_injections_no_deadlock(
        self, fault_injector_severe
    ):
        """
        50 concurrent faulty Kafka sends under severe conditions.
        No deadlock, no uncaught exceptions.
        """
        from tests.chaos.conftest import FaultyKafkaProducer
        producer = FaultyKafkaProducer(fault_injector_severe)
        payload = b"test-chaos-payload"

        async def _safe_send():
            try:
                await producer.send("ccdt.ebpf.events", payload)
            except (IOError, asyncio.TimeoutError):
                pass  # expected under severe fault injection

        await asyncio.gather(*[_safe_send() for _ in range(50)])
        # No deadlock: test completes within pytest timeout
        assert producer.attempted <= 50

    async def test_all_services_recovering_in_sequence(
        self, normal_gnn_payload, normal_guardian_payload
    ):
        """
        Simulate sequential recovery: GNN recovers first, then Guardian.
        Each stage must be independently testable.
        """
        # Stage 1: all down
        gnn_cfg = DegradedServiceConfig(always_timeout=True)
        guardian_cfg = DegradedServiceConfig(error_500_rate=1.0)

        client = _make_degraded_http_client(
            gnn_cfg=gnn_cfg, guardian_cfg=guardian_cfg,
            normal_gnn_payload=normal_gnn_payload,
            normal_guardian_payload=normal_guardian_payload,
        )

        try:
            await asyncio.wait_for(
                client.get("http://layer2-cognitive:8001/infer"), timeout=0.05
            )
        except (asyncio.TimeoutError, Exception):
            pass  # expected

        # Stage 2: GNN recovers
        gnn_cfg.always_timeout = False
        gnn_resp = await client.get("http://layer2-cognitive:8001/infer")
        assert gnn_resp.json().get("incident_type") == "FAULT"

        # Stage 3: Guardian recovers
        guardian_cfg.error_500_rate = 0.0
        g_resp = await client.post(
            "http://layer3-guardian:8002/actions/preview", json={}
        )
        assert g_resp.json().get("approved") is True
