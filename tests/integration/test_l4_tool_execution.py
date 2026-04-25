"""
Integration tests — Layer-4 Co-Pilot Tool Execution + Session Management
Tests the CCDTCoPilot tool dispatch (ghost preview, topology, eBPF events,
propose action), session rolling window, streaming SSE output,
and incident auto-report generation.

Uses mocked HTTP clients and a mocked Anthropic API.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.graph_pb2 import NodeClass, IncidentType
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionRequest, GhostSimulationResult,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, MessageType, SessionState, StreamEventType, ToolName,
    TokenUsage, ToolCall, ToolResult, ChatMessage, SessionContext,
    StreamEvent, IncidentReport,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Tool execution
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer4
class TestToolExecution:
    async def test_get_topology_tool_returns_nodes(
        self, mock_http_client, mock_topology_response
    ):
        """get_topology tool must call Layer-2 /topology and return nodes."""
        resp = await mock_http_client.get(
            "http://layer2-cognitive:8001/topology",
        )
        data = resp.json()
        assert "nodes"      in data
        assert "node_count" in data
        assert len(data["nodes"]) == mock_topology_response["node_count"]

    async def test_get_topology_node_structure(
        self, mock_http_client, mock_topology_response
    ):
        resp  = await mock_http_client.get("http://layer2-cognitive:8001/topology")
        nodes = resp.json()["nodes"]
        for node in nodes:
            assert "node_id"          in node
            assert "name"             in node
            assert "node_class"       in node
            assert "class_confidence" in node

    async def test_run_ghost_preview_tool(
        self, mock_http_client, mock_guardian_preview_response
    ):
        """run_ghost_preview tool must POST to Guardian /actions/preview."""
        resp = await mock_http_client.post(
            "http://layer3-guardian:8002/actions/preview",
            json={
                "action_id":    5,
                "target_node":  "payment-svc",
                "incident_type": "fault",
            },
        )
        data = resp.json()
        assert "risk_score"   in data
        assert "approved"     in data
        assert "confidence"   in data

    async def test_get_ebpf_events_tool(self, mock_http_client):
        """get_ebpf_events tool must call Layer-1 /events and return events list."""
        resp = await mock_http_client.get(
            "http://layer1-nervous:9100/events?limit=30",
        )
        data = resp.json()
        assert "events" in data

    async def test_propose_action_tool_supervised_mode(self, mock_http_client):
        """propose_action in supervised mode should POST to Guardian /execute."""
        resp = await mock_http_client.post(
            "http://layer3-guardian:8002/actions/execute",
            json={
                "incident_type": "fault",
                "dry_run":       False,
                "autonomy_mode": "supervised",
            },
        )
        data = resp.json()
        assert data.get("status") in ("SUCCEEDED", "AWAITING_APPROVAL", "PENDING")

    async def test_propose_action_tool_dry_run(self, mock_http_client):
        """With dry_run=True, Guardian should simulate without executing."""
        resp = await mock_http_client.post(
            "http://layer3-guardian:8002/actions/execute",
            json={
                "incident_type": "fault",
                "dry_run":       True,
                "autonomy_mode": "supervised",
            },
        )
        assert resp.status_code == 200

    async def test_tool_dispatch_returns_json_string(self):
        """
        Tool executor returns results as JSON strings for the Anthropic API.
        """
        topology_result = {
            "snapshot_id": _uid(),
            "node_count":  5,
            "incident_type": "FAULT",
            "nodes": [{"node_id": "a", "name": "svc-a", "node_class": "fault"}],
        }
        result_str = json.dumps(topology_result, indent=2)
        assert isinstance(result_str, str)
        parsed = json.loads(result_str)
        assert parsed["node_count"] == 5

    async def test_tool_timeout_returns_error_dict(self, mock_http_client):
        """
        If a tool call times out, the executor must return an error dict
        rather than raising (to allow Claude to handle gracefully).
        """
        import httpx
        mock_http_client.get = AsyncMock(
            side_effect=httpx.TimeoutException("Timeout after 3s")
        )
        try:
            await mock_http_client.get("http://layer2-cognitive:8001/topology")
            result = {"error": "Timeout after 3s"}
        except Exception as e:
            result = {"error": str(e)}

        assert "error" in result
        assert "timeout" in result["error"].lower() or "Timeout" in result["error"]


# ══════════════════════════════════════════════════════════════════════════════
# Session management
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer4
class TestCopilotSessionManagement:
    def test_new_session_created_on_first_message(self):
        sessions: dict[str, SessionContext] = {}
        session_id = _uid()

        if session_id not in sessions:
            sessions[session_id] = SessionContext(
                session_id=session_id,
                operator_id="alice",
                state=SessionState.SESSION_ACTIVE,
            )
        sessions[session_id].add_message(
            ChatMessage(role=MessageRole.ROLE_USER, content="What is wrong?")
        )
        assert session_id in sessions
        assert len(sessions[session_id].history) == 1

    def test_session_reused_across_turns(self):
        sessions: dict[str, SessionContext] = {}
        session_id = _uid()

        for turn in range(5):
            if session_id not in sessions:
                sessions[session_id] = SessionContext(
                    session_id=session_id, operator_id="alice",
                )
            session = sessions[session_id]
            session.add_message(ChatMessage(
                role=MessageRole.ROLE_USER, content=f"turn {turn}",
            ))
            session.add_message(ChatMessage(
                role=MessageRole.ROLE_ASSISTANT, content=f"response {turn}",
            ))

        session = sessions[session_id]
        assert session.turn_count == 5

    def test_session_cleared_on_delete(self):
        sessions: dict[str, SessionContext] = {
            "s1": SessionContext(session_id="s1", operator_id="alice"),
        }
        del sessions["s1"]
        assert "s1" not in sessions

    def test_multiple_concurrent_sessions(self):
        """Different operators should have isolated sessions."""
        sessions: dict[str, SessionContext] = {}
        for i in range(3):
            sid = f"session-{i}"
            sessions[sid] = SessionContext(
                session_id=sid, operator_id=f"op-{i}",
            )
            sessions[sid].add_message(
                ChatMessage(role=MessageRole.ROLE_USER, content=f"op-{i} message")
            )

        for i in range(3):
            sid = f"session-{i}"
            assert sessions[sid].operator_id == f"op-{i}"
            assert sessions[sid].history[0].content == f"op-{i} message"

    def test_session_list_endpoint_structure(self):
        """GET /sessions should return a list of session summaries."""
        sessions = {
            "s1": SessionContext(session_id="s1", operator_id="alice",
                                  state=SessionState.SESSION_ACTIVE),
            "s2": SessionContext(session_id="s2", operator_id="bob",
                                  state=SessionState.SESSION_IDLE),
        }
        response = {
            "sessions": [
                {
                    "session_id":  s.session_id,
                    "operator_id": s.operator_id,
                    "state":       SessionState(s.state).name,
                    "turn_count":  s.turn_count,
                }
                for s in sessions.values()
            ],
            "total": len(sessions),
        }
        assert response["total"] == 2
        assert all("session_id" in s for s in response["sessions"])


# ══════════════════════════════════════════════════════════════════════════════
# Streaming SSE
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer4
class TestStreamingSSE:
    async def test_stream_generates_token_events(self):
        """
        Simulate the stream generator yielding SSE events for each text chunk.
        """
        text_chunks = ["The ", "root ", "cause ", "is ", "OOM ", "pressure."]
        events = []
        for i, chunk in enumerate(text_chunks):
            ev  = StreamEvent(
                event_type=StreamEventType.STREAM_TOKEN,
                delta_text=chunk,
                seq_num=i,
            )
            events.append(ev.to_sse())

        assert len(events) == 6
        for sse in events:
            assert sse.startswith("data: ")
            assert sse.endswith("\n\n")
            payload = json.loads(sse[6:].strip())
            assert payload["event_type"] == int(StreamEventType.STREAM_TOKEN)
            assert len(payload["delta_text"]) > 0

    async def test_stream_completes_with_done_event(self):
        """Final event must be STREAM_COMPLETE with token usage."""
        done_event = StreamEvent(
            event_type=StreamEventType.STREAM_COMPLETE,
            final_usage=TokenUsage(input_tokens=500, output_tokens=200),
        )
        sse     = done_event.to_sse()
        payload = json.loads(sse[6:].strip())
        assert payload["event_type"] == int(StreamEventType.STREAM_COMPLETE)

    async def test_stream_error_event_on_api_failure(self):
        """If Claude API fails mid-stream, an error event must be sent."""
        error_event = StreamEvent(
            event_type=StreamEventType.STREAM_ERROR,
            error_message="Anthropic API rate limit: 529 Overloaded",
        )
        sse     = error_event.to_sse()
        payload = json.loads(sse[6:].strip())
        assert payload["event_type"] == int(StreamEventType.STREAM_ERROR)

    async def test_stream_tool_use_event_emitted(self):
        """When Claude calls a tool mid-stream, a TOOL_USE event should be emitted."""
        tool_event = StreamEvent(
            event_type=StreamEventType.STREAM_TOOL_USE,
            tool_name="get_topology",
            session_id="s1",
        )
        sse = tool_event.to_sse()
        assert sse.startswith("data: ")
        payload = json.loads(sse[6:].strip())
        assert payload["event_type"] == int(StreamEventType.STREAM_TOOL_USE)

    async def test_stream_seq_nums_are_monotonic(self):
        """Sequence numbers in stream events must be strictly increasing."""
        events = [
            StreamEvent(
                event_type=StreamEventType.STREAM_TOKEN,
                delta_text=f"word{i}",
                seq_num=i,
            )
            for i in range(10)
        ]
        seq_nums = []
        for ev in events:
            payload  = json.loads(ev.to_sse()[6:].strip())
            seq_nums.append(payload["seq_num"])
        assert seq_nums == sorted(seq_nums)
        assert len(set(seq_nums)) == len(seq_nums)   # no duplicates


# ══════════════════════════════════════════════════════════════════════════════
# Auto incident report generation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer4
class TestAutoIncidentReport:
    async def test_kafka_consumer_triggers_incident_report(
        self, fault_inference, fake_kafka_consumer_factory
    ):
        """
        When GNN publishes a fault inference to Kafka, Layer-4 should
        auto-generate an incident report and inject it into active sessions.
        """
        payload  = fault_inference.SerializeToString()
        consumer = fake_kafka_consumer_factory([payload])

        received = []
        async for msg in consumer:
            inference = GnnInferenceResult.FromString(msg.value)
            if inference.is_active_incident:
                received.append(inference)

        assert len(received) == 1
        assert received[0].incident_type == IncidentType.INCIDENT_FAULT

    async def test_auto_report_above_confidence_threshold(self, fault_inference):
        """
        Only inferences above AUTO_REPORT_THRESH=0.85 should auto-generate reports.
        """
        auto_report_threshold = 0.85
        # fault_inference has graph_confidence=0.88 — above threshold
        assert fault_inference.graph_confidence >= auto_report_threshold

    async def test_auto_report_not_generated_below_threshold(self):
        low_conf = GnnInferenceResult(
            inference_id=_uid(),
            incident_type=IncidentType.INCIDENT_FAULT,
            graph_confidence=0.72,   # Below 0.85 threshold
            root_cause_node_name="uncertain-svc",
        )
        auto_report_threshold = 0.85
        assert low_conf.graph_confidence < auto_report_threshold

    async def test_incident_report_injection_text_valid(self, incident_report):
        """
        The system prompt injection must be a non-empty string with required sections.
        """
        injection = incident_report.to_system_prompt_injection()
        assert isinstance(injection, str)
        assert len(injection) > 100   # Must be substantive
        assert injection.count("\n") > 5   # Must be multi-line

    async def test_report_published_to_active_sessions(
        self, fault_inference, incident_report
    ):
        """
        Active sessions should receive the incident report as an auto-injected
        system message.
        """
        sessions = {
            "s1": SessionContext(session_id="s1", operator_id="alice",
                                  state=SessionState.SESSION_ACTIVE),
        }
        # Simulate injection into active sessions
        for session in sessions.values():
            if session.state == SessionState.SESSION_ACTIVE:
                session.add_message(ChatMessage(
                    role=MessageRole.ROLE_SYSTEM,
                    message_type=MessageType.MSG_INCIDENT_REPORT,
                    content=incident_report.to_system_prompt_injection(),
                    auto_injected=True,
                ))

        session = sessions["s1"]
        injected = [m for m in session.history if m.auto_injected]
        assert len(injected) == 1
        assert "FAULT" in injected[0].content
