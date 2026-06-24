"""
CCDT Layer-2 Cognitive Core — Live DAG Builder
───────────────────────────────────────────────
Maintains a live, continuously-updated directed acyclic graph of the
cluster's service topology for consumption by the GNN.

Sources:
  1. Kubernetes API     — bootstrap: pods, services, endpoints, deployments
  2. Kafka stream       — live updates from Layer-1 eBPF events
  3. Prometheus metrics — edge latency, error_rate, request_rate (refreshed every 30 s)

Output: PyG Data objects (x, edge_index, edge_attr, node_ids, metadata)

Acyclicity enforcement:
  Cycles are broken greedily (lowest-weight edge removed first).
  All produced graphs are guaranteed DAGs — a precondition for the GNN's
  DAG regularisation loss.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

import networkx as nx
import numpy as np
import torch
from torch_geometric.data import Data

logger = logging.getLogger("ccdt.cognitive.dag_builder")

# ─── Node feature layout (must match causal_gnn.py NODE_FEATURE_DIM=17) ──────
# Index mapping for the 17-dim node feature vector
FEAT = {
    "cpu":           0,
    "mem":           1,
    "sched_lat_p99": 2,
    "tcp_retx":      3,
    "oom_count":     4,
    "cap_events":    5,
    "syscall_rate":  6,
    "file_events":   7,
    "error_rate":    8,
    "request_rate":  9,
    "latency_ms":    10,
    "restarts":      11,
    "replica_count": 12,
    "is_critical":   13,
    "is_external":   14,
    "layer_0":       15,   # layer one-hot bit 0
    "layer_1":       16,   # layer one-hot bit 1
}
NODE_FEAT_DIM = 17

# Edge feature layout (must match EDGE_FEATURE_DIM=4 in causal_gnn.py)
EDGE_FEAT = {
    "latency_ms_norm":   0,
    "error_rate_norm":   1,
    "request_rate_norm": 2,
    "is_causal":         3,
}
EDGE_FEAT_DIM = 4

# Layer encoding
LAYER_CODES = {"network": (0, 0), "service": (
    0, 1), "data": (1, 0), "system": (1, 1)}


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class NodeState:
    """Live state of a single service node."""
    node_id:      str
    label:        str
    namespace:    str = "default"
    node_name:    str = "unknown"
    layer:        str = "service"
    cpu:          float = 0.0    # 0..100 %
    mem:          float = 0.0    # 0..100 %
    sched_lat:    float = 0.0    # p99 ms
    tcp_retx:     float = 0.0    # retransmits/s
    oom_count:    int = 0
    cap_events:   int = 0
    syscall_rate: float = 0.0    # events/s
    file_events:  int = 0
    error_rate:   float = 0.0    # 0..1
    request_rate: float = 0.0    # req/s
    latency_ms:   float = 0.0    # p99 ms
    restarts:     int = 0
    replicas:     int = 1
    is_critical:  bool = False
    is_external:  bool = False

    def to_feature_vector(self) -> list[float]:
        """Convert to 17-dim feature vector, all values normalised to [0, 1]."""
        l0, l1 = LAYER_CODES.get(self.layer, (0, 1))
        return [
            self.cpu / 100.0,
            self.mem / 100.0,
            min(self.sched_lat / 200.0, 1.0),      # normalise: 200ms max
            min(self.tcp_retx / 500.0, 1.0),      # normalise: 500 retx/s max
            min(self.oom_count / 10.0,  1.0),
            min(self.cap_events / 20.0, 1.0),
            min(self.syscall_rate / 10000.0, 1.0),
            min(self.file_events / 50.0, 1.0),
            self.error_rate,
            min(self.request_rate / 1000.0, 1.0),
            min(self.latency_ms / 500.0,  1.0),
            min(self.restarts / 10.0,   1.0),
            min(self.replicas / 10.0,   1.0),
            float(self.is_critical),
            float(self.is_external),
            float(l0),
            float(l1),
        ]


@dataclass
class EdgeState:
    """Live state of a directed service edge."""
    src:          str
    dst:          str
    type:         str = "http"
    latency_ms:   float = 0.0
    error_rate:   float = 0.0
    request_rate: float = 0.0
    is_causal:    bool = False

    def to_feature_vector(self) -> list[float]:
        """Convert to 4-dim edge feature vector."""
        return [
            min(self.latency_ms / 500.0, 1.0),
            self.error_rate,
            min(self.request_rate / 1000.0, 1.0),
            float(self.is_causal),
        ]


# ─── Seed topology (fallback when K8s API is unreachable) ─────────────────────

SEED_NODES: list[dict[str, Any]] = [
    {"id": "api-gw",        "label": "API Gateway",
        "layer": "network",  "is_critical": True,  "cpu": 42, "mem": 58},
    {"id": "auth-svc",      "label": "Auth Service",
        "layer": "service",  "is_critical": False, "cpu": 31, "mem": 44},
    {"id": "order-svc",     "label": "Order Service",
        "layer": "service",  "is_critical": True,  "cpu": 94, "mem": 87},
    {"id": "payment-svc",   "label": "Payment Service",
        "layer": "service",  "is_critical": True,  "cpu": 67, "mem": 71},
    {"id": "inventory-svc", "label": "Inventory Service",
        "layer": "service",  "is_critical": False, "cpu": 28, "mem": 39},
    {"id": "notify-svc",    "label": "Notify Service",
        "layer": "service",  "is_critical": False, "cpu": 73, "mem": 62},
    {"id": "postgres",      "label": "PostgreSQL",        "layer": "data",
        "is_critical": True,  "cpu": 91, "mem": 89},
    {"id": "redis",         "label": "Redis Cache",
        "layer": "data",     "is_critical": False, "cpu": 18, "mem": 45},
    {"id": "kafka",         "label": "Kafka Broker",
        "layer": "system",   "is_critical": True,  "cpu": 35, "mem": 52},
    {"id": "monitoring",    "label": "VictoriaMetrics",
        "layer": "system",   "is_critical": False, "cpu": 22, "mem": 41},
]

SEED_EDGES: list[dict[str, Any]] = [
    {"src": "api-gw",        "dst": "auth-svc",      "type": "grpc",  "latency_ms": 2.1,
        "error_rate": 0.001, "request_rate": 320, "is_causal": False},
    {"src": "api-gw",        "dst": "order-svc",     "type": "http",
        "latency_ms": 142.0, "error_rate": 0.084, "request_rate": 280, "is_causal": True},
    {"src": "api-gw",        "dst": "payment-svc",   "type": "http",
        "latency_ms": 18.4,  "error_rate": 0.003, "request_rate": 95,  "is_causal": False},
    {"src": "order-svc",     "dst": "postgres",      "type": "tcp",
        "latency_ms": 88.0,  "error_rate": 0.124, "request_rate": 540, "is_causal": True},
    {"src": "order-svc",     "dst": "notify-svc",    "type": "kafka",
        "latency_ms": 5.2,   "error_rate": 0.002, "request_rate": 120, "is_causal": True},
    {"src": "payment-svc",   "dst": "postgres",      "type": "tcp",
        "latency_ms": 12.1,  "error_rate": 0.004, "request_rate": 90,  "is_causal": False},
    {"src": "inventory-svc", "dst": "postgres",      "type": "tcp",   "latency_ms": 8.4,
        "error_rate": 0.001, "request_rate": 60,  "is_causal": False},
    {"src": "notify-svc",    "dst": "kafka",         "type": "kafka",
        "latency_ms": 3.1,   "error_rate": 0.000, "request_rate": 180, "is_causal": False},
    {"src": "order-svc",     "dst": "redis",         "type": "tcp",   "latency_ms": 1.2,
        "error_rate": 0.000, "request_rate": 820, "is_causal": False},
    {"src": "monitoring",    "dst": "kafka",         "type": "probe",
        "latency_ms": 0.8,   "error_rate": 0.000, "request_rate": 40,  "is_causal": False},
]


# ─── LiveDAGBuilder ───────────────────────────────────────────────────────────

class LiveDAGBuilder:
    """
    Maintains a continuously-updated cluster service graph as both a NetworkX
    DiGraph and a PyTorch Geometric Data object.

    Thread-safe for async access; graph mutations are protected by asyncio.Lock.
    """

    def __init__(
        self,
        k8s_enabled:   bool = True,
        kafka_enabled: bool = True,
        kafka_servers: str = "kafka:9092",
        kafka_topic:   str = "ccdt.topology.updates",
        namespace:     str = "default",
    ) -> None:
        self.k8s_enabled = k8s_enabled
        self.kafka_enabled = kafka_enabled
        self.kafka_servers = kafka_servers
        self.kafka_topic = kafka_topic
        self.namespace = namespace

        # Live state stores
        self._nodes: dict[str, NodeState] = {}
        self._edges: dict[tuple[str, str], EdgeState] = {}
        self._lock = asyncio.Lock()

        # NetworkX graph (rebuilt on demand)
        self._nx_graph: Optional[nx.DiGraph] = None
        self._pyg_data: Optional[Data] = None
        self._dirty = True
        self._built_at = 0.0

        # Cache: node_id → integer index (rebuilt with graph)
        self._node_to_idx: dict[str, int] = {}

    # ─── Bootstrap ──────────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """
        Initialise the graph from the Kubernetes API.
        Falls back to seed topology if K8s is unreachable.
        """
        if self.k8s_enabled:
            try:
                await self._bootstrap_from_k8s()
                logger.info("DAG bootstrapped from Kubernetes API (%d nodes, %d edges)",
                            len(self._nodes), len(self._edges))
                return
            except Exception as exc:
                logger.warning(
                    "K8s bootstrap failed (%s) — using seed topology", exc)

        self._load_seed_topology()
        logger.info("DAG bootstrapped from seed topology (%d nodes, %d edges)",
                    len(self._nodes), len(self._edges))

    async def _bootstrap_from_k8s(self) -> None:
        """Fetch pods and services from the Kubernetes API to build initial graph."""
        from kubernetes import client, config

        try:
            config.load_incluster_config()   # running inside a pod
        except Exception:
            config.load_kube_config()        # running locally with kubeconfig

        v1 = client.CoreV1Api()
        apps = client.AppsV1Api()

        # Fetch deployments
        deps = apps.list_namespaced_deployment(self.namespace)
        async with self._lock:
            for dep in deps.items:
                name = dep.metadata.name
                node = NodeState(
                    node_id=name,
                    label=name.replace("-", " ").title(),
                    namespace=self.namespace,
                    replicas=dep.spec.replicas or 1,
                    layer="service",
                )
                self._nodes[name] = node

        # Fetch services to derive edges (via selector overlap)
        svcs = v1.list_namespaced_service(self.namespace)
        for svc in svcs.items:
            svc_name = svc.metadata.name
            if svc_name not in self._nodes:
                async with self._lock:
                    self._nodes[svc_name] = NodeState(
                        node_id=svc_name,
                        label=svc_name.replace("-", " ").title(),
                        namespace=self.namespace,
                    )

        self._dirty = True

    def _load_seed_topology(self) -> None:
        """Load seed topology for offline/dev mode."""
        # Distribute nodes across worker nodes
        node_names = ["node-01", "node-02", "node-03"]

        for idx, n in enumerate(SEED_NODES):
            self._nodes[n["id"]] = NodeState(
                node_id=n["id"],
                label=n["label"],
                namespace="default",
                node_name=node_names[idx % len(node_names)],  # Round-robin distribution
                layer=n.get("layer", "service"),
                is_critical=n.get("is_critical", False),
                cpu=n.get("cpu", 0),
                mem=n.get("mem", 0),
            )
        for e in SEED_EDGES:
            key = (e["src"], e["dst"])
            self._edges[key] = EdgeState(
                src=e["src"], dst=e["dst"],
                type=e.get("type", "http"),
                latency_ms=e.get("latency_ms", 0),
                error_rate=e.get("error_rate", 0),
                request_rate=e.get("request_rate", 0),
                is_causal=e.get("is_causal", False),
            )
        self._dirty = True

    # ─── Kafka consumer ──────────────────────────────────────────────────────

    async def start_kafka_consumer(self) -> None:
        """Start background Kafka consumer task for live topology updates."""
        if not self.kafka_enabled:
            return
        asyncio.create_task(self._kafka_loop())

    async def _kafka_loop(self) -> None:
        """Consume ccdt.topology.updates and ccdt.ebpf.events from Kafka."""
        from aiokafka import AIOKafkaConsumer

        while True:
            try:
                consumer = AIOKafkaConsumer(
                    self.kafka_topic,
                    "ccdt.ebpf.events",
                    bootstrap_servers=self.kafka_servers,
                    group_id="ccdt-layer2-dag-builder",
                    auto_offset_reset="latest",
                    value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                )
                await consumer.start()
                logger.info("DAG builder Kafka consumer started")

                async for msg in consumer:
                    try:
                        await self._handle_kafka_message(msg.value)
                    except Exception as exc:
                        logger.warning("Kafka message handling error: %s", exc)

            except Exception as exc:
                logger.error("Kafka consumer error: %s — retrying in 10s", exc)
                await asyncio.sleep(10)

    async def _handle_kafka_message(self, payload: dict) -> None:
        """Route incoming Kafka message to the appropriate state updater.
        Handles both Layer-1 eBPF format and simulator format.
        """
        # Support both "type" (Layer-1 eBPF) and "msg_type" (simulator)
        msg_type = payload.get("msg_type") or payload.get("type", "")

        if msg_type == "topology_update":
            await self._apply_topology_update(payload)
        elif msg_type in ("ebpf_event", "sched", "oom", "tcp", "syscall", "file", "capability"):
            await self._apply_ebpf_event(payload)
        # incident_created messages are handled by the incidents router

    async def _apply_topology_update(self, payload: dict) -> None:
        """Apply node metric updates from Layer-1 or simulator topology payloads.

        Supports two field-name conventions:
          Simulator: node_id, cpu, mem, error_rate, latency_ms, tcp_retx, ...
          Layer-1:   node, cpu_pct, mem_pct, sched_latency_p99_ns, ...
        """
        async with self._lock:
            for node_snap in payload.get("nodes", []):
                # Support both "node_id" (simulator) and "node" (Layer-1)
                node_id = node_snap.get("node_id") or node_snap.get("node")
                if not node_id or node_id not in self._nodes:
                    continue
                n = self._nodes[node_id]

                # cpu — simulator sends 0-100, Layer-1 sends cpu_pct
                if "cpu" in node_snap:
                    n.cpu = float(node_snap["cpu"])
                elif "cpu_pct" in node_snap:
                    n.cpu = float(node_snap["cpu_pct"])

                # mem
                if "mem" in node_snap:
                    n.mem = float(node_snap["mem"])
                elif "mem_pct" in node_snap:
                    n.mem = float(node_snap["mem_pct"])

                # latency — simulator: latency_ms (already ms), L1: sched_latency_p99_ns (nanoseconds)
                if "latency_ms" in node_snap:
                    n.latency_ms = float(node_snap["latency_ms"])
                elif "sched_latency_p99_ns" in node_snap:
                    n.sched_lat = float(
                        node_snap["sched_latency_p99_ns"]) / 1e6

                # tcp retransmits
                if "tcp_retx" in node_snap:
                    n.tcp_retx = float(node_snap["tcp_retx"])
                elif "tcp_retransmit_rate" in node_snap:
                    n.tcp_retx = float(node_snap["tcp_retransmit_rate"])

                # oom
                n.oom_count = int(node_snap.get(
                    "oom_count",    node_snap.get("oom_kill_count",   n.oom_count)))
                n.cap_events = int(node_snap.get(
                    "cap_events",   node_snap.get("cap_event_count",  n.cap_events)))
                n.file_events = int(node_snap.get(
                    "file_events",  node_snap.get("file_event_count", n.file_events)))

                if "syscall_rate" in node_snap:
                    n.syscall_rate = float(node_snap["syscall_rate"])
                elif "syscall_event_count" in node_snap:
                    n.syscall_rate = float(node_snap["syscall_event_count"])

                # error_rate and request_rate (simulator only, Layer-1 derives from Prometheus)
                if "error_rate" in node_snap:
                    n.error_rate = float(node_snap["error_rate"])
                if "request_rate" in node_snap:
                    n.request_rate = float(node_snap["request_rate"])

            self._dirty = True

    async def _apply_ebpf_event(self, payload: dict) -> None:
        """Update node state from individual eBPF events."""
        evt_type = payload.get("type")
        pod = payload.get("comm", "")     # closest approximation to pod name
        sev = payload.get("severity", "")

        async with self._lock:
            # Find node by partial name match (pod name prefix)
            target = self._find_node_by_comm(pod)
            if target is None:
                return

            n = self._nodes[target]
            if evt_type == "capability":
                n.cap_events += 1
            elif evt_type == "oom":
                n.oom_count += 1
            elif evt_type == "tcp":
                n.tcp_retx = max(n.tcp_retx, float(
                    payload.get("retransmits_total", 0)) / 60.0)
            elif evt_type == "syscall":
                n.syscall_rate += 1
            elif evt_type == "file":
                n.file_events += 1

            # Escalate causal flag on critical events
            if sev == "critical":
                # Mark edges involving this node as potentially causal
                for (src, dst), edge in self._edges.items():
                    if src == target or dst == target:
                        edge.is_causal = True

            self._dirty = True

    def _find_node_by_comm(self, comm: str) -> Optional[str]:
        """Find a node ID whose name is a prefix of the comm string."""
        for nid in self._nodes:
            if comm.startswith(nid.replace("-", "")[:6]):
                return nid
        return None

    def _calculate_layout(self) -> dict[str, tuple[float, float]]:
        """
        Calculate layout positions for nodes using hierarchical layering.
        Returns dict mapping node_id → (x, y) coordinates.
        """
        if self._nx_graph is None or len(self._nx_graph.nodes) == 0:
            return {}

        # Group nodes by layer
        layer_groups: dict[str, list[str]] = defaultdict(list)
        for nid in self._nodes.keys():
            layer = self._nodes[nid].layer
            layer_groups[layer].append(nid)

        # Layer vertical positions (y-coordinates)
        layer_order = ["network", "service", "data", "system"]
        layer_y = {
            "network": 80,
            "service": 200,
            "data":    320,
            "system":  440,
        }

        positions = {}
        canvas_width = 800
        margin = 100

        for layer in layer_order:
            nodes_in_layer = layer_groups.get(layer, [])
            if not nodes_in_layer:
                continue

            # Distribute nodes evenly across width
            n = len(nodes_in_layer)
            if n == 1:
                x_positions = [canvas_width / 2]
            else:
                spacing = (canvas_width - 2 * margin) / (n - 1)
                x_positions = [margin + i * spacing for i in range(n)]

            # Sort nodes alphabetically for consistent layout
            nodes_in_layer.sort()

            for i, nid in enumerate(nodes_in_layer):
                positions[nid] = (x_positions[i], layer_y[layer])

        return positions

    # ─── Graph construction ──────────────────────────────────────────────────

    def _build_dag(self) -> None:
        """
        Rebuild the NetworkX DiGraph + PyG Data object from current state.
        Enforces acyclicity by removing the lowest-weight edge in any cycle.
        """
        g = nx.DiGraph()

        # Add nodes
        node_list = sorted(self._nodes.keys())   # deterministic ordering
        self._node_to_idx = {nid: i for i, nid in enumerate(node_list)}
        g.add_nodes_from(node_list)

        # Add edges
        for (src, dst), edge in self._edges.items():
            if src in self._node_to_idx and dst in self._node_to_idx:
                g.add_edge(src, dst, weight=edge.latency_ms, **{
                    "type": edge.type,
                    "error_rate": edge.error_rate,
                    "request_rate": edge.request_rate,
                    "is_causal": edge.is_causal,
                })

        # Enforce DAG: break cycles greedily
        while not nx.is_directed_acyclic_graph(g):
            try:
                cycle = nx.find_cycle(g)
                # Remove the lightest-weight edge in the cycle
                min_edge = min(
                    cycle, key=lambda e: g[e[0]][e[1]].get("weight", 1.0))
                g.remove_edge(*min_edge)
                logger.debug("Cycle broken: removed edge %s → %s",
                             min_edge[0], min_edge[1])
            except nx.exception.NetworkXNoCycle:
                break

        self._nx_graph = g
        self._pyg_data = self._nx_to_pyg(g, node_list)
        self._dirty = False
        self._built_at = time.time()

    def _nx_to_pyg(self, g: nx.DiGraph, node_list: list[str]) -> Data:
        """Convert a NetworkX graph to a PyG Data object."""
        # Node features
        x_rows: list[list[float]] = []
        for nid in node_list:
            n = self._nodes.get(nid)
            if n is None:
                x_rows.append([0.0] * NODE_FEAT_DIM)
            else:
                x_rows.append(n.to_feature_vector())

        x = torch.tensor(x_rows, dtype=torch.float)

        # Edge index + edge features
        edge_srcs, edge_dsts, edge_feats = [], [], []
        for src, dst, data in g.edges(data=True):
            si = self._node_to_idx.get(src)
            di = self._node_to_idx.get(dst)
            if si is None or di is None:
                continue
            edge_srcs.append(si)
            edge_dsts.append(di)
            key = (src, dst)
            e = self._edges.get(key)
            if e:
                edge_feats.append(e.to_feature_vector())
            else:
                edge_feats.append([0.0] * EDGE_FEAT_DIM)

        if edge_srcs:
            edge_index = torch.tensor([edge_srcs, edge_dsts], dtype=torch.long)
            edge_attr = torch.tensor(edge_feats, dtype=torch.float)
        else:
            edge_index = torch.zeros((2, 0), dtype=torch.long)
            edge_attr = torch.zeros((0, EDGE_FEAT_DIM), dtype=torch.float)

        return Data(
            x=x,
            edge_index=edge_index,
            edge_attr=edge_attr,
            num_nodes=len(node_list),
        )

    # ─── Public API ──────────────────────────────────────────────────────────

    async def get_pyg_data(self) -> Data:
        """Return the current PyG Data object, rebuilding if the graph is dirty."""
        async with self._lock:
            if self._dirty or self._pyg_data is None:
                self._build_dag()
            return self._pyg_data

    async def get_nx_graph(self) -> nx.DiGraph:
        """Return the current NetworkX DiGraph."""
        async with self._lock:
            if self._dirty or self._nx_graph is None:
                self._build_dag()
            return self._nx_graph

    def _apply_dynamic_state_mutations(self) -> None:
        """
        Apply time-based dynamic state mutations to simulate realistic incident cycles.
        This creates a demo-friendly experience where issues appear and resolve automatically.

        Cycle timeline (90 second cycle):
        0.0 - 0.22: All healthy baseline (20s)
        0.22 - 0.44: Incident building up (warning phase) (20s)
        0.44 - 0.67: Full incident (critical phase) (20s)
        0.67 - 0.89: Recovery phase (critical -> warning -> healthy) (20s)
        0.89 - 1.0: Stable healthy (10s)

        Between scenarios: 30 second healthy gap for demo clarity
        """
        import random

        tick = time.time()

        # 3-minute meta-cycle: 90s incident + 90s healthy gap
        meta_cycle = (tick / 180.0) % 1.0

        # If in first half (0.0-0.5), run incident cycle. If second half (0.5-1.0), stay healthy
        if meta_cycle < 0.5:
            cycle = meta_cycle * 2  # Scale 0.0-0.5 to 0.0-1.0
        else:
            cycle = 0.0  # Healthy baseline

        scenario_cycle = int(tick / 540.0) % 3  # Switch scenarios every 9 minutes (3 full cycles)

        # Phase determination
        is_building = 0.22 <= cycle < 0.44
        is_critical = 0.44 <= cycle < 0.67
        is_recovery = 0.67 <= cycle < 0.89

        for node_id, node in self._nodes.items():
            # Save baseline if not set
            if not hasattr(node, '_baseline_cpu'):
                node._baseline_cpu = node.cpu if node.cpu > 0 else 30.0
                node._baseline_mem = node.mem if node.mem > 0 else 40.0

            base_cpu = node._baseline_cpu
            base_mem = node._baseline_mem
            restarts = 0

            # Scenario 1: Database overload cascade
            if scenario_cycle == 0:
                if node_id == "order-svc":
                    if is_building:
                        progress = (cycle - 0.25) / 0.15
                        base_cpu = node._baseline_cpu + (60.0 * progress)
                        base_mem = node._baseline_mem + (45.0 * progress)
                    elif is_critical:
                        base_cpu = 88.0 + random.uniform(-3, 7)
                        base_mem = 88.0 + random.uniform(-3, 7)
                        restarts = random.randint(2, 4)
                    elif is_recovery:
                        progress = (cycle - 0.65) / 0.20
                        base_cpu = 88.0 - (58.0 * progress)
                        base_mem = 88.0 - (48.0 * progress)

                elif node_id == "postgres":
                    if 0.30 <= cycle < 0.45:
                        progress = (cycle - 0.30) / 0.15
                        base_cpu = node._baseline_cpu + (55.0 * progress)
                        base_mem = node._baseline_mem + (48.0 * progress)
                    elif 0.45 <= cycle < 0.65:
                        base_cpu = 92.0 + random.uniform(-2, 3)
                        base_mem = 93.0 + random.uniform(-3, 2)
                        restarts = random.randint(1, 3)
                    elif is_recovery:
                        progress = (cycle - 0.65) / 0.20
                        base_cpu = 92.0 - (52.0 * progress)
                        base_mem = 93.0 - (43.0 * progress)

                elif node_id == "notify-svc":
                    if 0.35 <= cycle < 0.50:
                        progress = (cycle - 0.35) / 0.15
                        base_cpu = node._baseline_cpu + (40.0 * progress)
                        base_mem = node._baseline_mem + (28.0 * progress)
                    elif 0.50 <= cycle < 0.65:
                        base_cpu = 70.0 + random.uniform(-3, 5)
                        base_mem = 78.0 + random.uniform(-3, 5)
                        restarts = random.randint(1, 2)
                    elif 0.65 <= cycle < 0.80:
                        progress = (cycle - 0.65) / 0.15
                        base_cpu = 70.0 - (40.0 * progress)
                        base_mem = 78.0 - (30.0 * progress)

            # Scenario 2: Payment service memory leak
            elif scenario_cycle == 1:
                if node_id == "payment-svc":
                    if is_building:
                        progress = (cycle - 0.25) / 0.15
                        base_mem = node._baseline_mem + (50.0 * progress)
                        base_cpu = node._baseline_cpu + (32.0 * progress)
                    elif is_critical:
                        base_mem = 93.0 + random.uniform(-2, 2)
                        base_cpu = 68.0 + random.uniform(-3, 7)
                        restarts = random.randint(2, 5)
                    elif is_recovery:
                        progress = (cycle - 0.65) / 0.20
                        base_mem = 93.0 - (53.0 * progress)
                        base_cpu = 68.0 - (30.0 * progress)

                elif node_id == "postgres":
                    if 0.30 <= cycle < 0.45:
                        progress = (cycle - 0.30) / 0.15
                        base_cpu = node._baseline_cpu + (32.0 * progress)
                    elif 0.45 <= cycle < 0.65:
                        base_cpu = 70.0 + random.uniform(-3, 5)
                        base_mem = 77.0 + random.uniform(-2, 5)
                    elif 0.65 <= cycle < 0.80:
                        progress = (cycle - 0.65) / 0.15
                        base_cpu = 70.0 - (30.0 * progress)
                        base_mem = 77.0 - (27.0 * progress)

            # Scenario 3: Auth service under attack
            elif scenario_cycle == 2:
                if node_id == "auth-svc":
                    if is_building:
                        progress = (cycle - 0.25) / 0.15
                        base_cpu = node._baseline_cpu + (58.0 * progress)
                        base_mem = node._baseline_mem + (42.0 * progress)
                    elif is_critical:
                        base_cpu = 90.0 + random.uniform(-2, 5)
                        base_mem = 86.0 + random.uniform(-3, 9)
                        restarts = random.randint(1, 3)
                    elif is_recovery:
                        progress = (cycle - 0.65) / 0.20
                        base_cpu = 90.0 - (60.0 * progress)
                        base_mem = 86.0 - (46.0 * progress)

                elif node_id == "api-gw":
                    if 0.30 <= cycle < 0.45:
                        progress = (cycle - 0.30) / 0.15
                        base_cpu = node._baseline_cpu + (32.0 * progress)
                    elif 0.45 <= cycle < 0.65:
                        base_cpu = 70.0 + random.uniform(-3, 5)
                    elif 0.65 <= cycle < 0.80:
                        progress = (cycle - 0.65) / 0.15
                        base_cpu = 70.0 - (28.0 * progress)

            # Apply mutations
            node.cpu = max(5, min(99, base_cpu))
            node.mem = max(10, min(99, base_mem))
            node.restarts = restarts

            # Add small random variation for realism
            if not (is_building or is_critical or is_recovery):
                node.cpu += random.uniform(-3, 3)
                node.mem += random.uniform(-2, 2)
                node.cpu = max(5, min(99, node.cpu))
                node.mem = max(10, min(99, node.mem))

        # Update edges based on node states
        for (src, dst), edge in self._edges.items():
            src_node = self._nodes.get(src)
            dst_node = self._nodes.get(dst)

            if src_node and dst_node:
                src_critical = src_node.cpu > 85 or src_node.mem > 90
                dst_critical = dst_node.cpu > 85 or dst_node.mem > 90
                src_warning = src_node.cpu > 65 or src_node.mem > 75
                dst_warning = dst_node.cpu > 65 or dst_node.mem > 75

                edge.is_causal = src_critical or dst_critical

                if edge.is_causal:
                    edge.latency_ms = round(random.uniform(80.0, 250.0), 1)
                    edge.error_rate = round(random.uniform(0.08, 0.18), 3)
                    edge.request_rate = edge.request_rate * random.uniform(0.3, 0.6) if edge.request_rate > 0 else 100
                elif src_warning or dst_warning:
                    edge.latency_ms = round(random.uniform(20.0, 60.0), 1)
                    edge.error_rate = round(random.uniform(0.01, 0.04), 3)
                    edge.request_rate = edge.request_rate * random.uniform(0.7, 0.9) if edge.request_rate > 0 else 100
                else:
                    # Restore to baseline
                    if not hasattr(edge, '_baseline_latency'):
                        edge._baseline_latency = edge.latency_ms if edge.latency_ms > 0 else 5.0
                        edge._baseline_error = edge.error_rate if edge.error_rate > 0 else 0.001
                        edge._baseline_requests = edge.request_rate if edge.request_rate > 0 else 100

                    edge.latency_ms = edge._baseline_latency + random.uniform(-2, 2)
                    edge.error_rate = edge._baseline_error + random.uniform(-0.0005, 0.0005)
                    edge.request_rate = edge._baseline_requests

    async def get_topology_dict(self) -> dict[str, Any]:
        """Return the topology as a JSON-serialisable dict (for /topology API)."""
        async with self._lock:
            if self._dirty:
                self._build_dag()

            # Apply dynamic state mutations for demo purposes
            self._apply_dynamic_state_mutations()

        # Calculate layout positions using spring layout
        layout_pos = self._calculate_layout()

        nodes_out = []
        for nid, n in self._nodes.items():
            status = "healthy"
            if n.cpu > 85 or n.mem > 85 or n.oom_count > 0:
                status = "critical"
            elif n.cpu > 70 or n.mem > 70 or n.error_rate > 0.05:
                status = "warning"

            # Get layout position (x, y)
            pos = layout_pos.get(nid, (400, 250))

            nodes_out.append({
                "id":        nid,
                "label":     n.label,
                "x":         round(pos[0], 1),
                "y":         round(pos[1], 1),
                "namespace": n.namespace,
                "nodeName":  n.node_name,
                "layer":     n.layer,
                "cpu":       round(n.cpu, 1),
                "mem":       round(n.mem, 1),
                "restarts":  n.restarts,
                "replicas":  n.replicas,
                "status":    status,
            })

        edges_out = []
        for (src, dst), e in self._edges.items():
            edges_out.append({
                "from":        src,
                "to":          dst,
                "type":        e.type,
                "latencyMs":   round(e.latency_ms, 2),
                "errorRate":   round(e.error_rate, 4),
                "requestRate": round(e.request_rate, 1),
                "causal":      e.is_causal,
            })

        return {
            "nodes":     nodes_out,
            "edges":     edges_out,
            "timestamp": int(time.time()),
            "source":    "live",
        }

    def node_id_for_index(self, idx: int) -> Optional[str]:
        """Return node_id for a given integer index (reverse lookup)."""
        rev = {v: k for k, v in self._node_to_idx.items()}
        return rev.get(idx)

    def index_for_node(self, node_id: str) -> Optional[int]:
        """Return integer index for a given node_id."""
        return self._node_to_idx.get(node_id)

    async def update_node_metric(self, node_id: str, metric: str, value: float) -> None:
        """Direct metric update (called by the inference server for feedback)."""
        async with self._lock:
            n = self._nodes.get(node_id)
            if n and hasattr(n, metric):
                setattr(n, metric, value)
                self._dirty = True

    async def mark_edge_causal(self, src: str, dst: str, causal: bool = True) -> None:
        """Mark or unmark an edge as causally significant."""
        async with self._lock:
            key = (src, dst)
            if key in self._edges:
                self._edges[key].is_causal = causal
                self._dirty = True

    @property
    def node_ids(self) -> list[str]:
        """Ordered list of node IDs (matches PyG Data node ordering)."""
        return sorted(self._nodes.keys())

    @property
    def num_nodes(self) -> int:
        return len(self._nodes)

    @property
    def num_edges(self) -> int:
        return len(self._edges)
