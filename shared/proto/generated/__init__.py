"""
CCDT Proto Generated — Python protobuf message shims.

These are pure-Python dataclass implementations that mirror the protobuf
message API exactly. Replace with protoc-compiled stubs by running:

    cd ccdt/shared && make proto

Contents
--------
    events_pb2     TypedEbpfBatch, CapabilityEvent, OomKillEvent,
                   TcpRetransmitEvent, SchedLatencyEvent, FileAccessEvent,
                   SyscallEvent, ExecveEvent, NetworkConnectEvent,
                   EventMetadata, EventSeverity, LinuxCapability
    graph_pb2      GnnInferenceResult, TopologyNode, TopologyEdge,
                   TopologySnapshot, NodeFeatures, EdgeFeatures,
                   CausalChainNode, TopFeature, CounterfactualResult,
                   NodeClass, IncidentType, NodeType, EdgeType
    actions_pb2    ActionRequest, ActionResult, GhostSimulationResult,
                   ActionHistoryEntry, ApprovalRequest, ApprovalResponse,
                   ActionName, ActionStatus, AutonomyMode, RiskCategory,
                   ScaleParameters, RollbackParameters, ExecParameters,
                   NetworkPolicyParameters, ResourceLimitParameters,
                   NodeParameters, SecretRotationParameters, HpaParameters
    copilot_pb2    ChatMessage, SessionContext, ChatRequest, ChatResponse,
                   StreamEvent, IncidentReport, FinetuningExample,
                   ToolCall, ToolResult, ToolCallParameters, TokenUsage,
                   MessageRole, MessageType, SessionState, ToolName,
                   StreamEventType
"""

# ── events ────────────────────────────────────────────────────────────────────
from ccdt.shared.proto.generated.events_pb2 import (
    LinuxCapability,
    EventSeverity,
    NetworkProtocol,
    SchedEventType,
    EventMetadata,
    CapabilityEvent,
    OomKillEvent,
    TcpRetransmitEvent,
    SchedLatencyEvent,
    FileAccessEvent,
    SyscallEvent,
    ExecveEvent,
    NetworkConnectEvent,
    TypedEbpfBatch,
)

# ── graph ─────────────────────────────────────────────────────────────────────
from ccdt.shared.proto.generated.graph_pb2 import (
    NodeClass,
    IncidentType,
    NodeType,
    EdgeType,
    CausalDirection,
    NodeFeatures,
    EdgeFeatures,
    TopologyNode,
    TopologyEdge,
    CausalChainNode,
    TopFeature,
    CounterfactualResult,
    GnnInferenceResult,
    TopologySnapshot,
    GetInferenceRequest,
    StreamInferenceRequest,
    GetTopologyRequest,
    CounterfactualQuery,
    CounterfactualResponse,
)

# ── actions ───────────────────────────────────────────────────────────────────
from ccdt.shared.proto.generated.actions_pb2 import (
    ActionName,
    ActionStatus,
    AutonomyMode,
    RiskCategory,
    ScaleParameters,
    RollbackParameters,
    ExecParameters,
    NetworkPolicyParameters,
    ResourceLimitParameters,
    NodeParameters,
    SecretRotationParameters,
    HpaParameters,
    GhostSimulationResult,
    ActionRequest,
    ActionResult,
    ActionHistoryEntry,
    ApprovalRequest,
    ApprovalResponse,
    ActionHistoryRequest,
    ActionHistoryResponse,
    StreamActionsRequest,
    PendingApprovalsRequest,
    PendingApprovalsResponse,
    HealthCheckRequest,
    HealthCheckResponse,
)

# ── copilot ───────────────────────────────────────────────────────────────────
from ccdt.shared.proto.generated.copilot_pb2 import (
    MessageRole,
    MessageType,
    SessionState,
    StreamEventType,
    ToolName,
    TokenUsage,
    ToolCallParameters,
    ToolCall,
    ToolResult,
    ChatMessage,
    SessionContext,
    ChatRequest,
    ChatResponse,
    StreamEvent,
    IncidentReport,
    FinetuningExample,
    GetSessionRequest,
    ListSessionsRequest,
    ListSessionsResponse,
    CloseSessionRequest,
    CloseSessionResponse,
    InjectResponse,
    ExportRequest,
    CopilotHealthRequest,
    CopilotHealthResponse,
)

__all__ = [
    # events_pb2
    "LinuxCapability", "EventSeverity", "NetworkProtocol", "SchedEventType",
    "EventMetadata", "CapabilityEvent", "OomKillEvent", "TcpRetransmitEvent",
    "SchedLatencyEvent", "FileAccessEvent", "SyscallEvent", "ExecveEvent",
    "NetworkConnectEvent", "TypedEbpfBatch",
    # graph_pb2
    "NodeClass", "IncidentType", "NodeType", "EdgeType", "CausalDirection",
    "NodeFeatures", "EdgeFeatures", "TopologyNode", "TopologyEdge",
    "CausalChainNode", "TopFeature", "CounterfactualResult",
    "GnnInferenceResult", "TopologySnapshot",
    "GetInferenceRequest", "StreamInferenceRequest", "GetTopologyRequest",
    "CounterfactualQuery", "CounterfactualResponse",
    # actions_pb2
    "ActionName", "ActionStatus", "AutonomyMode", "RiskCategory",
    "ScaleParameters", "RollbackParameters", "ExecParameters",
    "NetworkPolicyParameters", "ResourceLimitParameters",
    "NodeParameters", "SecretRotationParameters", "HpaParameters",
    "GhostSimulationResult", "ActionRequest", "ActionResult",
    "ActionHistoryEntry", "ApprovalRequest", "ApprovalResponse",
    "ActionHistoryRequest", "ActionHistoryResponse",
    "StreamActionsRequest", "PendingApprovalsRequest", "PendingApprovalsResponse",
    "HealthCheckRequest", "HealthCheckResponse",
    # copilot_pb2
    "MessageRole", "MessageType", "SessionState", "StreamEventType", "ToolName",
    "TokenUsage", "ToolCallParameters", "ToolCall", "ToolResult",
    "ChatMessage", "SessionContext", "ChatRequest", "ChatResponse",
    "StreamEvent", "IncidentReport", "FinetuningExample",
    "GetSessionRequest", "ListSessionsRequest", "ListSessionsResponse",
    "CloseSessionRequest", "CloseSessionResponse",
    "InjectResponse", "ExportRequest",
    "CopilotHealthRequest", "CopilotHealthResponse",
]
