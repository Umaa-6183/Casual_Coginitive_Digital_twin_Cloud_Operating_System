"""
End-to-End tests — Co-Pilot Operator Session
Tests a realistic multi-turn operator conversation:
  Turn 1: Operator asks what is wrong → Claude calls get_topology
  Turn 2: Operator asks to run ghost preview → Claude calls run_ghost_preview
  Turn 3: Operator approves action → Claude calls propose_action
  Turn 4: Operator asks for incident summary → Claude generates report
  Turn 5: Session context is preserved across turns (rolling window)

All Anthropic API calls are mocked.
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
    ActionName, ActionStatus, ActionRequest, ActionResult,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, MessageType, SessionState, StreamEventType, ToolName,
    TokenUsage, ToolCall, ToolResult, ChatMessage, SessionContext,
    StreamEvent, IncidentReport, FinetuningExample,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Multi-turn operator session
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer4
class TestOperatorCopilotSession:
    """
    Simulates a complete operator session from incident detection to resolution.
    """

    def setup_method(self):
        self.session_id = _uid()
        self.session    = SessionContext(
            session_id=self.session_id,
            operator_id="operator:alice",
            operator_name="Alice Smith",
            state=SessionState.SESSION_ACTIVE,
            created_at=_now(),
        )

    def test_turn1_topology_request(self, mock_topology_response):
        """
        Turn 1: Operator asks 'What is wrong?' →
        Claude calls get_topology, returns node status summary.
        """
        # User message
        user_msg = ChatMessage(
            role=MessageRole.ROLE_USER,
            content="What is currently wrong with the cluster?",
            turn_index=0,
            created_at=_now(),
        )
        self.session.add_message(user_msg)

        # Simulated Claude response with tool use
        tool_call = ToolCall(
            tool_call_id="tc_001",
            tool_name=ToolName.TOOL_GET_TOPOLOGY,
            params_json='{"target_namespace":"production"}',
            called_at=_now(),
        )
        assistant_tool_msg = ChatMessage(
            role=MessageRole.ROLE_ASSISTANT,
            tool_call=tool_call,
        )
        self.session.add_message(assistant_tool_msg)

        # Tool result
        tool_result = ToolResult(
            tool_call_id="tc_001",
            tool_name=ToolName.TOOL_GET_TOPOLOGY,
            success=True,
            result_json=json.dumps(mock_topology_response),
            duration_ms=42.0,
        )
        tool_result_msg = ChatMessage(role=MessageRole.ROLE_TOOL, tool_result=tool_result)
        self.session.add_message(tool_result_msg)

        # Final assistant reply
        reply_msg = ChatMessage(
            role=MessageRole.ROLE_ASSISTANT,
            content="I can see that **payment-svc** is classified as FAULT "
                    "(91% confidence). It's experiencing OOM kills at 4× baseline. "
                    "The blast radius is 1 downstream service (api-gateway).",
            token_usage=TokenUsage(input_tokens=820, output_tokens=95),
        )
        self.session.add_message(reply_msg)

        assert self.session.turn_count >= 1
        assert len(self.session.history) == 4

    def test_turn2_ghost_preview_request(self, mock_guardian_preview_response):
        """
        Turn 2: Operator asks 'What happens if we restart payment-svc?' →
        Claude calls run_ghost_preview.
        """
        user_msg = ChatMessage(
            role=MessageRole.ROLE_USER,
            content="What would happen if we restart payment-svc?",
            turn_index=1,
        )
        self.session.add_message(user_msg)

        tool_call = ToolCall(
            tool_call_id="tc_002",
            tool_name=ToolName.TOOL_RUN_GHOST_PREVIEW,
            params_json=json.dumps({
                "action_id": 5,
                "target_node": "payment-svc",
                "incident_type": "fault",
            }),
        )
        self.session.add_message(
            ChatMessage(role=MessageRole.ROLE_ASSISTANT, tool_call=tool_call)
        )
        self.session.add_message(
            ChatMessage(role=MessageRole.ROLE_TOOL, tool_result=ToolResult(
                tool_call_id="tc_002",
                success=True,
                result_json=json.dumps(mock_guardian_preview_response),
            ))
        )
        self.session.add_message(
            ChatMessage(
                role=MessageRole.ROLE_ASSISTANT,
                content="Ghost Preview results: Restarting payment-svc would:\n"
                        "- Risk score: 12% (VERY LOW) ✅\n"
                        "- OPA: Approved by all 5 policies ✅\n"
                        "- MTTR reduction: ~3 minutes\n"
                        "- Traffic impact: ~5% during restart\n\n"
                        "I recommend proceeding. Would you like me to execute this?",
                token_usage=TokenUsage(input_tokens=950, output_tokens=130),
            )
        )
        assert self.session.turn_count >= 1

    def test_turn3_operator_approves_action(self):
        """
        Turn 3: Operator says 'Yes, do it' → Claude calls propose_action.
        """
        user_msg = ChatMessage(
            role=MessageRole.ROLE_USER,
            content="Yes, go ahead and restart it.",
        )
        self.session.add_message(user_msg)

        tool_call = ToolCall(
            tool_call_id="tc_003",
            tool_name=ToolName.TOOL_PROPOSE_ACTION,
            params_json=json.dumps({
                "action_id":    5,
                "target_node":  "payment-svc",
                "incident_type": "fault",
                "dry_run":      False,
            }),
        )
        self.session.add_message(
            ChatMessage(role=MessageRole.ROLE_ASSISTANT, tool_call=tool_call)
        )
        self.session.add_message(
            ChatMessage(role=MessageRole.ROLE_TOOL, tool_result=ToolResult(
                tool_call_id="tc_003",
                success=True,
                result_json=json.dumps({
                    "audit_id": _uid(),
                    "status":   "SUCCEEDED",
                    "message":  "Pod payment-svc-pod-abc123 deleted — K8s recreating",
                }),
            ))
        )
        self.session.add_message(
            ChatMessage(
                role=MessageRole.ROLE_ASSISTANT,
                content="Action executed successfully ✅\n\n"
                        "Pod `payment-svc-pod-abc123` has been restarted. "
                        "The controller will recreate it within ~30 seconds. "
                        "I'll monitor the OOM kill rate to confirm the fix.",
                token_usage=TokenUsage(input_tokens=1050, output_tokens=85),
            )
        )
        assert any("SUCCEEDED" in str(m.tool_result.result_json)
                   for m in self.session.history
                   if m.role == MessageRole.ROLE_TOOL and m.tool_result)

    def test_session_history_integrity_across_turns(self):
        """Session history must be preserved correctly across all turns."""
        # Add 3 complete turns (6 messages)
        for turn in range(3):
            self.session.add_message(ChatMessage(
                role=MessageRole.ROLE_USER,
                content=f"User turn {turn}",
                turn_index=turn,
            ))
            self.session.add_message(ChatMessage(
                role=MessageRole.ROLE_ASSISTANT,
                content=f"Assistant reply {turn}",
                token_usage=TokenUsage(input_tokens=100, output_tokens=50),
            ))

        assert self.session.turn_count    == 3
        assert len(self.session.history)  == 6
        assert self.session.total_token_usage.input_tokens  == 300
        assert self.session.total_token_usage.output_tokens == 150

    def test_anthropic_messages_format_for_api(self):
        """
        The messages list sent to Claude must alternate user/assistant roles.
        """
        for turn in range(3):
            self.session.add_message(ChatMessage(
                role=MessageRole.ROLE_USER, content=f"question {turn}"
            ))
            self.session.add_message(ChatMessage(
                role=MessageRole.ROLE_ASSISTANT, content=f"answer {turn}"
            ))

        msgs = self.session.to_anthropic_messages()
        roles = [m["role"] for m in msgs]
        # Must start with user and alternate
        assert roles[0] == "user"
        for i in range(len(roles) - 1):
            assert roles[i] != roles[i + 1], \
                f"Adjacent same roles at positions {i} and {i+1}: {roles[i]}"


# ══════════════════════════════════════════════════════════════════════════════
# Incident report generation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer4
class TestIncidentReportGeneration:
    def test_fault_incident_report_structure(self, fault_inference, incident_report):
        """Generated report must contain all required sections."""
        injection = incident_report.to_system_prompt_injection()

        # Required sections
        assert "[INCIDENT"   in injection or "INCIDENT" in injection
        assert "FAULT"       in injection
        assert "payment-svc" in injection
        assert "88"          in injection   # 88% confidence
        assert "oom_kill"    in injection   # Top feature

    def test_attack_report_severity_critical(self, attack_inference):
        report = IncidentReport(
            incident_type=IncidentType.INCIDENT_ATTACK,
            graph_confidence=0.93,
            root_cause_service="auth-svc",
            root_cause_class=NodeClass.NODE_CLASS_ATTACK,
            severity="critical",
            nl_summary="Container escape attempt in auth-svc.",
            top_features=attack_inference.top_features,
        )
        injection = report.to_system_prompt_injection()
        assert "CRITICAL" in injection.upper()

    def test_report_proposed_action_included(self, incident_report):
        injection = incident_report.to_system_prompt_injection()
        assert "restart_pod" in injection.lower()
        assert "0.12"        in injection   # risk_score

    def test_report_no_proposed_action_when_unset(self, fault_inference):
        report = IncidentReport(
            incident_type=IncidentType.INCIDENT_FAULT,
            graph_confidence=0.88,
            root_cause_service="payment-svc",
            severity="high",
            nl_summary="OOM pressure in payment-svc.",
        )
        injection = report.to_system_prompt_injection()
        assert "FAULT" in injection
        # Should not crash when no action is set


# ══════════════════════════════════════════════════════════════════════════════
# Finetuning data export
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer4
class TestFinetuningDataExport:
    def test_high_quality_session_exports_as_finetuning_example(
        self, copilot_session, succeeded_action_result
    ):
        """
        Sessions with operator_rating=5 + action resolved the incident
        should be exported as high-quality fine-tuning examples.
        """
        ex = FinetuningExample(
            messages=copilot_session.history,
            human_approved=True,
            operator_rating=5,
            action_resolved_incident=True,
            incident_type="FAULT",
            recommended_action="restart_pod",
        )
        assert ex.quality_score()     == pytest.approx(1.0)
        assert len(ex.to_chatml())    >= 2

    def test_low_quality_session_filtered_out(self, copilot_session):
        """Sessions with rating ≤ 2 and no resolution should have low quality."""
        ex = FinetuningExample(
            messages=copilot_session.history,
            human_approved=False,
            operator_rating=1,
            action_resolved_incident=False,
        )
        assert ex.quality_score() < 0.4

    def test_chatml_format_matches_openai_spec(self, copilot_session):
        """ChatML format must match OpenAI fine-tuning data format."""
        ex     = FinetuningExample(messages=copilot_session.history, human_approved=True)
        chatml = ex.to_chatml()
        for msg in chatml:
            assert "role"    in msg
            assert "content" in msg
            assert msg["role"] in ("user", "assistant", "system", "tool")

    def test_quality_threshold_for_export(self, copilot_session):
        """Only examples with quality_score >= 0.7 should be exported."""
        export_threshold = 0.70
        high_quality = FinetuningExample(
            messages=copilot_session.history,
            human_approved=True,
            operator_rating=4,
            action_resolved_incident=True,
        )
        low_quality = FinetuningExample(
            messages=copilot_session.history,
            human_approved=False,
            operator_rating=2,
            action_resolved_incident=False,
        )
        assert high_quality.quality_score() >= export_threshold
        assert low_quality.quality_score()  <  export_threshold
