# ─────────────────────────────────────────────────────────────────────────────
# CCDT copilot_pb2.py — Pure-Python message shims for copilot.proto
# ─────────────────────────────────────────────────────────────────────────────
# Mirrors the full copilot.proto API with Python dataclasses.
# Run `make proto` to replace with protoc-compiled stubs.
#
# Message hierarchy:
#   ToolCallParameters, ToolCall, ToolResult   — Claude tool-use round-trip
#   TokenUsage                                  — API cost tracking
#   ChatMessage                                 — one turn in a conversation
#   SessionContext                              — rolling 20-turn session
#   ChatRequest / ChatResponse                  — REST request/response
#   StreamEvent                                 — SSE token stream events
#   IncidentReport                              — auto-injected from GNN ≥ 0.85
#   FinetuningExample                           — SFT/RLHF training record
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Dict, List, Optional

from ccdt.shared.proto.generated.graph_pb2 import (
    GnnInferenceResult,
    IncidentType,
    NodeClass,
    TopFeature,
)
from ccdt.shared.proto.generated.actions_pb2 import (
    ActionRequest,
    ActionResult,
    GhostSimulationResult,
)


# ── Enums ─────────────────────────────────────────────────────────────────────

class MessageRole(IntEnum):
    ROLE_UNKNOWN   = 0
    ROLE_USER      = 1
    ROLE_ASSISTANT = 2
    ROLE_SYSTEM    = 3
    ROLE_TOOL      = 4

    def label(self) -> str:
        return {1: "user", 2: "assistant", 3: "system", 4: "tool"}.get(self.value, "unknown")

    def anthropic_role(self) -> str:
        """Map to Anthropic API role string."""
        return {
            1: "user",
            2: "assistant",
            3: "user",    # system content is injected as a user message in context
            4: "user",
        }.get(self.value, "user")


class MessageType(IntEnum):
    MSG_UNKNOWN         = 0
    MSG_TEXT            = 1
    MSG_INCIDENT_REPORT = 2
    MSG_ACTION_PROPOSAL = 3
    MSG_ACTION_RESULT   = 4
    MSG_TOPOLOGY_EMBED  = 5
    MSG_TOOL_CALL       = 6
    MSG_TOOL_RESULT     = 7
    MSG_ERROR           = 8


class SessionState(IntEnum):
    SESSION_UNKNOWN = 0
    SESSION_ACTIVE  = 1
    SESSION_IDLE    = 2
    SESSION_CLOSED  = 3
    SESSION_ERROR   = 4

    @property
    def is_alive(self) -> bool:
        return self in (SessionState.SESSION_ACTIVE, SessionState.SESSION_IDLE)


class StreamEventType(IntEnum):
    STREAM_UNKNOWN     = 0
    STREAM_TOKEN       = 1
    STREAM_TOOL_USE    = 2
    STREAM_TOOL_RESULT = 3
    STREAM_COMPLETE    = 4
    STREAM_ERROR       = 5
    STREAM_INCIDENT    = 6
    STREAM_THINKING    = 7


class ToolName(IntEnum):
    TOOL_UNKNOWN              = 0
    TOOL_GET_TOPOLOGY         = 1
    TOOL_GET_EBPF_EVENTS      = 2
    TOOL_RUN_GHOST_PREVIEW    = 3
    TOOL_PROPOSE_ACTION       = 4
    TOOL_GET_LOGS             = 5
    TOOL_GET_METRICS          = 6
    TOOL_GET_INCIDENT_HISTORY = 7
    TOOL_SEARCH_RUNBOOKS      = 8

    def label(self) -> str:
        return self.name.replace("TOOL_", "").lower()

    @classmethod
    def from_string(cls, s: str) -> "ToolName":
        mapping = {
            "get_topology":         cls.TOOL_GET_TOPOLOGY,
            "get_ebpf_events":      cls.TOOL_GET_EBPF_EVENTS,
            "run_ghost_preview":    cls.TOOL_RUN_GHOST_PREVIEW,
            "propose_action":       cls.TOOL_PROPOSE_ACTION,
            "get_logs":             cls.TOOL_GET_LOGS,
            "get_metrics":          cls.TOOL_GET_METRICS,
            "get_incident_history": cls.TOOL_GET_INCIDENT_HISTORY,
            "search_runbooks":      cls.TOOL_SEARCH_RUNBOOKS,
        }
        return mapping.get(s.lower(), cls.TOOL_UNKNOWN)


# ── Base message mixin ────────────────────────────────────────────────────────

class _ProtoMessage:
    def SerializeToString(self) -> bytes:
        return json.dumps(self._to_dict(), default=str).encode("utf-8")

    @classmethod
    def FromString(cls, data: bytes):
        return cls._from_dict(json.loads(data.decode("utf-8")))

    def _to_dict(self) -> dict:
        result: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is None:
                continue
            if isinstance(v, _ProtoMessage):
                d = v._to_dict()
                if d:
                    result[k] = d
            elif hasattr(v, "_to_dict"):  # cross-module _ProtoMessage
                d = v._to_dict()
                if d:
                    result[k] = d
            elif isinstance(v, list):
                s = []
                for i in v:
                    if hasattr(i, "_to_dict"):
                        s.append(i._to_dict())
                    elif isinstance(i, IntEnum):
                        s.append(int(i))
                    else:
                        s.append(i)
                if s:
                    result[k] = s
            elif isinstance(v, dict):
                if v:
                    result[k] = v
            elif isinstance(v, IntEnum):
                if v.value != 0:
                    result[k] = int(v)
            elif isinstance(v, bool):
                if v:
                    result[k] = v
            elif isinstance(v, (int, float)):
                if v != 0:
                    result[k] = v
            elif isinstance(v, str):
                if v:
                    result[k] = v
        return result

    @classmethod
    def _from_dict(cls, d: dict):
        obj = cls.__new__(cls)
        obj.__init__()
        for k, v in d.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj

    def __repr__(self) -> str:
        d = self._to_dict()
        fields_str = ", ".join(f"{k}={v!r}" for k, v in list(d.items())[:5])
        return f"{self.__class__.__name__}({fields_str})"

    def ByteSize(self) -> int:
        return len(self.SerializeToString())


# ── Token usage ───────────────────────────────────────────────────────────────

@dataclass
class TokenUsage(_ProtoMessage):
    """
    Claude API token consumption for one API call.
    estimated_cost_usd is calculated at ~$3/M input, $15/M output (Sonnet 4).
    """
    input_tokens:       int   = 0
    output_tokens:      int   = 0
    cache_read_tokens:  int   = 0
    cache_write_tokens: int   = 0
    estimated_cost_usd: float = 0.0

    # Pricing for claude-sonnet-4 as of 2025
    _INPUT_COST_PER_M  = 3.00
    _OUTPUT_COST_PER_M = 15.00
    _CACHE_READ_PER_M  = 0.30
    _CACHE_WRITE_PER_M = 3.75

    def compute_cost(self) -> float:
        """Compute and store estimated_cost_usd from token counts."""
        self.estimated_cost_usd = (
            (self.input_tokens       / 1_000_000) * self._INPUT_COST_PER_M +
            (self.output_tokens      / 1_000_000) * self._OUTPUT_COST_PER_M +
            (self.cache_read_tokens  / 1_000_000) * self._CACHE_READ_PER_M +
            (self.cache_write_tokens / 1_000_000) * self._CACHE_WRITE_PER_M
        )
        return self.estimated_cost_usd

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        result = TokenUsage(
            input_tokens       = self.input_tokens       + other.input_tokens,
            output_tokens      = self.output_tokens      + other.output_tokens,
            cache_read_tokens  = self.cache_read_tokens  + other.cache_read_tokens,
            cache_write_tokens = self.cache_write_tokens + other.cache_write_tokens,
            estimated_cost_usd = self.estimated_cost_usd + other.estimated_cost_usd,
        )
        return result


# ── Tool call parameters ──────────────────────────────────────────────────────

@dataclass
class ToolCallParameters(_ProtoMessage):
    """
    Typed parameters for all tool calls.
    Only the fields relevant to the specific tool are populated.
    """
    # TOOL_GET_TOPOLOGY
    target_namespace:  str              = ""
    # TOOL_GET_EBPF_EVENTS
    node_name:         str              = ""
    event_type_filter: str              = ""
    limit:             int              = 50
    time_range_s:      float            = 300.0
    # TOOL_RUN_GHOST_PREVIEW / TOOL_PROPOSE_ACTION
    action_name:       str              = ""
    target_node:       str              = ""
    target_resource:   str              = ""
    action_params:     Dict[str, str]   = field(default_factory=dict)
    # TOOL_GET_LOGS
    pod_name:          str              = ""
    container_name:    str              = ""
    tail_lines:        int              = 100
    # TOOL_GET_METRICS
    promql_expr:       str              = ""
    lookback_minutes:  int              = 30
    # TOOL_SEARCH_RUNBOOKS
    search_query:      str              = ""


@dataclass
class ToolCall(_ProtoMessage):
    """A tool call made by Claude during a response."""
    tool_call_id: str                          = ""
    tool_name:    ToolName                     = ToolName.TOOL_UNKNOWN
    params_json:  str                          = ""   # raw JSON from Claude API
    params:       Optional[ToolCallParameters] = None
    called_at:    str                          = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @classmethod
    def from_api_response(
        cls, tool_call_id: str, name: str, input_json: str
    ) -> "ToolCall":
        """Build ToolCall from Claude API tool_use block."""
        try:
            raw = json.loads(input_json)
        except json.JSONDecodeError:
            raw = {}
        params = ToolCallParameters(**{
            k: v for k, v in raw.items()
            if k in ToolCallParameters.__dataclass_fields__
        })
        return cls(
            tool_call_id=tool_call_id,
            tool_name=ToolName.from_string(name),
            params_json=input_json,
            params=params,
        )


@dataclass
class ToolResult(_ProtoMessage):
    """Result returned to Claude after executing a tool call."""
    tool_call_id: str      = ""
    tool_name:    ToolName = ToolName.TOOL_UNKNOWN
    success:      bool     = False
    result_json:  str      = ""
    error:        str      = ""
    summary:      str      = ""
    duration_ms:  float    = 0.0
    returned_at:  str      = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_anthropic_content(self) -> dict:
        """Format for inclusion in Claude API messages array as tool_result."""
        return {
            "type":        "tool_result",
            "tool_use_id": self.tool_call_id,
            "content":     self.result_json if self.success else f"Error: {self.error}",
        }


# ── Chat message ──────────────────────────────────────────────────────────────

@dataclass
class ChatMessage(_ProtoMessage):
    """One turn in a Co-Pilot conversation."""
    message_id:         str                         = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    session_id:         str                         = ""
    role:               MessageRole                 = MessageRole.ROLE_USER
    message_type:       MessageType                 = MessageType.MSG_TEXT

    content:            str                         = ""
    tool_call:          Optional[ToolCall]          = None
    tool_result:        Optional[ToolResult]        = None

    inference_context:  Optional[GnnInferenceResult]  = None
    action_proposal:    Optional[ActionRequest]        = None
    action_result:      Optional[ActionResult]         = None

    model:              str                         = ""
    stop_reason:        str                         = ""
    token_usage:        Optional[TokenUsage]        = None

    created_at:         str                         = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    turn_index:         int                         = 0
    auto_injected:      bool                        = False
    api_latency_ms:     float                       = 0.0

    def to_anthropic_message(self) -> dict:
        """
        Convert to Anthropic API messages array format.
        Handles all content types: text, tool_use, tool_result.
        """
        role = MessageRole(self.role).anthropic_role()

        if self.tool_call is not None:
            # Claude requested a tool call (assistant message with tool_use)
            return {
                "role": "assistant",
                "content": [{
                    "type":  "tool_use",
                    "id":    self.tool_call.tool_call_id,
                    "name":  ToolName(self.tool_call.tool_name).label(),
                    "input": json.loads(self.tool_call.params_json or "{}"),
                }],
            }
        if self.tool_result is not None:
            # Tool result being fed back to Claude (user message with tool_result)
            return {
                "role":    "user",
                "content": [self.tool_result.to_anthropic_content()],
            }
        return {"role": role, "content": self.content}


# ── Session context ───────────────────────────────────────────────────────────

@dataclass
class SessionContext(_ProtoMessage):
    """
    One operator's rolling 20-turn conversation with the Co-Pilot.
    Sessions are stored in memory (Redis) with a 30-minute TTL.
    """
    session_id:          str                         = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    operator_id:         str                         = ""
    operator_name:       str                         = ""
    history:             List[ChatMessage]           = field(default_factory=list)
    active_incident_id:  str                         = ""
    latest_inference:    Optional[GnnInferenceResult] = None
    state:               SessionState                = SessionState.SESSION_ACTIVE
    created_at:          str                         = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_activity_at:    str                         = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    turn_count:          int                         = 0
    total_token_usage:   TokenUsage                  = field(default_factory=TokenUsage)
    system_prompt_hash:  str                         = ""

    _MAX_HISTORY_TURNS = 20

    def add_message(self, msg: ChatMessage) -> None:
        """
        Add a message to history, maintaining the rolling 20-turn window.
        Pair of (user, assistant) messages = 1 turn.
        """
        msg.session_id  = self.session_id
        msg.turn_index  = len(self.history)
        self.history.append(msg)
        self.turn_count = len(self.history) // 2
        self.last_activity_at = datetime.now(timezone.utc).isoformat()

        # Rolling window: keep most recent 40 messages (20 turns)
        max_messages = self._MAX_HISTORY_TURNS * 2
        if len(self.history) > max_messages:
            # Always preserve the first system message if present
            system_msgs = [m for m in self.history
                           if m.role == MessageRole.ROLE_SYSTEM]
            other_msgs  = [m for m in self.history
                           if m.role != MessageRole.ROLE_SYSTEM]
            self.history = system_msgs + other_msgs[-(max_messages - len(system_msgs)):]

        # Accumulate token usage
        if msg.token_usage:
            self.total_token_usage = self.total_token_usage + msg.token_usage

    def to_anthropic_messages(self) -> list[dict]:
        """Build the messages array for the Claude API call."""
        return [m.to_anthropic_message() for m in self.history
                if m.role != MessageRole.ROLE_SYSTEM]

    def set_system_prompt(self, prompt: str) -> None:
        """Store the system prompt hash for injection-detection."""
        self.system_prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

    def touch(self) -> None:
        """Update last_activity_at to now."""
        self.last_activity_at = datetime.now(timezone.utc).isoformat()
        if self.state == SessionState.SESSION_IDLE:
            self.state = SessionState.SESSION_ACTIVE


# ── Chat request / response ───────────────────────────────────────────────────

@dataclass
class ChatRequest(_ProtoMessage):
    session_id:       str  = ""   # empty = create new session
    operator_id:      str  = ""
    message:          str  = ""
    include_context:  bool = True
    force_tool:       str  = ""
    sent_at:          str  = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class ChatResponse(_ProtoMessage):
    session_id:   str                 = ""
    message_id:   str                 = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    content:      str                 = ""
    tool_calls:   List[ToolCall]      = field(default_factory=list)
    tool_results: List[ToolResult]    = field(default_factory=list)
    token_usage:  Optional[TokenUsage] = None
    stop_reason:  str                 = "end_turn"
    model:        str                 = ""
    latency_ms:   float               = 0.0
    tool_rounds:  int                 = 0
    responded_at: str                 = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ── Stream event ──────────────────────────────────────────────────────────────

@dataclass
class StreamEvent(_ProtoMessage):
    """
    Sent over SSE (Server-Sent Events) as Claude streams its response.
    The client accumulates STREAM_TOKEN events to display the response
    incrementally.  STREAM_COMPLETE signals the end with final token counts.
    """
    session_id:       str             = ""
    message_id:       str             = ""
    event_type:       StreamEventType = StreamEventType.STREAM_UNKNOWN
    delta_text:       str             = ""
    tool_call:        Optional[ToolCall]   = None
    tool_result:      Optional[ToolResult] = None
    final_usage:      Optional[TokenUsage] = None
    incident_summary: str             = ""
    error:            str             = ""
    thinking_delta:   str             = ""
    event_ts:         str             = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    seq_num:          int             = 0

    def to_sse(self) -> str:
        """Serialize to SSE wire format for HTTP streaming response."""
        payload = json.dumps(self._to_dict(), default=str)
        return f"data: {payload}\n\n"


# ── Incident report ───────────────────────────────────────────────────────────

@dataclass
class IncidentReport(_ProtoMessage):
    """
    Auto-generated when GNN confidence ≥ 0.85.
    Injected into the active Co-Pilot session as a ROLE_SYSTEM message
    with type MSG_INCIDENT_REPORT.
    """
    report_id:             str                           = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    detected_at:           str                           = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    inference_id:          str                           = ""
    incident_type:         IncidentType                  = IncidentType.INCIDENT_NONE
    graph_confidence:      float                         = 0.0

    root_cause_service:    str                           = ""
    root_cause_namespace:  str                           = ""
    root_cause_class:      NodeClass                     = NodeClass.NODE_CLASS_UNKNOWN

    affected_services:     int                           = 0
    affected_namespaces:   List[str]                     = field(default_factory=list)
    top_features:          List[TopFeature]              = field(default_factory=list)

    proposed_action:       Optional[ActionRequest]       = None
    ghost_result:          Optional[GhostSimulationResult] = None

    nl_summary:            str                           = ""
    severity:              str                           = "medium"
    incident_id:           str                           = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    schema_ver:            str                           = "1.0"

    def to_system_prompt_injection(self) -> str:
        """
        Format the incident report as a system message to inject into
        the Co-Pilot conversation context.
        """
        itype = IncidentType(self.incident_type).label()
        cls   = NodeClass(self.root_cause_class).label()
        features_str = "\n".join(
            f"  - {f.feature_name} on {f.node_name}: {f.explanation}"
            for f in self.top_features[:3]
        )
        action_str = ""
        if self.proposed_action:
            risk = ""
            if self.ghost_result:
                risk = f" (risk: {self.ghost_result.risk_score:.2f})"
            action_str = (
                f"\n\nProposed action: {self.proposed_action.short_desc()}{risk}"
            )

        return (
            f"🚨 INCIDENT DETECTED [{self.severity.upper()}]\n"
            f"Type: {itype} | Confidence: {self.graph_confidence:.0%}\n"
            f"Root cause: {self.root_cause_service} ({cls})\n"
            f"Affected: {self.affected_services} service(s)\n"
            f"Incident ID: {self.incident_id}\n"
            f"\nTop contributing signals:\n{features_str}"
            f"{action_str}\n\n"
            f"Summary: {self.nl_summary}"
        )


# ── Fine-tuning example ───────────────────────────────────────────────────────

@dataclass
class FinetuningExample(_ProtoMessage):
    """
    SFT/RLHF training record exported from a Co-Pilot session.
    Used by dataset_builder.py to build training datasets for fine-tuning.
    """
    example_id:              str               = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    messages:                List[ChatMessage]  = field(default_factory=list)
    human_approved:          bool              = False
    operator_rating:         int               = 0   # 1-5
    operator_comment:        str               = ""
    action_resolved_incident: bool             = False
    created_at:              str               = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_chatml(self) -> list[dict]:
        """Export as ChatML format for SFT training."""
        result = []
        for msg in self.messages:
            role = MessageRole(msg.role).label()
            result.append({"role": role, "content": msg.content})
        return result

    def quality_score(self) -> float:
        """Composite quality score for RLHF reward modelling."""
        score = 0.0
        if self.human_approved:
            score += 0.50
        if self.operator_rating > 0:
            score += (self.operator_rating / 5) * 0.30
        if self.action_resolved_incident:
            score += 0.20
        return min(1.0, score)


# ── gRPC service request/response helpers ─────────────────────────────────────

@dataclass
class GetSessionRequest(_ProtoMessage):
    session_id:  str = ""
    operator_id: str = ""

@dataclass
class ListSessionsRequest(_ProtoMessage):
    operator_id: str          = ""
    state:       SessionState = SessionState.SESSION_UNKNOWN

@dataclass
class ListSessionsResponse(_ProtoMessage):
    sessions: List[SessionContext] = field(default_factory=list)

@dataclass
class CloseSessionRequest(_ProtoMessage):
    session_id: str = ""
    reason:     str = ""

@dataclass
class CloseSessionResponse(_ProtoMessage):
    success:    bool = False
    session_id: str  = ""

@dataclass
class InjectResponse(_ProtoMessage):
    success:    bool = False
    message_id: str  = ""

@dataclass
class ExportRequest(_ProtoMessage):
    session_id:       str  = ""
    include_all_turns: bool = True

@dataclass
class CopilotHealthRequest(_ProtoMessage):
    pass

@dataclass
class CopilotHealthResponse(_ProtoMessage):
    healthy:            bool  = False
    status:             str   = ""
    claude_model:       str   = ""
    active_sessions:    int   = 0
    avg_response_ms:    float = 0.0
    total_tokens_today: int   = 0
