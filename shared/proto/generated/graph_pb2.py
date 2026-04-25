# ─────────────────────────────────────────────────────────────────────────────
# CCDT graph_pb2.py — Pure-Python message shims for graph.proto
# ─────────────────────────────────────────────────────────────────────────────
# Mirrors the full graph.proto API with Python dataclasses so application code
# works without running protoc. Run `make proto` to replace with compiled stubs.
#
# Message hierarchy:
#   NodeFeatures          — 16-dim node feature vector (eBPF + k8s signals)
#   EdgeFeatures          — 4-dim edge feature vector
#   TopologyNode          — node in the cluster graph (class + features)
#   TopologyEdge          — directed edge (service dependency)
#   CausalChainNode       — one hop in the ordered causal chain
#   TopFeature            — top contributing feature for explanation
#   CounterfactualResult  — Pearl do-calculus P(Y|do(X)) estimate
#   GnnInferenceResult    — full inference result published to Kafka
#   TopologySnapshot      — on-demand topology graph snapshot
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Dict, List, Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class NodeClass(IntEnum):
    """GNN node classification output."""
    NODE_CLASS_UNKNOWN = 0
    NODE_CLASS_HEALTHY = 1   # nominal operation
    NODE_CLASS_FAULT   = 2   # software fault (OOM, crash loop, latency spike)
    NODE_CLASS_ATTACK  = 3   # active security threat

    def label(self) -> str:
        return {
            0: "UNKNOWN",
            1: "HEALTHY",
            2: "FAULT",
            3: "ATTACK",
        }[self.value]


class IncidentType(IntEnum):
    """Graph-level incident type."""
    INCIDENT_UNKNOWN      = 0
    INCIDENT_NONE         = 1
    INCIDENT_FAULT        = 2
    INCIDENT_ATTACK       = 3
    INCIDENT_FAULT_ATTACK = 4
    INCIDENT_PERFORMANCE  = 5
    INCIDENT_RESOURCE     = 6

    def label(self) -> str:
        return {
            0: "UNKNOWN",
            1: "NONE",
            2: "FAULT",
            3: "ATTACK",
            4: "FAULT_ATTACK",
            5: "PERFORMANCE",
            6: "RESOURCE",
        }[self.value]

    @property
    def is_active(self) -> bool:
        return self.value not in (0, 1)

    @property
    def is_security(self) -> bool:
        return self.value in (3, 4)


class NodeType(IntEnum):
    NODE_TYPE_UNKNOWN  = 0
    NODE_TYPE_SERVICE  = 1
    NODE_TYPE_POD      = 2
    NODE_TYPE_DATABASE = 3
    NODE_TYPE_CACHE    = 4
    NODE_TYPE_QUEUE    = 5
    NODE_TYPE_INGRESS  = 6
    NODE_TYPE_EXTERNAL = 7
    NODE_TYPE_NODE     = 8


class EdgeType(IntEnum):
    EDGE_TYPE_UNKNOWN   = 0
    EDGE_TYPE_HTTP      = 1
    EDGE_TYPE_GRPC      = 2
    EDGE_TYPE_DATABASE  = 3
    EDGE_TYPE_QUEUE     = 4
    EDGE_TYPE_CACHE     = 5
    EDGE_TYPE_DNS       = 6
    EDGE_TYPE_COLOCATED = 7


class CausalDirection(IntEnum):
    CAUSAL_UNKNOWN    = 0
    CAUSAL_CAUSES     = 1   # A → B
    CAUSAL_CAUSED_BY  = 2   # A ← B
    CAUSAL_CORRELATED = 3   # A ↔ B


# ── Base message mixin ────────────────────────────────────────────────────────

class _ProtoMessage:
    """Minimal proto-compatible message base — mirrors protobuf Python API."""

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
                serialised = [
                    i._to_dict() if isinstance(i, _ProtoMessage)
                    else (int(i) if isinstance(i, IntEnum) else i)
                    for i in v
                ]
                if serialised:
                    result[k] = serialised
            elif isinstance(v, dict):
                if v:
                    result[k] = {
                        dk: (dv._to_dict() if isinstance(dv, _ProtoMessage) else dv)
                        for dk, dv in v.items()
                    }
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
        obj = object.__new__(cls)
        obj.__init__(**{})   # call default __init__ first
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

    def ListFields(self) -> list:
        return [(k, v) for k, v in self.__dict__.items() if v]

    def HasField(self, name: str) -> bool:
        v = getattr(self, name, None)
        return v is not None and v != type(v)()


# ── Node feature vector ───────────────────────────────────────────────────────

@dataclass
class NodeFeatures(_ProtoMessage):
    """
    16-dimensional node feature vector.
    All values are normalised to [0, 1] unless noted.
    Matches the GNN model's expected input shape: (N, 16).
    """
    # Resource metrics
    cpu_utilization:     float = 0.0   # CPU usage %
    memory_utilization:  float = 0.0   # RSS / limit
    net_rx_bytes_rate:   float = 0.0   # log-normalised bytes/s ingress
    net_tx_bytes_rate:   float = 0.0   # log-normalised bytes/s egress
    disk_read_rate:      float = 0.0
    disk_write_rate:     float = 0.0
    # K8s health
    restart_count:       float = 0.0   # log-normalised container restarts
    ready_replicas_ratio: float = 1.0  # ready / desired
    pod_age_seconds:     float = 0.0   # log-normalised
    # eBPF-derived security signals
    capability_event_rate:  float = 0.0
    oom_kill_rate:          float = 0.0
    tcp_retransmit_rate:    float = 0.0
    sched_latency_p99_us:   float = 0.0
    sensitive_file_rate:    float = 0.0
    syscall_anomaly_score:  float = 0.0
    execve_rate:            float = 0.0

    def to_tensor_list(self) -> list[float]:
        """Return feature values as an ordered list suitable for torch.tensor()."""
        return [
            self.cpu_utilization,
            self.memory_utilization,
            self.net_rx_bytes_rate,
            self.net_tx_bytes_rate,
            self.disk_read_rate,
            self.disk_write_rate,
            self.restart_count,
            self.ready_replicas_ratio,
            self.pod_age_seconds,
            self.capability_event_rate,
            self.oom_kill_rate,
            self.tcp_retransmit_rate,
            self.sched_latency_p99_us,
            self.sensitive_file_rate,
            self.syscall_anomaly_score,
            self.execve_rate,
        ]

    def anomaly_score(self) -> float:
        """
        Heuristic overall anomaly score for this node (0-1).
        Weighted sum of security and resource signals.
        """
        return min(1.0, (
            self.syscall_anomaly_score * 0.25 +
            self.capability_event_rate * 0.15 +
            self.oom_kill_rate         * 0.20 +
            self.sensitive_file_rate   * 0.15 +
            self.cpu_utilization       * 0.10 +
            self.memory_utilization    * 0.10 +
            self.tcp_retransmit_rate   * 0.05
        ))


# ── Edge feature vector ───────────────────────────────────────────────────────

@dataclass
class EdgeFeatures(_ProtoMessage):
    """4-dimensional edge feature vector. Matches GNN edge_attr shape: (E, 4)."""
    request_rate:        float = 0.0
    error_rate:          float = 0.0   # fraction [0,1]
    latency_p99_ms:      float = 0.0   # log-normalised
    bandwidth_bytes_sec: float = 0.0   # log-normalised


# ── Topology node ─────────────────────────────────────────────────────────────

@dataclass
class TopologyNode(_ProtoMessage):
    node_id:          str              = ""
    name:             str              = ""
    node_type:        NodeType         = NodeType.NODE_TYPE_SERVICE
    namespace:        str              = ""
    pod_name:         str              = ""
    deployment:       str              = ""
    node_name:        str              = ""
    pod_ips:          List[str]        = field(default_factory=list)
    node_class:       NodeClass        = NodeClass.NODE_CLASS_HEALTHY
    class_confidence: float            = 0.0
    features:         NodeFeatures     = field(default_factory=NodeFeatures)
    is_root_cause:    bool             = False
    is_isolated:      bool             = False
    labels:           Dict[str, str]   = field(default_factory=dict)

    def is_anomalous(self, threshold: float = 0.5) -> bool:
        return self.features.anomaly_score() >= threshold

    def summary(self) -> str:
        cls = NodeClass(self.node_class).label()
        return (
            f"{self.name} [{cls} {self.class_confidence:.0%}]"
            f"{' ← ROOT CAUSE' if self.is_root_cause else ''}"
            f"{' [ISOLATED]' if self.is_isolated else ''}"
        )


# ── Topology edge ─────────────────────────────────────────────────────────────

@dataclass
class TopologyEdge(_ProtoMessage):
    edge_id:         str              = ""
    source_node_id:  str              = ""
    target_node_id:  str              = ""
    edge_type:       EdgeType         = EdgeType.EDGE_TYPE_HTTP
    features:        EdgeFeatures     = field(default_factory=EdgeFeatures)
    is_causal:       bool             = False
    causal_direction: CausalDirection = CausalDirection.CAUSAL_UNKNOWN
    gnn_weight:      float            = 0.0


# ── Causal chain node ─────────────────────────────────────────────────────────

@dataclass
class CausalChainNode(_ProtoMessage):
    """One hop in the ordered causal chain. Depth 0 = root cause."""
    node_id:          str       = ""
    node_name:        str       = ""
    node_class:       NodeClass = NodeClass.NODE_CLASS_UNKNOWN
    causal_score:     float     = 0.0   # fraction of causal influence [0,1]
    do_calculus_prob: float     = 0.0   # P(Y | do(X)) — counterfactual
    explanation:      str       = ""
    depth:            int       = 0


# ── Top contributing feature ──────────────────────────────────────────────────

@dataclass
class TopFeature(_ProtoMessage):
    feature_name: str   = ""
    node_name:    str   = ""
    importance:   float = 0.0
    value:        float = 0.0
    threshold:    float = 0.0
    explanation:  str   = ""


# ── Counterfactual result ─────────────────────────────────────────────────────

@dataclass
class CounterfactualResult(_ProtoMessage):
    """Pearl do-calculus estimate: P(incident | do(node = healthy))."""
    node_id:                    str   = ""
    node_name:                  str   = ""
    incident_prob_if_healthy:   float = 0.0
    incident_prob_actual:       float = 0.0
    causal_effect:              float = 0.0   # actual − counterfactual
    explanation:                str   = ""


# ── GNN inference result ──────────────────────────────────────────────────────

@dataclass
class GnnInferenceResult(_ProtoMessage):
    """
    Full GNN inference result published to Kafka ccdt.gnn.inference.

    Created every ~5 seconds when confidence ≥ 0.65, or every 30 s as a
    heartbeat (is_heartbeat=True, incident_type=NONE).

    The Kafka message key is set to inference_id for ordered delivery
    within the partition.
    """
    inference_id:          str                         = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    timestamp:             str                         = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    incident_type:         IncidentType                = IncidentType.INCIDENT_NONE
    graph_confidence:      float                       = 0.0

    root_cause_node_id:    str                         = ""
    root_cause_node_name:  str                         = ""
    root_cause_confidence: float                       = 0.0

    blast_radius_node_ids: List[str]                   = field(default_factory=list)
    blast_radius_count:    int                         = 0

    causal_chain:          List[CausalChainNode]       = field(default_factory=list)
    node_classifications:  List[TopologyNode]          = field(default_factory=list)
    top_features:          List[TopFeature]            = field(default_factory=list)
    counterfactuals:       List[CounterfactualResult]  = field(default_factory=list)

    inference_latency_ms:  float                       = 0.0
    node_count:            int                         = 0
    edge_count:            int                         = 0
    is_heartbeat:          bool                        = False
    schema_ver:            str                         = "1.0"

    # ── Convenience methods ────────────────────────────────────────────────

    @property
    def is_active_incident(self) -> bool:
        return self.incident_type not in (
            IncidentType.INCIDENT_UNKNOWN,
            IncidentType.INCIDENT_NONE,
        )

    @property
    def severity(self) -> str:
        """Map confidence + incident type to a human-readable severity."""
        if not self.is_active_incident:
            return "none"
        if self.incident_type in (
            IncidentType.INCIDENT_ATTACK,
            IncidentType.INCIDENT_FAULT_ATTACK,
        ):
            return "critical" if self.graph_confidence >= 0.85 else "high"
        if self.graph_confidence >= 0.85:
            return "high"
        if self.graph_confidence >= 0.70:
            return "medium"
        return "low"

    def attack_nodes(self) -> list[TopologyNode]:
        return [n for n in self.node_classifications
                if n.node_class == NodeClass.NODE_CLASS_ATTACK]

    def fault_nodes(self) -> list[TopologyNode]:
        return [n for n in self.node_classifications
                if n.node_class == NodeClass.NODE_CLASS_FAULT]

    def nl_summary(self) -> str:
        """One-line human-readable summary for incident reports."""
        if not self.is_active_incident:
            return (f"Cluster healthy — {self.node_count} nodes monitored "
                    f"({self.inference_latency_ms:.0f}ms)")
        itype = IncidentType(self.incident_type).label()
        return (
            f"{itype} detected — root cause: {self.root_cause_node_name or 'unknown'} "
            f"(confidence {self.graph_confidence:.0%}), "
            f"blast radius: {self.blast_radius_count} nodes"
        )


# ── Topology snapshot ─────────────────────────────────────────────────────────

@dataclass
class TopologySnapshot(_ProtoMessage):
    """On-demand full topology graph snapshot (gRPC GetTopology response)."""
    snapshot_id:   str                  = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    timestamp:     str                  = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    nodes:         List[TopologyNode]   = field(default_factory=list)
    edges:         List[TopologyEdge]   = field(default_factory=list)
    incident_type: IncidentType         = IncidentType.INCIDENT_NONE
    cluster_name:  str                  = ""
    schema_ver:    str                  = "1.0"

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def get_node(self, node_id: str) -> Optional[TopologyNode]:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def get_neighbors(self, node_id: str) -> list[TopologyNode]:
        """Return all nodes directly connected to node_id."""
        neighbor_ids = {
            e.target_node_id for e in self.edges if e.source_node_id == node_id
        } | {
            e.source_node_id for e in self.edges if e.target_node_id == node_id
        }
        return [n for n in self.nodes if n.node_id in neighbor_ids]


# ── gRPC request/response helpers ─────────────────────────────────────────────

@dataclass
class GetInferenceRequest(_ProtoMessage):
    cluster: str = ""

@dataclass
class StreamInferenceRequest(_ProtoMessage):
    cluster:        str   = ""
    min_confidence: float = 0.0

@dataclass
class GetTopologyRequest(_ProtoMessage):
    cluster: str = ""

@dataclass
class CounterfactualQuery(_ProtoMessage):
    node_id:            str = ""
    hypothetical_class: str = "healthy"

@dataclass
class CounterfactualResponse(_ProtoMessage):
    results:     List[CounterfactualResult] = field(default_factory=list)
    explanation: str                        = ""
