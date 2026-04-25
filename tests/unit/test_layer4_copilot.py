"""
Unit tests — Layer-4 Co-Pilot (LLM Interface)
Tests SessionContext rolling window, ChatMessage Anthropic format conversion,
TokenUsage cost tracking, StreamEvent SSE serialization,
IncidentReport injection text, ToolCall parsing, and FinetuningExample scoring.

All tests are network-free.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.graph_pb2 import NodeClass, IncidentType, TopFeature
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionRequest, GhostSimulationResult, RiskCategory,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, MessageType, SessionState, StreamEventType, ToolName,
    TokenUsage, ToolCallParameters, ToolCall, ToolResult,
    ChatMessage, SessionContext, ChatRequest, ChatResponse,
    StreamEvent, IncidentReport, FinetuningExample,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# TokenUsage
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer4
class TestTokenUsage:
    def test_zero_usage(self):
        u = TokenUsage()
        assert u.total_tokens   == 0
        assert u.compute_cost() == 0.0

    def test_total_tokens(self):
        u = TokenUsage(input_tokens=1000, output_tokens=500)
        assert u.total_tokens == 1500

    def test_total_tokens_with_cache(self):
        u = TokenUsage(input_tokens=1000, output_tokens=500,
                       cache_read_tokens=200, cache_write_tokens=100)
        assert u.total_tokens == 1800

    def test_compute_cost_positive(self):
        u = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = u.compute_cost()
        assert cost > 0.0

    def test_compute_cost_sets_estimated_cost_usd(self):
        u = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = u.compute_cost()
        assert u.estimated_cost_usd == pytest.approx(cost)

    def test_addition_operator(self):
        a = TokenUsage(input_tokens=100, output_tokens=50,
                       cache_read_tokens=20, cache_write_tokens=10)
        b = TokenUsage(input_tokens=200, output_tokens=100,
                       cache_read_tokens=30, cache_write_tokens=15)
        c = a + b
        assert c.input_tokens        == 300
        assert c.output_tokens       == 150
        assert c.cache_read_tokens   == 50
        assert c.cache_write_tokens  == 25

    def test_addition_accumulates_cost(self):
        a = TokenUsage(input_tokens=1000, output_tokens=500)
        b = TokenUsage(input_tokens=1000, output_tokens=500)
        a.compute_cost()
        b.compute_cost()
        c = a + b
        assert c.estimated_cost_usd == pytest.approx(a.estimated_cost_usd + b.estimated_cost_usd)

    def test_cost_output_more_expensive_than_input(self):
        """Output tokens (generated) cost more than input tokens per token."""
        input_only  = TokenUsage(input_tokens=1000,  output_tokens=0)
        output_only = TokenUsage(input_tokens=0, output_tokens=1000)
        assert output_only.compute_cost() > input_only.compute_cost()

    def test_serialize_roundtrip(self):
        u    = TokenUsage(input_tokens=2048, output_tokens=512, cache_read_tokens=100)
        raw  = u.SerializeToString()
        back = TokenUsage.FromString(raw)
        assert back.input_tokens      == 2048
        assert back.output_tokens     == 512
        assert back.cache_read_tokens == 100


# ══════════════════════════════════════════════════════════════════════════════
# ChatMessage — Anthropic format conversion
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer4
class TestChatMessage:
    def test_user_text_to_anthropic(self):
        msg = ChatMessage(role=MessageRole.ROLE_USER, content="What is wrong?")
        am  = msg.to_anthropic_message()
        assert am["role"]    == "user"
        assert am["content"] == "What is wrong?"

    def test_assistant_text_to_anthropic(self):
        msg = ChatMessage(role=MessageRole.ROLE_ASSISTANT, content="Root cause is OOM.")
        am  = msg.to_anthropic_message()
        assert am["role"]    == "assistant"
        assert am["content"] == "Root cause is OOM."

    def test_tool_use_to_anthropic(self):
        tc  = ToolCall(tool_call_id="tc_001", tool_name=ToolName.TOOL_GET_TOPOLOGY,
                       params_json='{"target_namespace":"production"}')
        msg = ChatMessage(role=MessageRole.ROLE_ASSISTANT, tool_call=tc)
        am  = msg.to_anthropic_message()
        assert am["role"]                  == "assistant"
        assert am["content"][0]["type"]    == "tool_use"
        assert am["content"][0]["id"]      == "tc_001"
        assert am["content"][0]["name"]    == "get_topology"

    def test_tool_result_to_anthropic(self):
        tr  = ToolResult(tool_call_id="tc_001", tool_name=ToolName.TOOL_GET_TOPOLOGY,
                         success=True, result_json='{"nodes":[]}')
        msg = ChatMessage(role=MessageRole.ROLE_TOOL, tool_result=tr)
        am  = msg.to_anthropic_message()
        assert am["role"]                        == "user"
        assert am["content"][0]["type"]          == "tool_result"
        assert am["content"][0]["tool_use_id"]   == "tc_001"

    def test_tool_result_error_to_anthropic(self):
        tr  = ToolResult(tool_call_id="tc_002", success=False,
                         error="GNN service timeout after 3s")
        msg = ChatMessage(role=MessageRole.ROLE_TOOL, tool_result=tr)
        am  = msg.to_anthropic_message()
        assert "error" in am["content"][0]["content"].lower() or \
               am["content"][0]["is_error"] is True

    def test_system_message_excluded_from_anthropic(self):
        """System messages are handled separately and not part of the messages array."""
        msg = ChatMessage(role=MessageRole.ROLE_SYSTEM, content="You are an AIOps assistant.")
        am  = msg.to_anthropic_message()
        assert am["role"] == "system"

    def test_auto_injected_flag(self):
        msg = ChatMessage(
            role=MessageRole.ROLE_SYSTEM,
            content="[INCIDENT ALERT] ...",
            auto_injected=True,
        )
        assert msg.auto_injected is True

    def test_turn_index_tracked(self):
        msg = ChatMessage(
            role=MessageRole.ROLE_USER,
            content="Question",
            turn_index=5,
        )
        raw  = msg.SerializeToString()
        back = ChatMessage.FromString(raw)
        assert back.turn_index == 5

    def test_token_usage_embedded(self):
        msg = ChatMessage(
            role=MessageRole.ROLE_ASSISTANT,
            content="Analysis done.",
            token_usage=TokenUsage(input_tokens=500, output_tokens=150),
        )
        raw  = msg.SerializeToString()
        back = ChatMessage.FromString(raw)
        assert back.token_usage.input_tokens  == 500
        assert back.token_usage.output_tokens == 150


# ══════════════════════════════════════════════════════════════════════════════
# ToolCall parsing
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer4
class TestToolCall:
    @pytest.mark.parametrize("tool_name_str,expected_enum", [
        ("get_topology",          ToolName.TOOL_GET_TOPOLOGY),
        ("get_ebpf_events",       ToolName.TOOL_GET_EBPF_EVENTS),
        ("run_ghost_preview",     ToolName.TOOL_RUN_GHOST_PREVIEW),
        ("propose_action",        ToolName.TOOL_PROPOSE_ACTION),
        ("get_logs",              ToolName.TOOL_GET_LOGS),
        ("get_metrics",           ToolName.TOOL_GET_METRICS),
        ("get_incident_history",  ToolName.TOOL_GET_INCIDENT_HISTORY),
        ("search_runbooks",       ToolName.TOOL_SEARCH_RUNBOOKS),
    ])
    def test_from_api_response_tool_names(self, tool_name_str, expected_enum):
        tc = ToolCall.from_api_response(
            tool_call_id=f"tc_{tool_name_str}",
            tool_name=tool_name_str,
            params_json="{}",
        )
        assert tc.tool_name    == expected_enum
        assert tc.tool_call_id == f"tc_{tool_name_str}"

    def test_from_api_response_params_parsed(self):
        params = {"target_namespace": "production", "node_id": "svc-payment"}
        tc = ToolCall.from_api_response(
            tool_call_id="tc_1",
            tool_name="get_topology",
            params_json=json.dumps(params),
        )
        assert tc.params.target_namespace == "production"
        assert tc.params.node_id          == "svc-payment"

    def test_from_api_response_ghost_preview_params(self):
        params = {"action_id": 5, "target_node": "payment-svc", "incident_type": "fault"}
        tc = ToolCall.from_api_response(
            tool_call_id="tc_ghost",
            tool_name="run_ghost_preview",
            params_json=json.dumps(params),
        )
        assert tc.params.action_id    == 5
        assert tc.params.target_node  == "payment-svc"
        assert tc.params.incident_type == "fault"

    def test_from_api_response_ebpf_events_params(self):
        params = {"event_type": "capability", "limit": 50}
        tc = ToolCall.from_api_response(
            tool_call_id="tc_ebpf",
            tool_name="get_ebpf_events",
            params_json=json.dumps(params),
        )
        assert tc.params.event_type == "capability"
        assert tc.params.limit      == 50

    def test_serialize_roundtrip(self):
        tc   = ToolCall(tool_call_id="tc_999", tool_name=ToolName.TOOL_PROPOSE_ACTION,
                        params_json='{"action_id":1,"dry_run":true}')
        raw  = tc.SerializeToString()
        back = ToolCall.FromString(raw)
        assert back.tool_call_id == "tc_999"
        assert back.tool_name    == ToolName.TOOL_PROPOSE_ACTION


# ══════════════════════════════════════════════════════════════════════════════
# SessionContext
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer4
class TestSessionContext:
    def test_initial_state(self, copilot_session):
        assert copilot_session.state         == SessionState.SESSION_ACTIVE
        assert copilot_session.operator_id   == "operator:alice"
        assert len(copilot_session.history)  == 2  # from conftest

    def test_add_message_increments_history(self, copilot_session):
        before = len(copilot_session.history)
        copilot_session.add_message(
            ChatMessage(role=MessageRole.ROLE_USER, content="follow-up question")
        )
        assert len(copilot_session.history) == before + 1

    def test_turn_count_increments_on_assistant_reply(self, copilot_session):
        """turn_count tracks complete user+assistant pairs."""
        initial_turns = copilot_session.turn_count
        copilot_session.add_message(ChatMessage(role=MessageRole.ROLE_USER, content="q"))
        copilot_session.add_message(ChatMessage(role=MessageRole.ROLE_ASSISTANT, content="a"))
        assert copilot_session.turn_count == initial_turns + 1

    def test_rolling_window_enforced_at_20_turns(self):
        """History must not exceed 40 messages (20 complete turns)."""
        session = SessionContext(operator_id="test-op")
        for i in range(25):
            session.add_message(ChatMessage(role=MessageRole.ROLE_USER, content=f"q{i}"))
            session.add_message(ChatMessage(role=MessageRole.ROLE_ASSISTANT, content=f"a{i}"))
        assert len(session.history) <= 40

    def test_rolling_window_keeps_most_recent(self):
        """After trimming, the most recent messages must be retained."""
        session = SessionContext(operator_id="test-op")
        for i in range(25):
            session.add_message(ChatMessage(role=MessageRole.ROLE_USER, content=f"q{i}"))
            session.add_message(ChatMessage(role=MessageRole.ROLE_ASSISTANT, content=f"a{i}"))
        last_user_content = f"q24"
        user_contents = [m.content for m in session.history
                         if m.role == MessageRole.ROLE_USER]
        assert last_user_content in user_contents

    def test_to_anthropic_messages_excludes_system(self, copilot_session):
        copilot_session.add_message(ChatMessage(
            role=MessageRole.ROLE_SYSTEM, content="[INCIDENT] ..."
        ))
        msgs = copilot_session.to_anthropic_messages()
        assert all(m["role"] != "system" for m in msgs)

    def test_to_anthropic_messages_alternates_roles(self, copilot_session):
        msgs = copilot_session.to_anthropic_messages()
        roles = [m["role"] for m in msgs]
        for i in range(len(roles) - 1):
            assert roles[i] != roles[i + 1], f"Adjacent same roles at {i}: {roles}"

    def test_set_system_prompt_hash(self, copilot_session):
        copilot_session.set_system_prompt("You are an AIOps assistant with real-time access.")
        assert len(copilot_session.system_prompt_hash) == 16

    def test_set_system_prompt_consistent_hash(self, copilot_session):
        copilot_session.set_system_prompt("same prompt text")
        h1 = copilot_session.system_prompt_hash
        copilot_session.set_system_prompt("same prompt text")
        h2 = copilot_session.system_prompt_hash
        assert h1 == h2

    def test_token_usage_accumulates(self):
        session = SessionContext(operator_id="test-op")
        for _ in range(3):
            session.add_message(ChatMessage(
                role=MessageRole.ROLE_ASSISTANT,
                content="response",
                token_usage=TokenUsage(input_tokens=100, output_tokens=50),
            ))
        assert session.total_token_usage.input_tokens  == 300
        assert session.total_token_usage.output_tokens == 150

    def test_touch_activates_idle_session(self):
        session = SessionContext(operator_id="test-op", state=SessionState.SESSION_IDLE)
        session.touch()
        assert session.state == SessionState.SESSION_ACTIVE

    def test_touch_does_not_reopen_closed_session(self):
        session = SessionContext(operator_id="test-op", state=SessionState.SESSION_CLOSED)
        session.touch()
        assert session.state == SessionState.SESSION_CLOSED

    def test_active_incident_tracking(self, copilot_session):
        incident_id = _uid()
        copilot_session.active_incident_id = incident_id
        raw  = copilot_session.SerializeToString()
        back = SessionContext.FromString(raw)
        assert back.active_incident_id == incident_id

    def test_session_serialize_roundtrip(self, copilot_session):
        raw  = copilot_session.SerializeToString()
        back = SessionContext.FromString(raw)
        assert back.operator_id   == copilot_session.operator_id
        assert back.session_id    == copilot_session.session_id
        assert len(back.history)  == len(copilot_session.history)


# ══════════════════════════════════════════════════════════════════════════════
# StreamEvent
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer4
class TestStreamEvent:
    def test_token_event_sse_format(self):
        ev  = StreamEvent(
            session_id="s1", message_id="m1",
            event_type=StreamEventType.STREAM_TOKEN,
            delta_text="Hello ",
            seq_num=1,
        )
        sse = ev.to_sse()
        assert sse.startswith("data: ")
        assert sse.endswith("\n\n")

    def test_token_event_payload(self):
        ev      = StreamEvent(event_type=StreamEventType.STREAM_TOKEN, delta_text="world")
        sse     = ev.to_sse()
        payload = json.loads(sse[6:].strip())
        assert payload["delta_text"]  == "world"
        assert payload["event_type"]  == int(StreamEventType.STREAM_TOKEN)

    def test_complete_event_has_usage(self):
        ev = StreamEvent(
            event_type=StreamEventType.STREAM_COMPLETE,
            final_usage=TokenUsage(input_tokens=500, output_tokens=200),
        )
        sse     = ev.to_sse()
        payload = json.loads(sse[6:].strip())
        assert payload["event_type"] == int(StreamEventType.STREAM_COMPLETE)

    def test_error_event(self):
        ev = StreamEvent(
            event_type=StreamEventType.STREAM_ERROR,
            error_message="Claude API rate limit exceeded",
        )
        sse     = ev.to_sse()
        payload = json.loads(sse[6:].strip())
        assert payload["event_type"] == int(StreamEventType.STREAM_ERROR)

    def test_tool_call_event(self):
        ev = StreamEvent(
            event_type=StreamEventType.STREAM_TOOL_USE,
            tool_name="get_topology",
        )
        sse = ev.to_sse()
        assert sse.startswith("data: ")

    def test_seq_num_increases(self):
        events = [
            StreamEvent(event_type=StreamEventType.STREAM_TOKEN,
                        delta_text=f"chunk{i}", seq_num=i)
            for i in range(5)
        ]
        for i, ev in enumerate(events):
            payload = json.loads(ev.to_sse()[6:].strip())
            assert payload["seq_num"] == i

    def test_empty_delta_is_valid(self):
        ev  = StreamEvent(event_type=StreamEventType.STREAM_TOKEN, delta_text="")
        sse = ev.to_sse()
        assert len(sse) > 0


# ══════════════════════════════════════════════════════════════════════════════
# IncidentReport
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer4
class TestIncidentReport:
    def test_to_system_prompt_injection_contains_severity(self, incident_report):
        injection = incident_report.to_system_prompt_injection()
        assert "HIGH" in injection.upper() or "high" in injection.lower()

    def test_to_system_prompt_injection_contains_incident_type(self, incident_report):
        injection = incident_report.to_system_prompt_injection()
        assert "FAULT" in injection

    def test_to_system_prompt_injection_contains_root_cause(self, incident_report):
        injection = incident_report.to_system_prompt_injection()
        assert incident_report.root_cause_service in injection

    def test_to_system_prompt_injection_contains_confidence(self, incident_report):
        injection = incident_report.to_system_prompt_injection()
        # 88% or 0.88 should appear
        assert "88" in injection

    def test_to_system_prompt_injection_contains_top_features(self, incident_report):
        injection = incident_report.to_system_prompt_injection()
        assert "oom_kill_rate" in injection

    def test_to_system_prompt_injection_contains_proposed_action(self, incident_report):
        injection = incident_report.to_system_prompt_injection()
        assert "restart_pod" in injection.lower()

    def test_to_system_prompt_injection_contains_ghost_risk(self, incident_report):
        injection = incident_report.to_system_prompt_injection()
        assert "0.12" in injection or "12" in injection

    def test_to_system_prompt_injection_attack_is_critical(self, attack_inference):
        report = IncidentReport(
            incident_type=IncidentType.INCIDENT_ATTACK,
            graph_confidence=0.93,
            root_cause_service=attack_inference.root_cause_node_name,
            root_cause_class=NodeClass.NODE_CLASS_ATTACK,
            affected_services=3,
            severity="critical",
            nl_summary="Lateral movement in auth-svc.",
            top_features=attack_inference.top_features,
        )
        injection = report.to_system_prompt_injection()
        assert "CRITICAL" in injection.upper()
        assert "ATTACK"   in injection

    def test_serialize_roundtrip(self, incident_report):
        raw  = incident_report.SerializeToString()
        back = IncidentReport.FromString(raw)
        assert back.root_cause_service == incident_report.root_cause_service
        assert back.graph_confidence   == pytest.approx(incident_report.graph_confidence)
        assert back.severity           == incident_report.severity


# ══════════════════════════════════════════════════════════════════════════════
# FinetuningExample
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer4
class TestFinetuningExample:
    def test_quality_score_perfect(self):
        ex = FinetuningExample(
            human_approved=True, operator_rating=5, action_resolved_incident=True
        )
        assert ex.quality_score() == pytest.approx(1.0)

    def test_quality_score_zero(self):
        ex = FinetuningExample(
            human_approved=False, operator_rating=1, action_resolved_incident=False
        )
        score = ex.quality_score()
        assert 0.0 <= score < 0.5

    def test_quality_score_partial(self):
        ex = FinetuningExample(
            human_approved=True, operator_rating=3, action_resolved_incident=False
        )
        score = ex.quality_score()
        assert 0.0 < score < 1.0

    def test_quality_score_range_always_0_to_1(self):
        for approved in (True, False):
            for rating in range(1, 6):
                for resolved in (True, False):
                    ex = FinetuningExample(
                        human_approved=approved,
                        operator_rating=rating,
                        action_resolved_incident=resolved,
                    )
                    score = ex.quality_score()
                    assert 0.0 <= score <= 1.0

    def test_to_chatml_correct_roles(self, copilot_session):
        messages = copilot_session.history[:2]
        ex       = FinetuningExample(messages=messages, human_approved=True, operator_rating=4)
        chatml   = ex.to_chatml()
        assert chatml[0]["role"] == "user"
        assert chatml[1]["role"] == "assistant"

    def test_to_chatml_content_preserved(self, copilot_session):
        messages = copilot_session.history[:2]
        ex       = FinetuningExample(messages=messages, human_approved=True)
        chatml   = ex.to_chatml()
        assert "payment service" in chatml[0]["content"].lower()

    def test_serialize_roundtrip(self, copilot_session):
        ex  = FinetuningExample(
            messages=copilot_session.history[:2],
            human_approved=True,
            operator_rating=5,
            action_resolved_incident=True,
        )
        raw  = ex.SerializeToString()
        back = FinetuningExample.FromString(raw)
        assert len(back.messages)        == 2
        assert back.human_approved       is True
        assert back.operator_rating      == 5
        assert back.action_resolved_incident is True
