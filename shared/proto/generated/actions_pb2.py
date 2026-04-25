# ─────────────────────────────────────────────────────────────────────────────
# CCDT actions_pb2.py — Pure-Python message shims for actions.proto
# ─────────────────────────────────────────────────────────────────────────────
# Mirrors the full actions.proto API with Python dataclasses.
# Run `make proto` to replace with protoc-compiled stubs.
#
# Message hierarchy:
#   Parameter types:
#       ScaleParameters, RollbackParameters, ExecParameters,
#       NetworkPolicyParameters, ResourceLimitParameters,
#       NodeParameters, SecretRotationParameters, HpaParameters
#   GhostSimulationResult — dry-run simulation + OPA check output
#   ActionRequest         — proposed action (RL policy → OPA gate → executor)
#   ActionResult          — execution outcome (published to Kafka)
#   ActionHistoryEntry    — for OPA lateral_movement retry-loop detection
#   ApprovalRequest/Response — human approval workflow
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Dict, List, Optional

# Import graph types for trigger context
from ccdt.shared.proto.generated.graph_pb2 import NodeClass


# ── Enums ─────────────────────────────────────────────────────────────────────

class ActionName(IntEnum):
    ACTION_UNKNOWN               = 0
    # Pod-level
    ACTION_RESTART_POD           = 1
    ACTION_EVICT_POD             = 2
    ACTION_KILL_POD              = 3
    ACTION_EXEC_COMMAND          = 4
    # Deployment-level
    ACTION_SCALE_UP_REPLICAS     = 10
    ACTION_SCALE_DOWN_REPLICAS   = 11
    ACTION_ROLLBACK_DEPLOYMENT   = 12
    ACTION_PATCH_RESOURCE_LIMITS = 13
    ACTION_PAUSE_DEPLOYMENT      = 14
    ACTION_RESUME_DEPLOYMENT     = 15
    ACTION_UPDATE_HPA_BOUNDS     = 16
    # Node-level
    ACTION_CORDON_NODE           = 20
    ACTION_UNCORDON_NODE         = 21
    ACTION_DRAIN_NODE            = 22
    # Network-level
    ACTION_ISOLATE_CONTAINER     = 30
    ACTION_REMOVE_ISOLATION      = 31
    ACTION_APPLY_NETWORK_POLICY  = 32
    # Secret / Config
    ACTION_ROTATE_SECRET         = 40
    ACTION_PATCH_CONFIGMAP       = 41
    # HPA
    ACTION_THROTTLE_CPU          = 50
    ACTION_INCREASE_OOM_THRESHOLD = 51

    def label(self) -> str:
        """Snake-case label for Kafka messages and Prometheus labels."""
        return self.name.replace("ACTION_", "").lower()

    @property
    def is_destructive(self) -> bool:
        """Actions that permanently remove or kill workloads."""
        return self in (
            ActionName.ACTION_KILL_POD,
            ActionName.ACTION_DRAIN_NODE,
            ActionName.ACTION_PAUSE_DEPLOYMENT,
        )

    @property
    def is_network(self) -> bool:
        return self in (
            ActionName.ACTION_ISOLATE_CONTAINER,
            ActionName.ACTION_REMOVE_ISOLATION,
            ActionName.ACTION_APPLY_NETWORK_POLICY,
        )


class ActionStatus(IntEnum):
    STATUS_UNKNOWN           = 0
    STATUS_PENDING           = 1
    STATUS_APPROVED          = 2
    STATUS_DENIED            = 3
    STATUS_EXECUTING         = 4
    STATUS_SUCCEEDED         = 5
    STATUS_FAILED            = 6
    STATUS_TIMEOUT           = 7
    STATUS_ROLLED_BACK       = 8
    STATUS_AWAITING_APPROVAL = 9

    def label(self) -> str:
        return self.name.replace("STATUS_", "")

    @property
    def is_terminal(self) -> bool:
        return self in (
            ActionStatus.STATUS_SUCCEEDED,
            ActionStatus.STATUS_FAILED,
            ActionStatus.STATUS_DENIED,
            ActionStatus.STATUS_TIMEOUT,
            ActionStatus.STATUS_ROLLED_BACK,
        )


class AutonomyMode(IntEnum):
    AUTONOMY_UNKNOWN       = 0
    AUTONOMY_SUPERVISED    = 1   # human approves everything
    AUTONOMY_HUMAN_IN_LOOP = 2   # auto for low-risk, human for high-risk
    AUTONOMY_FULL_AUTO     = 3

    def label(self) -> str:
        return {
            0: "unknown",
            1: "supervised",
            2: "human-in-loop",
            3: "full-auto",
        }[self.value]

    @classmethod
    def from_string(cls, s: str) -> "AutonomyMode":
        mapping = {
            "supervised":    cls.AUTONOMY_SUPERVISED,
            "human-in-loop": cls.AUTONOMY_HUMAN_IN_LOOP,
            "full-auto":     cls.AUTONOMY_FULL_AUTO,
        }
        return mapping.get(s.lower(), cls.AUTONOMY_UNKNOWN)


class RiskCategory(IntEnum):
    RISK_UNKNOWN   = 0
    RISK_VERY_LOW  = 1   # < 0.15
    RISK_LOW       = 2   # 0.15 – 0.35
    RISK_MEDIUM    = 3   # 0.35 – 0.60
    RISK_HIGH      = 4   # 0.60 – 0.80
    RISK_VERY_HIGH = 5   # > 0.80

    @staticmethod
    def from_score(score: float) -> "RiskCategory":
        if score < 0.15: return RiskCategory.RISK_VERY_LOW
        if score < 0.35: return RiskCategory.RISK_LOW
        if score < 0.60: return RiskCategory.RISK_MEDIUM
        if score < 0.80: return RiskCategory.RISK_HIGH
        return RiskCategory.RISK_VERY_HIGH

    def label(self) -> str:
        return self.name.replace("RISK_", "").replace("_", "-").lower()

    @property
    def requires_human_approval(self) -> bool:
        return self in (RiskCategory.RISK_HIGH, RiskCategory.RISK_VERY_HIGH)


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
            elif isinstance(v, list):
                s = [
                    i._to_dict() if isinstance(i, _ProtoMessage)
                    else (int(i) if isinstance(i, IntEnum) else i)
                    for i in v
                ]
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
        fields_str = ", ".join(f"{k}={v!r}" for k, v in list(d.items())[:6])
        return f"{self.__class__.__name__}({fields_str})"

    def ByteSize(self) -> int:
        return len(self.SerializeToString())


# ── Action parameter types ─────────────────────────────────────────────────────

@dataclass
class ScaleParameters(_ProtoMessage):
    current_replicas: int = 0
    target_replicas:  int = 0
    deployment_name:  str = ""
    namespace:        str = ""
    reason:           str = ""


@dataclass
class RollbackParameters(_ProtoMessage):
    deployment_name: str = ""
    namespace:       str = ""
    target_revision: int = 0   # 0 = previous revision
    current_image:   str = ""
    target_image:    str = ""


@dataclass
class ExecParameters(_ProtoMessage):
    pod_name:       str       = ""
    container_name: str       = ""
    namespace:      str       = ""
    command:        List[str] = field(default_factory=list)
    timeout_s:      float     = 30.0   # seconds


@dataclass
class NetworkPolicyParameters(_ProtoMessage):
    pod_name:             str = ""
    namespace:            str = ""
    policy_spec_json:     str = ""
    existing_policy_name: str = ""
    direction:            str = "both"   # ingress | egress | both


@dataclass
class ResourceLimitParameters(_ProtoMessage):
    resource_name:   str = ""
    namespace:       str = ""
    container_name:  str = ""
    cpu_limit:       str = ""
    memory_limit:    str = ""
    original_cpu:    str = ""
    original_memory: str = ""


@dataclass
class NodeParameters(_ProtoMessage):
    node_name:     str   = ""
    drain_grace:   bool  = True
    drain_timeout_s: float = 300.0


@dataclass
class SecretRotationParameters(_ProtoMessage):
    secret_name:     str = ""
    namespace:       str = ""
    rotation_method: str = "manual"   # aws-iam | vault | manual


@dataclass
class HpaParameters(_ProtoMessage):
    hpa_name:     str = ""
    namespace:    str = ""
    min_replicas: int = 0
    max_replicas: int = 0
    original_min: int = 0
    original_max: int = 0


# ── Ghost simulation result ───────────────────────────────────────────────────

@dataclass
class GhostSimulationResult(_ProtoMessage):
    """
    Output of the Kubernetes dry-run + risk scoring simulation.
    Attached to every ActionRequest before the OPA gate.
    """
    risk_score:             float          = 0.0
    risk_category:          RiskCategory   = RiskCategory.RISK_UNKNOWN
    confidence:             float          = 0.0

    # Impact estimates
    mttr_delta_seconds:     float          = 0.0
    traffic_impact_pct:     float          = 0.0
    availability_impact:    float          = 0.0
    affected_pod_count:     int            = 0

    collateral_node_ids:    List[str]      = field(default_factory=list)
    collateral_node_names:  List[str]      = field(default_factory=list)

    # OPA check
    opa_violations:         List[str]      = field(default_factory=list)
    opa_approved:           bool           = False

    # Kubernetes dry-run
    dry_run_succeeded:      bool           = False
    dry_run_error:          str            = ""

    # Recommendation
    recommended_action:     str            = ""
    recommendation_reason:  str            = ""
    projected_status:       str            = ""

    # Performance
    sim_duration_ms:        float          = 0.0
    sim_timestamp:          str            = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def is_safe(self) -> bool:
        """True if risk score is below the 'ghost_risk_threshold' (0.35)."""
        return self.risk_score < 0.35 and self.opa_approved

    def summary(self) -> str:
        cat = RiskCategory(self.risk_category).label()
        status = "✓ APPROVED" if self.opa_approved else "✗ DENIED"
        return (
            f"Ghost: risk={self.risk_score:.2f} ({cat}) "
            f"confidence={self.confidence:.0%} "
            f"OPA={status} "
            f"pods_affected={self.affected_pod_count}"
        )


# ── Action request ────────────────────────────────────────────────────────────

@dataclass
class ActionRequest(_ProtoMessage):
    """
    Proposed remediation action produced by the RL policy.
    Lifecycle: RL policy → Ghost simulator → OPA gate → executor.
    """
    request_id:          str                    = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    requested_at:        str                    = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    action_name:         ActionName             = ActionName.ACTION_UNKNOWN
    action_label:        str                    = ""

    # Target
    target_node_id:      str                    = ""
    target_node_name:    str                    = ""
    target_namespace:    str                    = ""
    target_resource:     str                    = ""

    # GNN trigger context
    inference_id:        str                    = ""
    trigger_class:       NodeClass              = NodeClass.NODE_CLASS_UNKNOWN
    trigger_confidence:  float                  = 0.0
    root_cause_node:     str                    = ""

    # RL policy details
    policy_version:      str                    = "1.0"
    rl_q_value:          float                  = 0.0

    # Autonomy context
    autonomy_mode:       AutonomyMode           = AutonomyMode.AUTONOMY_SUPERVISED
    requester:           str                    = "rl-policy"

    # Type-specific parameters (exactly one should be set)
    scale:               Optional[ScaleParameters]              = None
    rollback:            Optional[RollbackParameters]           = None
    exec_cmd:            Optional[ExecParameters]               = None
    network:             Optional[NetworkPolicyParameters]      = None
    resource:            Optional[ResourceLimitParameters]      = None
    node:                Optional[NodeParameters]               = None
    secret:              Optional[SecretRotationParameters]     = None
    hpa:                 Optional[HpaParameters]                = None

    # Ghost simulation (attached before OPA gate)
    ghost_result:        Optional[GhostSimulationResult]        = None

    # Fallback generic parameters
    extra_params:        Dict[str, str]         = field(default_factory=dict)

    def get_parameters(self) -> Optional[_ProtoMessage]:
        """Return the active parameter object regardless of type."""
        for attr in ("scale", "rollback", "exec_cmd", "network",
                     "resource", "node", "secret", "hpa"):
            v = getattr(self, attr, None)
            if v is not None:
                return v
        return None

    def is_high_risk(self) -> bool:
        if self.ghost_result is None:
            return True   # assume high risk if not simulated
        return RiskCategory(self.ghost_result.risk_category).requires_human_approval

    def requires_human_approval(self) -> bool:
        if self.autonomy_mode == AutonomyMode.AUTONOMY_SUPERVISED:
            return True
        if self.autonomy_mode == AutonomyMode.AUTONOMY_HUMAN_IN_LOOP:
            return self.is_high_risk()
        return False

    def short_desc(self) -> str:
        action = ActionName(self.action_name).label()
        return f"{action}({self.target_node_name or self.target_resource})"


# ── Action result ─────────────────────────────────────────────────────────────

@dataclass
class ActionResult(_ProtoMessage):
    """
    Execution outcome for an ActionRequest.
    Published to Kafka ccdt.guardian.actions after every attempt.
    """
    audit_id:              str          = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    request:               Optional[ActionRequest] = None

    status:                ActionStatus = ActionStatus.STATUS_UNKNOWN
    message:               str          = ""
    error_detail:          str          = ""

    # Timing
    requested_at:          str          = ""
    executed_at:           str          = ""
    completed_at:          str          = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    execution_duration_ms: float        = 0.0

    # Kubernetes API response
    k8s_resource_version:  str          = ""
    k8s_uid:               str          = ""

    # Post-execution verification
    verified_effect:       bool         = False
    post_action_health:    float        = 0.0
    verification_note:     str          = ""

    # Rollback
    was_rolled_back:       bool         = False
    rollback_reason:       str          = ""

    # Context
    autonomy_mode:         AutonomyMode = AutonomyMode.AUTONOMY_UNKNOWN
    approved_by:           str          = ""
    incident_id:           str          = ""
    schema_ver:            str          = "1.0"

    @property
    def succeeded(self) -> bool:
        return self.status == ActionStatus.STATUS_SUCCEEDED

    @property
    def denied(self) -> bool:
        return self.status == ActionStatus.STATUS_DENIED

    def summary(self) -> str:
        status = ActionStatus(self.status).label()
        action = ""
        if self.request:
            action = self.request.short_desc() + " → "
        return f"{action}{status} ({self.execution_duration_ms:.0f}ms)"


# ── Action history entry ──────────────────────────────────────────────────────

@dataclass
class ActionHistoryEntry(_ProtoMessage):
    """
    Compact history record — used by OPA lateral_movement policy
    to detect retry loops (>3 identical actions in 10 minutes).
    """
    action_name: str          = ""
    target_node: str          = ""
    status:      ActionStatus = ActionStatus.STATUS_UNKNOWN
    executed_at: str          = ""
    age_minutes: float        = 0.0


# ── Approval request / response ───────────────────────────────────────────────

@dataclass
class ApprovalRequest(_ProtoMessage):
    """
    Sent to operator notification channel (Slack/email) in supervised mode.
    Expires automatically after 15 minutes (auto-deny on timeout).
    """
    approval_id:    str                          = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    action_request: Optional[ActionRequest]      = None
    ghost_result:   Optional[GhostSimulationResult] = None
    reasoning:      str                          = ""
    expires_at:     str                          = ""
    notify_channel: str                          = ""
    notify_webhook: str                          = ""


@dataclass
class ApprovalResponse(_ProtoMessage):
    approval_id:  str  = ""
    approved:     bool = False
    approver:     str  = ""
    reason:       str  = ""
    responded_at: str  = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ── gRPC service request/response helpers ─────────────────────────────────────

@dataclass
class ActionHistoryRequest(_ProtoMessage):
    node_name:      str   = ""
    limit:          int   = 20
    within_minutes: float = 10.0


@dataclass
class ActionHistoryResponse(_ProtoMessage):
    entries:     List[ActionHistoryEntry] = field(default_factory=list)
    total_count: int                      = 0


@dataclass
class StreamActionsRequest(_ProtoMessage):
    filter_status: List[ActionStatus] = field(default_factory=list)


@dataclass
class PendingApprovalsRequest(_ProtoMessage):
    namespace: str = ""


@dataclass
class PendingApprovalsResponse(_ProtoMessage):
    approvals: List[ApprovalRequest] = field(default_factory=list)


@dataclass
class HealthCheckRequest(_ProtoMessage):
    pass


@dataclass
class HealthCheckResponse(_ProtoMessage):
    healthy:          bool = False
    status:           str  = ""
    opa_status:       str  = ""
    rl_model_version: str  = ""
    autonomy_mode:    str  = ""
