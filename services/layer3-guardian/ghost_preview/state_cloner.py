"""
CCDT Layer-3 Guardian — State Cloner
═══════════════════════════════════════════════════════════════════════════════
Creates a deep, isolated copy of the current cluster state for use in
Ghost Preview simulations. The clone is entirely in-memory and has NO
side-effects on the real cluster.

Cloning pipeline:
  1. Snapshot current topology from LiveDAGBuilder
  2. Deep-copy all node states, edge states, and metric aggregations
  3. Apply any provided metric overrides (for scenario injection)
  4. Return a ClusterSnapshot that the Simulator can mutate freely

The snapshot captures:
  - Node states (cpu, mem, status, class, metadata)
  - Edge states (latency, error_rate, request_rate, causal flag)
  - Cluster-level metadata (namespace, incident type, timestamp)
"""
from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("ccdt.guardian.state_cloner")


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class NodeSnapshot:
    """Immutable snapshot of a single node's state."""
    id:                   str
    label:                str
    status:               str          # healthy | warning | critical
    node_class:           str          # healthy | fault | attack
    layer:                str          # network | service | data | system
    cpu:                  float        # 0-1
    mem:                  float        # 0-1
    restarts:             int
    oom_kills:            int
    tcp_retransmit_rate:  float
    sched_latency_p99_ms: float
    cap_event:            bool
    file_event:           bool

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "label":                self.label,
            "status":               self.status,
            "class":                self.node_class,
            "layer":                self.layer,
            "cpu":                  round(self.cpu * 100, 1),
            "mem":                  round(self.mem * 100, 1),
            "restarts":             self.restarts,
            "oom_kills":            self.oom_kills,
            "tcp_retransmit_rate":  self.tcp_retransmit_rate,
            "sched_latency_p99_ms": self.sched_latency_p99_ms,
            "cap_event":            self.cap_event,
            "file_event":           self.file_event,
        }

    def clone(self) -> "NodeSnapshot":
        return copy.deepcopy(self)


@dataclass
class EdgeSnapshot:
    """Immutable snapshot of a single directed edge."""
    src:          str
    dst:          str
    latency_ms:   float
    error_rate:   float
    request_rate: float
    causal:       bool

    def to_dict(self) -> dict:
        return {
            "from":         self.src,
            "to":           self.dst,
            "latency_ms":   self.latency_ms,
            "error_rate":   self.error_rate,
            "request_rate": self.request_rate,
            "causal":       self.causal,
        }

    def clone(self) -> "EdgeSnapshot":
        return copy.deepcopy(self)


@dataclass
class ClusterSnapshot:
    """
    Full cluster state snapshot used for Ghost Preview simulation.

    Nodes and edges are mutable lists — the Simulator modifies them
    in-place without affecting the original topology.
    """
    snapshot_id:    str
    timestamp:      float
    namespace:      str
    incident_type:  str                          # fault | attack | healthy
    nodes:          list[NodeSnapshot]  = field(default_factory=list)
    edges:          list[EdgeSnapshot]  = field(default_factory=list)
    metadata:       dict                = field(default_factory=dict)

    # ── Lookup helpers ────────────────────────────────────────────────────────

    def get_node(self, node_id: str) -> Optional[NodeSnapshot]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_edges_from(self, src: str) -> list[EdgeSnapshot]:
        return [e for e in self.edges if e.src == src]

    def get_edges_to(self, dst: str) -> list[EdgeSnapshot]:
        return [e for e in self.edges if e.dst == dst]

    def incident_nodes(self) -> list[NodeSnapshot]:
        return [n for n in self.nodes if n.node_class in ("fault", "attack")]

    def critical_nodes(self) -> list[NodeSnapshot]:
        return [n for n in self.nodes if n.status == "critical"]

    def all_healthy(self) -> bool:
        return all(n.status == "healthy" for n in self.nodes)

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_topology_dict(self) -> dict:
        """Convert to the same format as LiveDAGBuilder.get_topology_dict()."""
        return {
            "nodes":         [n.to_dict() for n in self.nodes],
            "edges":         [e.to_dict() for e in self.edges],
            "snapshot_id":   self.snapshot_id,
            "incident_type": self.incident_type,
            "timestamp":     int(self.timestamp),
        }

    def clone(self) -> "ClusterSnapshot":
        """Return a deep copy — safe to mutate without affecting the original."""
        return ClusterSnapshot(
            snapshot_id    = self.snapshot_id + "_clone",
            timestamp      = self.timestamp,
            namespace      = self.namespace,
            incident_type  = self.incident_type,
            nodes          = [n.clone() for n in self.nodes],
            edges          = [e.clone() for e in self.edges],
            metadata       = copy.deepcopy(self.metadata),
        )


# ─── StateCloner ──────────────────────────────────────────────────────────────

class StateCloner:
    """
    Captures the current cluster state and returns isolated ClusterSnapshot
    objects for use in Ghost Preview simulations.

    Usage:
        cloner   = StateCloner(dag_builder)
        snapshot = await cloner.snapshot()
        sim_copy = snapshot.clone()    # always clone before simulating
    """

    def __init__(self, dag_builder=None, namespace: str = "default") -> None:
        self._dag_builder = dag_builder
        self._namespace   = namespace

    async def snapshot(
        self,
        topology_override: Optional[dict] = None,
        incident_type:     str            = "fault",
    ) -> ClusterSnapshot:
        """
        Capture a snapshot of the current cluster state.

        If topology_override is provided, uses that dict directly.
        Otherwise queries the LiveDAGBuilder.
        """
        import uuid
        snap_id = str(uuid.uuid4())[:8]

        topo = topology_override
        if topo is None and self._dag_builder is not None:
            topo = self._dag_builder.get_topology_dict()

        if topo is None:
            logger.warning("No topology available — using empty snapshot")
            return ClusterSnapshot(
                snapshot_id   = snap_id,
                timestamp     = time.time(),
                namespace     = self._namespace,
                incident_type = incident_type,
            )

        # Infer incident type if not provided
        if incident_type == "fault":
            incident_type = self._infer_incident_type(topo)

        nodes = self._parse_nodes(topo.get("nodes", []))
        edges = self._parse_edges(topo.get("edges", []))

        return ClusterSnapshot(
            snapshot_id   = snap_id,
            timestamp     = time.time(),
            namespace     = self._namespace,
            incident_type = incident_type,
            nodes         = nodes,
            edges         = edges,
            metadata      = {
                "source":    "dag_builder",
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        )

    def snapshot_sync(
        self,
        topology_dict: dict,
        incident_type: str = "fault",
    ) -> ClusterSnapshot:
        """
        Synchronous version of snapshot() for non-async contexts.
        Requires topology_dict to be provided explicitly.
        """
        import uuid
        nodes = self._parse_nodes(topology_dict.get("nodes", []))
        edges = self._parse_edges(topology_dict.get("edges", []))
        return ClusterSnapshot(
            snapshot_id   = str(uuid.uuid4())[:8],
            timestamp     = time.time(),
            namespace     = self._namespace,
            incident_type = incident_type,
            nodes         = nodes,
            edges         = edges,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _parse_nodes(self, raw_nodes: list[dict]) -> list[NodeSnapshot]:
        result = []
        for n in raw_nodes:
            result.append(NodeSnapshot(
                id                   = n.get("id", "unknown"),
                label                = n.get("label", n.get("id", "unknown")),
                status               = n.get("status", "healthy"),
                node_class           = n.get("class", n.get("node_class", "healthy")),
                layer                = n.get("layer", "service"),
                cpu                  = float(n.get("cpu", 30)) / 100.0,
                mem                  = float(n.get("mem", 40)) / 100.0,
                restarts             = int(n.get("restarts", 0)),
                oom_kills            = int(n.get("oom_kills", 0)),
                tcp_retransmit_rate  = float(n.get("tcp_retransmit_rate", 0)),
                sched_latency_p99_ms = float(n.get("sched_latency_p99_ms", 0)),
                cap_event            = bool(n.get("cap_event", False)),
                file_event           = bool(n.get("file_event", False)),
            ))
        return result

    def _parse_edges(self, raw_edges: list[dict]) -> list[EdgeSnapshot]:
        result = []
        for e in raw_edges:
            src = e.get("from") or e.get("source") or e.get("src", "")
            dst = e.get("to")   or e.get("target") or e.get("dst", "")
            result.append(EdgeSnapshot(
                src          = src,
                dst          = dst,
                latency_ms   = float(e.get("latencyMs",   e.get("latency_ms",   1.0))),
                error_rate   = float(e.get("errorRate",   e.get("error_rate",   0.0))),
                request_rate = float(e.get("requestRate", e.get("request_rate", 0.0))),
                causal       = bool(e.get("causal", False)),
            ))
        return result

    def _infer_incident_type(self, topo: dict) -> str:
        """Infer fault vs attack from topology node flags."""
        for n in topo.get("nodes", []):
            if n.get("cap_event") or n.get("file_event"):
                return "attack"
        return "fault"
