"""
Tests for CCDT shared proto shims (graph_pb2, actions_pb2, copilot_pb2).
All tests work WITHOUT protoc — they use the pure-Python dataclass shims.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from ccdt.shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, CapabilityEvent, OomKillEvent, TcpRetransmitEvent,
    SchedLatencyEvent, FileAccessEvent, ExecveEvent, NetworkConnectEvent,
    EventMetadata, EventSeverity, LinuxCapability, NetworkProtocol,
)
from ccdt.shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, NodeFeatures, EdgeFeatures,
    TopologyNode, TopologyEdge, CausalChainNode, TopFeature,
    CounterfactualResult, GnnInferenceResult, TopologySnapshot,
)
from ccdt.shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    ScaleParameters, RollbackParameters, GhostSimulationResult,
    ActionRequest, ActionResult, ActionHistoryEntry,
    ApprovalRequest, ApprovalResponse,
)
from ccdt.shared.proto.generated.copilot_pb2 import (
    MessageRole, MessageType, SessionState, ToolName, StreamEventType,
    TokenUsage, ToolCallParameters, ToolCall, ToolResult,
    ChatMessage, SessionContext, ChatRequest, ChatResponse,
    StreamEvent, IncidentReport, FinetuningExample,
)


# ── Events ─────────────────────────────────────────────────────────────────────

class TestEventMetadata:
    def test_default_construction(self):
        meta = EventMetadata()
        assert meta.pid == 0
        assert meta.severity == EventSeverity.SEVERITY_INFO

    def test_now_factory(self):
        meta = EventMetadata.now("node-1", 1234, "nginx")
        assert meta.node_name == "node-1"
        assert meta.pid == 1234
        assert meta.comm == "nginx"
        assert meta.kernel_ts_ns > 0

    def test_serialize_deserialize(self):
        meta = EventMetadata.now("node-1", 42, "bash",
                                  namespace="default", pod_name="my-pod")
        raw = meta.SerializeToString()
        assert isinstance(raw, bytes)
        loaded = EventMetadata.FromString(raw)
        assert loaded.node_name == "node-1"
        assert loaded.namespace == "default"

    def test_labels_dict(self):
        meta = EventMetadata(labels={"env": "prod", "region": "us-east-1"})
        d = meta._to_dict()
        assert d["labels"]["env"] == "prod"


class TestTypedEbpfBatch:
    def _make_batch(self) -> TypedEbpfBatch:
        meta = EventMetadata.now("node-1", 100, "nginx")
        cap = CapabilityEvent(meta=meta, capability=LinuxCapability.CAP_NET_ADMIN, allowed=False)
        oom = OomKillEvent(meta=meta, victim_pid=200, victim_comm="oom-victim",
                           victim_rss_bytes=512*1024*1024)
        tcp = TcpRetransmitEvent(meta=meta, src_addr="10.0.0.1", dst_addr="10.0.0.2",
                                  retransmit_count=5, rtt_us=1000)
        return TypedEbpfBatch(
            batch_id=str(uuid.uuid4()),
            node_name="node-1",
            collector_id=str(uuid.uuid4()),
            batch_ts=datetime.now(timezone.utc).isoformat(),
            capability_events=[cap],
            oom_kill_events=[oom],
            tcp_retransmit_events=[tcp],
            schema_ver="1.0",
        )

    def test_total_events(self):
        batch = self._make_batch()
        assert batch.total_events() == 3

    def test_type_counts(self):
        batch = self._make_batch()
        batch.compute_type_counts()
        assert batch.type_counts["capability"] == 1
        assert batch.type_counts["oom_kill"] == 1
        assert batch.type_counts["tcp_retransmit"] == 1

    def test_serialize_roundtrip(self):
        batch = self._make_batch()
        raw = batch.SerializeToString()
        loaded = TypedEbpfBatch.FromString(raw)
        assert loaded.node_name == batch.node_name
        assert loaded.schema_ver == "1.0"

    def test_byte_size(self):
        batch = self._make_batch()
        assert batch.ByteSize() > 0


# ── Graph ──────────────────────────────────────────────────────────────────────

class TestNodeFeatures:
    def test_to_tensor_list_length(self):
        f = NodeFeatures(cpu_utilization=0.8, oom_kill_rate=0.3)
        t = f.to_tensor_list()
        assert len(t) == 16

    def test_tensor_order(self):
        f = NodeFeatures(cpu_utilization=0.5)
        t = f.to_tensor_list()
        assert t[0] == 0.5  # cpu_utilization is index 0

    def test_anomaly_score_range(self):
        f = NodeFeatures(
            syscall_anomaly_score=1.0,
            capability_event_rate=1.0,
            oom_kill_rate=1.0,
        )
        score = f.anomaly_score()
        assert 0.0 <= score <= 1.0

    def test_anomaly_score_zero_for_healthy(self):
        f = NodeFeatures()
        assert f.anomaly_score() == 0.0


class TestTopologyNode:
    def test_summary_healthy(self):
        n = TopologyNode(
            node_id="svc-1",
            name="payment-svc",
            node_class=NodeClass.NODE_CLASS_HEALTHY,
            class_confidence=0.95,
        )
        s = n.summary()
        assert "payment-svc" in s
        assert "HEALTHY" in s
        assert "ROOT CAUSE" not in s

    def test_summary_root_cause(self):
        n = TopologyNode(
            node_id="svc-2",
            name="db-svc",
            node_class=NodeClass.NODE_CLASS_FAULT,
            class_confidence=0.9,
            is_root_cause=True,
        )
        s = n.summary()
        assert "ROOT CAUSE" in s
        assert "FAULT" in s

    def test_is_anomalous_above_threshold(self):
        f = NodeFeatures(syscall_anomaly_score=0.8)
        n = TopologyNode(node_id="x", name="x", features=f)
        assert n.is_anomalous(threshold=0.5) is True

    def test_is_anomalous_below_threshold(self):
        n = TopologyNode(node_id="x", name="x", features=NodeFeatures())
        assert n.is_anomalous(threshold=0.5) is False


class TestGnnInferenceResult:
    def _make_inference(self) -> GnnInferenceResult:
        nodes = [
            TopologyNode(
                node_id="svc-a", name="auth-svc",
                node_class=NodeClass.NODE_CLASS_FAULT,
                class_confidence=0.92,
                is_root_cause=True,
                features=NodeFeatures(oom_kill_rate=0.9, cpu_utilization=0.85),
            ),
            TopologyNode(
                node_id="svc-b", name="api-gw",
                node_class=NodeClass.NODE_CLASS_HEALTHY,
                class_confidence=0.88,
            ),
        ]
        return GnnInferenceResult(
            inference_id=str(uuid.uuid4()),
            incident_type=IncidentType.INCIDENT_FAULT,
            graph_confidence=0.87,
            root_cause_node_id="svc-a",
            root_cause_node_name="auth-svc",
            root_cause_confidence=0.92,
            blast_radius_node_ids=["svc-b", "svc-c"],
            blast_radius_count=2,
            node_classifications=nodes,
            node_count=2,
            edge_count=1,
            inference_latency_ms=38.7,
        )

    def test_is_active_incident(self):
        inf = self._make_inference()
        assert inf.is_active_incident is True

    def test_is_not_active_for_heartbeat(self):
        inf = GnnInferenceResult(
            incident_type=IncidentType.INCIDENT_NONE,
            is_heartbeat=True,
            graph_confidence=0.0,
        )
        assert inf.is_active_incident is False

    def test_severity_high(self):
        inf = self._make_inference()
        assert inf.severity == "high"

    def test_severity_critical_for_attack(self):
        inf = GnnInferenceResult(
            incident_type=IncidentType.INCIDENT_ATTACK,
            graph_confidence=0.90,
        )
        assert inf.severity == "critical"

    def test_fault_nodes(self):
        inf = self._make_inference()
        assert len(inf.fault_nodes()) == 1
        assert inf.fault_nodes()[0].name == "auth-svc"

    def test_nl_summary_active(self):
        inf = self._make_inference()
        s = inf.nl_summary()
        assert "auth-svc" in s
        assert "FAULT" in s
        assert "2 nodes" in s

    def test_nl_summary_heartbeat(self):
        inf = GnnInferenceResult(
            incident_type=IncidentType.INCIDENT_NONE,
            is_heartbeat=True,
            node_count=15,
            inference_latency_ms=12.0,
        )
        s = inf.nl_summary()
        assert "healthy" in s.lower()

    def test_serialize_roundtrip(self):
        inf = self._make_inference()
        raw = inf.SerializeToString()
        loaded = GnnInferenceResult.FromString(raw)
        assert loaded.root_cause_node_name == "auth-svc"
        assert loaded.graph_confidence == pytest.approx(0.87)


class TestTopologySnapshot:
    def test_node_count(self):
        snap = TopologySnapshot(
            nodes=[TopologyNode(node_id="a", name="a"),
                   TopologyNode(node_id="b", name="b")],
        )
        assert snap.node_count == 2

    def test_get_node_found(self):
        snap = TopologySnapshot(
            nodes=[TopologyNode(node_id="svc-1", name="auth")]
        )
        node = snap.get_node("svc-1")
        assert node is not None
        assert node.name == "auth"

    def test_get_node_not_found(self):
        snap = TopologySnapshot(nodes=[])
        assert snap.get_node("missing") is None

    def test_get_neighbors(self):
        snap = TopologySnapshot(
            nodes=[
                TopologyNode(node_id="a", name="a"),
                TopologyNode(node_id="b", name="b"),
                TopologyNode(node_id="c", name="c"),
            ],
            edges=[
                TopologyEdge(edge_id="e1", source_node_id="a", target_node_id="b"),
                TopologyEdge(edge_id="e2", source_node_id="a", target_node_id="c"),
            ],
        )
        neighbors = snap.get_neighbors("a")
        names = {n.name for n in neighbors}
        assert names == {"b", "c"}


# ── Actions ────────────────────────────────────────────────────────────────────

class TestRiskCategory:
    @pytest.mark.parametrize("score,expected", [
        (0.10, RiskCategory.RISK_VERY_LOW),
        (0.20, RiskCategory.RISK_LOW),
        (0.45, RiskCategory.RISK_MEDIUM),
        (0.65, RiskCategory.RISK_HIGH),
        (0.90, RiskCategory.RISK_VERY_HIGH),
    ])
    def test_from_score(self, score, expected):
        assert RiskCategory.from_score(score) == expected

    def test_requires_human_approval_high(self):
        assert RiskCategory.RISK_HIGH.requires_human_approval is True

    def test_requires_human_approval_low(self):
        assert RiskCategory.RISK_LOW.requires_human_approval is False


class TestGhostSimulationResult:
    def test_is_safe_approved(self):
        ghost = GhostSimulationResult(
            risk_score=0.20,
            risk_category=RiskCategory.RISK_LOW,
            confidence=0.85,
            opa_approved=True,
        )
        assert ghost.is_safe is True

    def test_is_safe_denied(self):
        ghost = GhostSimulationResult(
            risk_score=0.20,
            opa_approved=False,
        )
        assert ghost.is_safe is False

    def test_summary_contains_key_fields(self):
        ghost = GhostSimulationResult(
            risk_score=0.15, risk_category=RiskCategory.RISK_LOW,
            confidence=0.9, opa_approved=True, affected_pod_count=3,
        )
        s = ghost.summary()
        assert "0.15" in s
        assert "APPROVED" in s
        assert "3" in s


class TestActionRequest:
    def _make_request(self) -> ActionRequest:
        return ActionRequest(
            action_name=ActionName.ACTION_RESTART_POD,
            action_label="restart_pod",
            target_node_name="auth-svc-pod-abc",
            target_namespace="default",
            inference_id=str(uuid.uuid4()),
            trigger_class=NodeClass.NODE_CLASS_FAULT,
            trigger_confidence=0.88,
            autonomy_mode=AutonomyMode.AUTONOMY_SUPERVISED,
            ghost_result=GhostSimulationResult(
                risk_score=0.12,
                risk_category=RiskCategory.RISK_VERY_LOW,
                confidence=0.9,
                opa_approved=True,
            ),
            scale=ScaleParameters(
                deployment_name="auth-svc",
                current_replicas=2,
                target_replicas=3,
                namespace="default",
            ),
        )

    def test_get_parameters_returns_scale(self):
        req = self._make_request()
        params = req.get_parameters()
        assert isinstance(params, ScaleParameters)

    def test_requires_human_approval_supervised(self):
        req = self._make_request()
        assert req.requires_human_approval() is True

    def test_does_not_require_approval_full_auto_low_risk(self):
        req = ActionRequest(
            action_name=ActionName.ACTION_RESTART_POD,
            autonomy_mode=AutonomyMode.AUTONOMY_FULL_AUTO,
            ghost_result=GhostSimulationResult(
                risk_score=0.10,
                risk_category=RiskCategory.RISK_VERY_LOW,
                opa_approved=True,
            ),
        )
        assert req.requires_human_approval() is False

    def test_short_desc(self):
        req = self._make_request()
        d = req.short_desc()
        assert "restart_pod" in d
        assert "auth-svc-pod-abc" in d

    def test_serialize_roundtrip(self):
        req = self._make_request()
        raw = req.SerializeToString()
        loaded = ActionRequest.FromString(raw)
        assert loaded.target_namespace == "default"
        assert loaded.trigger_confidence == pytest.approx(0.88)


class TestActionResult:
    def test_succeeded_property(self):
        r = ActionResult(status=ActionStatus.STATUS_SUCCEEDED)
        assert r.succeeded is True

    def test_denied_property(self):
        r = ActionResult(status=ActionStatus.STATUS_DENIED)
        assert r.denied is True

    def test_summary_with_request(self):
        req = ActionRequest(
            action_name=ActionName.ACTION_ROLLBACK_DEPLOYMENT,
            target_node_name="payment-svc",
        )
        result = ActionResult(
            request=req,
            status=ActionStatus.STATUS_SUCCEEDED,
            execution_duration_ms=1250,
        )
        s = result.summary()
        assert "SUCCEEDED" in s


class TestAutonomyMode:
    def test_from_string(self):
        assert AutonomyMode.from_string("supervised") == AutonomyMode.AUTONOMY_SUPERVISED
        assert AutonomyMode.from_string("full-auto") == AutonomyMode.AUTONOMY_FULL_AUTO
        assert AutonomyMode.from_string("unknown-mode") == AutonomyMode.AUTONOMY_UNKNOWN

    def test_label(self):
        assert AutonomyMode.AUTONOMY_HUMAN_IN_LOOP.label() == "human-in-loop"


# ── Copilot ────────────────────────────────────────────────────────────────────

class TestTokenUsage:
    def test_compute_cost(self):
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = usage.compute_cost()
        assert cost > 0
        assert cost == pytest.approx(usage.estimated_cost_usd)

    def test_addition(self):
        a = TokenUsage(input_tokens=100, output_tokens=50)
        b = TokenUsage(input_tokens=200, output_tokens=100)
        c = a + b
        assert c.input_tokens == 300
        assert c.output_tokens == 150

    def test_total_tokens(self):
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        assert usage.total_tokens == 1500

    def test_zero_usage(self):
        usage = TokenUsage()
        assert usage.total_tokens == 0
        assert usage.compute_cost() == 0.0


class TestChatMessage:
    def test_to_anthropic_text(self):
        msg = ChatMessage(
            role=MessageRole.ROLE_USER,
            content="What is the root cause of this incident?",
        )
        am = msg.to_anthropic_message()
        assert am["role"] == "user"
        assert am["content"] == "What is the root cause of this incident?"

    def test_to_anthropic_tool_use(self):
        tc = ToolCall(
            tool_call_id="tc_123",
            tool_name=ToolName.TOOL_GET_TOPOLOGY,
            params_json='{"target_namespace": "default"}',
        )
        msg = ChatMessage(role=MessageRole.ROLE_ASSISTANT, tool_call=tc)
        am = msg.to_anthropic_message()
        assert am["role"] == "assistant"
        assert am["content"][0]["type"] == "tool_use"
        assert am["content"][0]["id"] == "tc_123"

    def test_to_anthropic_tool_result(self):
        tr = ToolResult(
            tool_call_id="tc_123",
            tool_name=ToolName.TOOL_GET_TOPOLOGY,
            success=True,
            result_json='{"nodes": []}',
        )
        msg = ChatMessage(role=MessageRole.ROLE_TOOL, tool_result=tr)
        am = msg.to_anthropic_message()
        assert am["role"] == "user"
        assert am["content"][0]["type"] == "tool_result"
        assert am["content"][0]["tool_use_id"] == "tc_123"


class TestSessionContext:
    def _make_session(self) -> SessionContext:
        return SessionContext(
            session_id=str(uuid.uuid4()),
            operator_id="alice",
            operator_name="Alice Smith",
        )

    def test_add_message_increments_turn_count(self):
        session = self._make_session()
        session.add_message(ChatMessage(role=MessageRole.ROLE_USER, content="hello"))
        session.add_message(ChatMessage(role=MessageRole.ROLE_ASSISTANT, content="hi there"))
        assert session.turn_count == 1
        assert len(session.history) == 2

    def test_rolling_window_enforced(self):
        session = self._make_session()
        # Add 22 turns (44 messages) — should trim to 40
        for i in range(22):
            session.add_message(ChatMessage(role=MessageRole.ROLE_USER, content=f"q{i}"))
            session.add_message(ChatMessage(role=MessageRole.ROLE_ASSISTANT, content=f"a{i}"))
        assert len(session.history) <= 40

    def test_to_anthropic_messages_excludes_system(self):
        session = self._make_session()
        session.add_message(ChatMessage(
            role=MessageRole.ROLE_SYSTEM,
            content="You are an AIOps assistant.",
        ))
        session.add_message(ChatMessage(
            role=MessageRole.ROLE_USER,
            content="Hello",
        ))
        msgs = session.to_anthropic_messages()
        assert all(m["role"] != "system" for m in msgs)

    def test_set_system_prompt_hash(self):
        session = self._make_session()
        session.set_system_prompt("You are an AIOps assistant.")
        assert len(session.system_prompt_hash) == 16

    def test_accumulates_token_usage(self):
        session = self._make_session()
        msg = ChatMessage(
            role=MessageRole.ROLE_ASSISTANT,
            content="analysis",
            token_usage=TokenUsage(input_tokens=100, output_tokens=50),
        )
        session.add_message(msg)
        assert session.total_token_usage.input_tokens == 100

    def test_touch_activates_idle_session(self):
        session = self._make_session()
        session.state = SessionState.SESSION_IDLE
        session.touch()
        assert session.state == SessionState.SESSION_ACTIVE


class TestToolCall:
    def test_from_api_response(self):
        tc = ToolCall.from_api_response(
            "tc_abc",
            "get_logs",
            '{"pod_name": "auth-pod-xyz", "tail_lines": 50}',
        )
        assert tc.tool_name == ToolName.TOOL_GET_LOGS
        assert tc.tool_call_id == "tc_abc"
        assert tc.params.pod_name == "auth-pod-xyz"
        assert tc.params.tail_lines == 50


class TestStreamEvent:
    def test_token_event_sse(self):
        ev = StreamEvent(
            session_id="s1",
            message_id="m1",
            event_type=StreamEventType.STREAM_TOKEN,
            delta_text="Hello ",
            seq_num=1,
        )
        sse = ev.to_sse()
        assert sse.startswith("data: ")
        assert sse.endswith("\n\n")
        payload = json.loads(sse[6:].strip())
        assert payload["delta_text"] == "Hello "

    def test_complete_event(self):
        ev = StreamEvent(
            event_type=StreamEventType.STREAM_COMPLETE,
            final_usage=TokenUsage(input_tokens=500, output_tokens=200),
        )
        sse = ev.to_sse()
        assert "STREAM_COMPLETE" not in sse  # event_type is serialized as int
        payload = json.loads(sse[6:].strip())
        assert payload["event_type"] == int(StreamEventType.STREAM_COMPLETE)


class TestIncidentReport:
    def test_to_system_prompt_injection(self):
        report = IncidentReport(
            incident_type=IncidentType.INCIDENT_ATTACK,
            graph_confidence=0.91,
            root_cause_service="payment-svc",
            root_cause_namespace="prod",
            root_cause_class=NodeClass.NODE_CLASS_ATTACK,
            affected_services=4,
            nl_summary="Possible lateral movement detected in payment service.",
            severity="critical",
            top_features=[
                TopFeature(
                    feature_name="capability_event_rate",
                    node_name="payment-svc",
                    explanation="CAP_NET_ADMIN checked 12x/s — 10× above baseline",
                )
            ],
        )
        injection = report.to_system_prompt_injection()
        assert "CRITICAL" in injection
        assert "ATTACK" in injection
        assert "payment-svc" in injection
        assert "91%" in injection
        assert "lateral movement" in injection
        assert "CAP_NET_ADMIN" in injection


class TestFinetuningExample:
    def test_quality_score_all_positive(self):
        ex = FinetuningExample(
            human_approved=True,
            operator_rating=5,
            action_resolved_incident=True,
        )
        assert ex.quality_score() == pytest.approx(1.0)

    def test_quality_score_unapproved(self):
        ex = FinetuningExample(
            human_approved=False,
            operator_rating=3,
            action_resolved_incident=False,
        )
        score = ex.quality_score()
        assert 0.0 < score < 1.0

    def test_to_chatml(self):
        messages = [
            ChatMessage(role=MessageRole.ROLE_USER, content="What's wrong?"),
            ChatMessage(role=MessageRole.ROLE_ASSISTANT, content="Root cause is OOM."),
        ]
        ex = FinetuningExample(messages=messages, human_approved=True)
        chatml = ex.to_chatml()
        assert chatml[0]["role"] == "user"
        assert chatml[1]["role"] == "assistant"
        assert chatml[1]["content"] == "Root cause is OOM."


# ── Cross-module integration ───────────────────────────────────────────────────

class TestCrossModuleIntegration:
    def test_action_request_uses_gnn_node_class(self):
        req = ActionRequest(
            action_name=ActionName.ACTION_ISOLATE_CONTAINER,
            trigger_class=NodeClass.NODE_CLASS_ATTACK,
            trigger_confidence=0.95,
        )
        assert req.trigger_class == NodeClass.NODE_CLASS_ATTACK
        raw = req.SerializeToString()
        loaded = ActionRequest.FromString(raw)
        assert loaded.trigger_confidence == pytest.approx(0.95)

    def test_incident_report_embeds_action_request(self):
        action = ActionRequest(
            action_name=ActionName.ACTION_ISOLATE_CONTAINER,
            target_node_name="attack-svc",
            ghost_result=GhostSimulationResult(risk_score=0.25, opa_approved=True),
        )
        report = IncidentReport(
            incident_type=IncidentType.INCIDENT_ATTACK,
            graph_confidence=0.88,
            proposed_action=action,
            ghost_result=GhostSimulationResult(risk_score=0.25, opa_approved=True),
            severity="critical",
        )
        injection = report.to_system_prompt_injection()
        assert "isolate_container" in injection.lower()
        assert "0.25" in injection

    def test_session_with_inference_context(self):
        inference = GnnInferenceResult(
            incident_type=IncidentType.INCIDENT_FAULT,
            graph_confidence=0.82,
            root_cause_node_name="db-svc",
        )
        msg = ChatMessage(
            role=MessageRole.ROLE_SYSTEM,
            message_type=MessageType.MSG_INCIDENT_REPORT,
            content=inference.nl_summary(),
            inference_context=inference,
            auto_injected=True,
        )
        session = SessionContext(operator_id="bob")
        session.add_message(msg)
        assert len(session.history) == 1
        assert session.history[0].auto_injected is True
