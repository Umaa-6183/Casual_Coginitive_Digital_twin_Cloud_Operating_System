"""
CCDT Layer-2 Cognitive Core — Training Dataset
────────────────────────────────────────────────
CausalIncidentDataset generates synthetic cluster incident graphs for
supervised training of the CausalGNN model.

Each graph sample represents a point-in-time cluster snapshot with:
  - Node features (17-dim per node)
  - Edge connectivity (directed, acyclic)
  - Node-level labels: 0=healthy, 1=fault, 2=attack
  - Graph-level label: dominant incident type

Two scenario families are supported:
  fault_scenarios   — hardware failures, OOM cascades, CPU saturation
  attack_scenarios  — privilege escalation, container escape, lateral movement

Scenario definitions are loaded from JSON files in:
  data/fault_scenarios/*.json
  data/attack_scenarios/*.json

Each scenario JSON defines:
  {
    "name": "oom_cascade",
    "graph_label": 1,
    "affected_nodes": ["postgres", "order-svc"],
    "node_labels": {"postgres": 1, "order-svc": 2, ...},
    "metric_overrides": {"postgres": {"oom_count": 3, "cpu": 95}, ...},
    "edge_overrides": [{"src": "order-svc", "dst": "postgres", "is_causal": true}]
  }
"""
from __future__ import annotations

import copy
import json
import logging
import os
import random
from pathlib import Path
from typing import Optional

import numpy as np
import torch
from torch_geometric.data import Data, Dataset, InMemoryDataset

logger = logging.getLogger("ccdt.cognitive.dataset")

# ─── Constants ────────────────────────────────────────────────────────────────
NODE_FEAT_DIM = 17
EDGE_FEAT_DIM = 4
NUM_CLASSES   = 3      # 0=healthy, 1=fault, 2=attack
HEALTHY       = 0
FAULT         = 1
ATTACK        = 2

# Seed topology (same as dag_builder.py for consistency)
_NODES = [
    {"id": "api-gw",        "layer": "network", "is_critical": True,  "is_external": False},
    {"id": "auth-svc",      "layer": "service", "is_critical": False, "is_external": False},
    {"id": "order-svc",     "layer": "service", "is_critical": True,  "is_external": False},
    {"id": "payment-svc",   "layer": "service", "is_critical": True,  "is_external": False},
    {"id": "inventory-svc", "layer": "service", "is_critical": False, "is_external": False},
    {"id": "notify-svc",    "layer": "service", "is_critical": False, "is_external": False},
    {"id": "postgres",      "layer": "data",    "is_critical": True,  "is_external": False},
    {"id": "redis",         "layer": "data",    "is_critical": False, "is_external": False},
    {"id": "kafka",         "layer": "system",  "is_critical": True,  "is_external": False},
    {"id": "monitoring",    "layer": "system",  "is_critical": False, "is_external": False},
]

_EDGES = [
    ("api-gw",        "auth-svc"),
    ("api-gw",        "order-svc"),
    ("api-gw",        "payment-svc"),
    ("order-svc",     "postgres"),
    ("order-svc",     "notify-svc"),
    ("payment-svc",   "postgres"),
    ("inventory-svc", "postgres"),
    ("notify-svc",    "kafka"),
    ("order-svc",     "redis"),
    ("monitoring",    "kafka"),
]

_NODE_IDS    = [n["id"] for n in _NODES]
_NODE_TO_IDX = {n["id"]: i for i, n in enumerate(_NODES)}

LAYER_CODES  = {"network": (0, 0), "service": (0, 1), "data": (1, 0), "system": (1, 1)}

# ─── Synthetic scenario templates ────────────────────────────────────────────
# Used when JSON scenario files are not found
_BUILTIN_SCENARIOS = [
    # ── Fault scenarios ───────────────────────────────────────────────────────
    {
        "name": "oom_cascade",
        "graph_label": FAULT,
        "node_labels":  {"postgres": FAULT, "order-svc": FAULT, "notify-svc": FAULT},
        "metric_overrides": {
            "postgres":  {"oom_count": 3, "cpu": 95.0, "mem": 92.0, "error_rate": 0.18},
            "order-svc": {"tcp_retx": 120, "latency_ms": 200, "error_rate": 0.12},
            "notify-svc":{"sched_lat": 80.0, "cpu": 74.0},
        },
        "edge_overrides": [
            {"src": "order-svc", "dst": "postgres", "is_causal": True, "error_rate": 0.18},
        ],
    },
    {
        "name": "cpu_saturation",
        "graph_label": FAULT,
        "node_labels":  {"order-svc": FAULT, "notify-svc": FAULT},
        "metric_overrides": {
            "order-svc": {"cpu": 98.0, "sched_lat": 150.0, "latency_ms": 300.0},
            "notify-svc":{"cpu": 85.0, "sched_lat": 90.0},
        },
        "edge_overrides": [],
    },
    {
        "name": "redis_eviction",
        "graph_label": FAULT,
        "node_labels":  {"redis": FAULT, "order-svc": FAULT},
        "metric_overrides": {
            "redis":     {"mem": 98.0, "error_rate": 0.15, "latency_ms": 45.0},
            "order-svc": {"latency_ms": 120.0, "error_rate": 0.08},
        },
        "edge_overrides": [
            {"src": "order-svc", "dst": "redis", "is_causal": True, "error_rate": 0.12},
        ],
    },
    {
        "name": "kafka_lag",
        "graph_label": FAULT,
        "node_labels":  {"kafka": FAULT, "notify-svc": FAULT},
        "metric_overrides": {
            "kafka":     {"cpu": 88.0, "latency_ms": 250.0},
            "notify-svc":{"error_rate": 0.09, "latency_ms": 180.0},
        },
        "edge_overrides": [
            {"src": "notify-svc", "dst": "kafka", "is_causal": True},
        ],
    },
    # ── Attack scenarios ──────────────────────────────────────────────────────
    {
        "name": "privilege_escalation",
        "graph_label": ATTACK,
        "node_labels":  {"order-svc": ATTACK, "postgres": FAULT},
        "metric_overrides": {
            "order-svc": {"cap_events": 5, "syscall_rate": 3200, "cpu": 94.0},
            "postgres":  {"oom_count": 2, "cpu": 91.0, "mem": 89.0},
        },
        "edge_overrides": [
            {"src": "order-svc", "dst": "postgres", "is_causal": True, "error_rate": 0.12},
        ],
    },
    {
        "name": "lateral_movement",
        "graph_label": ATTACK,
        "node_labels":  {"auth-svc": ATTACK, "order-svc": ATTACK, "payment-svc": FAULT},
        "metric_overrides": {
            "auth-svc":    {"cap_events": 3, "file_events": 8, "syscall_rate": 1800},
            "order-svc":   {"cap_events": 2, "syscall_rate": 2400},
            "payment-svc": {"error_rate": 0.07, "tcp_retx": 45},
        },
        "edge_overrides": [
            {"src": "api-gw", "dst": "auth-svc",    "is_causal": True},
            {"src": "api-gw", "dst": "order-svc",   "is_causal": True},
        ],
    },
    {
        "name": "container_escape",
        "graph_label": ATTACK,
        "node_labels":  {"notify-svc": ATTACK},
        "metric_overrides": {
            "notify-svc": {
                "cap_events": 8, "file_events": 15, "syscall_rate": 5000,
                "cpu": 78.0,     "oom_count": 1,
            },
        },
        "edge_overrides": [],
    },
    {
        "name": "credential_theft",
        "graph_label": ATTACK,
        "node_labels":  {"payment-svc": ATTACK},
        "metric_overrides": {
            "payment-svc": {
                "file_events": 12, "syscall_rate": 2800, "cap_events": 4,
            },
        },
        "edge_overrides": [],
    },
    # ── Healthy baseline ──────────────────────────────────────────────────────
    {
        "name": "healthy_baseline",
        "graph_label": HEALTHY,
        "node_labels":  {},
        "metric_overrides": {},
        "edge_overrides": [],
    },
]


# ─── Feature generation helpers ───────────────────────────────────────────────

def _healthy_node_features(
    node: dict,
    rng: random.Random,
) -> list[float]:
    """Generate healthy-state node features with realistic noise."""
    l0, l1 = LAYER_CODES.get(node["layer"], (0, 1))
    cpu     = rng.uniform(10, 55)
    mem     = rng.uniform(20, 65)
    return [
        cpu / 100.0,
        mem / 100.0,
        rng.uniform(0, 0.03),           # sched_lat_p99 (normalised)
        rng.uniform(0, 0.02),           # tcp_retx
        0.0,                            # oom_count
        0.0,                            # cap_events
        rng.uniform(0.005, 0.03),       # syscall_rate
        0.0,                            # file_events
        rng.uniform(0, 0.008),          # error_rate
        rng.uniform(0.05, 0.5),         # request_rate
        rng.uniform(0.002, 0.04),       # latency_ms
        float(rng.randint(0, 1)),       # restarts
        min(rng.randint(1, 3) / 10.0, 1.0),  # replicas
        float(node.get("is_critical", False)),
        float(node.get("is_external", False)),
        float(l0),
        float(l1),
    ]


def _apply_metric_overrides(
    feat: list[float],
    overrides: dict,
    rng: random.Random,
) -> list[float]:
    """Apply scenario metric overrides onto a feature vector with noise."""
    feat = feat.copy()
    FEAT_IDX = {
        "cpu":          0,  "mem":          1,  "sched_lat":    2,
        "tcp_retx":     3,  "oom_count":    4,  "cap_events":   5,
        "syscall_rate": 6,  "file_events":  7,  "error_rate":   8,
        "request_rate": 9,  "latency_ms":   10, "restarts":     11,
        "replicas":     12,
    }
    NORM_FACTORS = {
        "cpu": 100.0, "mem": 100.0, "sched_lat": 200.0, "tcp_retx": 500.0,
        "oom_count": 10.0, "cap_events": 20.0, "syscall_rate": 10000.0,
        "file_events": 50.0, "error_rate": 1.0, "request_rate": 1000.0,
        "latency_ms": 500.0, "restarts": 10.0, "replicas": 10.0,
    }

    for metric, val in overrides.items():
        if metric in FEAT_IDX:
            noise = rng.gauss(0, 0.03)
            norm  = NORM_FACTORS.get(metric, 1.0)
            feat[FEAT_IDX[metric]] = max(0.0, min(1.0, val / norm + noise))
    return feat


# ─── Graph sample builder ────────────────────────────────────────────────────

def _build_graph_sample(
    scenario: dict,
    rng: random.Random,
    augment: bool = True,
) -> Data:
    """Build a single PyG Data sample from a scenario definition."""
    node_features  = []
    node_labels    = []
    scenario_labels = scenario.get("node_labels", {})
    metric_ovr     = scenario.get("metric_overrides", {})

    for node in _NODES:
        nid  = node["id"]
        feat = _healthy_node_features(node, rng)

        # Apply metric overrides for this node
        if nid in metric_ovr:
            feat = _apply_metric_overrides(feat, metric_ovr[nid], rng)

        node_features.append(feat)
        node_labels.append(scenario_labels.get(nid, HEALTHY))

    # Build edges
    edge_overrides = {(e["src"], e["dst"]): e for e in scenario.get("edge_overrides", [])}
    ei_srcs, ei_dsts, ea_feats = [], [], []

    for src, dst in _EDGES:
        si = _NODE_TO_IDX.get(src)
        di = _NODE_TO_IDX.get(dst)
        if si is None or di is None:
            continue

        ovr = edge_overrides.get((src, dst), {})
        lat_n   = ovr.get("latency_ms",   rng.uniform(1, 15)) / 500.0
        err_n   = ovr.get("error_rate",   rng.uniform(0, 0.01))
        req_n   = ovr.get("request_rate", rng.uniform(10, 300)) / 1000.0
        causal  = float(ovr.get("is_causal", False))

        # Add noise
        if augment:
            lat_n += rng.gauss(0, 0.01)
            err_n += rng.gauss(0, 0.002)

        ei_srcs.append(si)
        ei_dsts.append(di)
        ea_feats.append([
            max(0.0, min(1.0, lat_n)),
            max(0.0, min(1.0, err_n)),
            max(0.0, min(1.0, req_n)),
            causal,
        ])

    x          = torch.tensor(node_features, dtype=torch.float)
    edge_index = torch.tensor([ei_srcs, ei_dsts], dtype=torch.long)
    edge_attr  = torch.tensor(ea_feats, dtype=torch.float)
    y_node     = torch.tensor(node_labels, dtype=torch.long)
    y_graph    = torch.tensor([scenario["graph_label"]], dtype=torch.long)

    return Data(
        x=x,
        edge_index=edge_index,
        edge_attr=edge_attr,
        y=y_node,
        graph_y=y_graph,
        num_nodes=len(_NODES),
        scenario_name=scenario.get("name") or scenario.get("scenario_id", "unknown"),
    )


# ─── CausalIncidentDataset ────────────────────────────────────────────────────

class CausalIncidentDataset(InMemoryDataset):
    """
    In-memory PyG dataset of synthetic cluster incident graphs.

    Args:
        root:            Root directory for dataset storage (for caching)
        scenario_dir:    Path to data/fault_scenarios + data/attack_scenarios
        num_samples:     Total number of graphs to generate
        val_split:       Fraction of data for validation (0..1)
        augment:         Apply random metric noise during generation
        seed:            Random seed for reproducibility
        split:           "train" | "val" | "test"
    """

    def __init__(
        self,
        root:          str   = "/tmp/ccdt_dataset",
        scenario_dir:  Optional[str] = None,
        num_samples:   int   = 2000,
        val_split:     float = 0.15,
        test_split:    float = 0.10,
        augment:       bool  = True,
        seed:          int   = 42,
        split:         str   = "train",
    ) -> None:
        self.scenario_dir = scenario_dir
        self.num_samples  = num_samples
        self.val_split    = val_split
        self.test_split   = test_split
        self.augment      = augment
        self.seed         = seed
        self.split        = split

        super().__init__(root=root)

        # Load or generate
        self._all_data = self._generate()
        self._split_data()

    def _load_scenarios_from_json(self) -> list[dict]:
        """Load scenario definitions from JSON files in the scenario directory.

        The JSON files on disk use a rich node-list format:
          {
            "scenario_id": "fault_oom_cascade",
            "nodes": [{"id": "postgres", "cpu": 95, "node_label": 1, ...}, ...],
            "edges": [{"from": "a", "to": "b", "latency_ms": 200, "causal": true}, ...]
          }

        _build_graph_sample expects the compact schema:
          {
            "name":            str,
            "graph_label":     int,          # 0=healthy, 1=fault, 2=attack
            "node_labels":     {node_id: int},
            "metric_overrides":{node_id: {metric: value}},
            "edge_overrides":  [{"src": str, "dst": str, "latency_ms": ..., "is_causal": bool}]
          }

        This method translates the disk format → compact schema transparently.
        Scenarios already in compact schema (have "name" key) are passed through.
        """
        scenarios: list[dict] = []
        if not self.scenario_dir or not os.path.isdir(self.scenario_dir):
            return []

        for subdir in ("fault_scenarios", "attack_scenarios"):
            path = Path(self.scenario_dir) / subdir
            if not path.exists():
                continue
            for json_file in path.glob("*.json"):
                try:
                    with open(json_file) as f:
                        raw = json.load(f)

                    # ── Already in compact schema → pass through ──────────────
                    if "name" in raw and "graph_label" in raw:
                        scenarios.append(raw)
                        logger.debug("Loaded compact scenario: %s", json_file.name)
                        continue

                    # ── Translate rich node-list schema → compact schema ───────
                    sc = self._translate_json_scenario(raw, json_file.name)
                    if sc is not None:
                        scenarios.append(sc)
                        logger.debug("Translated scenario: %s → %s", json_file.name, sc["name"])

                except Exception as exc:
                    logger.warning("Failed to load %s: %s", json_file, exc)

        return scenarios

    @staticmethod
    def _translate_json_scenario(raw: dict, filename: str) -> dict | None:
        """Translate the rich on-disk JSON format to the compact scenario schema.

        JSON format (on disk):
          scenario_id   str        e.g. "fault_oom_cascade"
          nodes         list       [{id, cpu, mem, restarts, oom_kills,
                                     tcp_retransmit_rate, sched_latency_p99_ms,
                                     cap_event, file_event, node_label}, ...]
          edges         list       [{from, to, latency_ms, error_rate,
                                     request_rate, causal}, ...]

        Compact schema (_build_graph_sample):
          name, graph_label, node_labels, metric_overrides, edge_overrides
        """
        try:
            sid = raw.get("scenario_id") or raw.get("id") or filename.replace(".json", "")

            # ── Derive graph_label from scenario_id prefix ─────────────────────
            # attack_* → ATTACK(2),  fault_* → FAULT(1),  else → HEALTHY(0)
            if sid.startswith("attack"):
                graph_label = ATTACK
            elif sid.startswith("fault"):
                graph_label = FAULT
            else:
                # Fallback: use max node_label across all nodes
                labels = [n.get("node_label", 0) for n in raw.get("nodes", [])]
                graph_label = max(labels) if labels else HEALTHY

            # ── Build node_labels dict ────────────────────────────────────────
            node_labels: dict[str, int] = {}
            for node in raw.get("nodes", []):
                nid = node.get("id")
                if nid:
                    node_labels[nid] = int(node.get("node_label", HEALTHY))

            # ── Build metric_overrides dict ───────────────────────────────────
            # Map JSON field names → dataset metric names
            METRIC_MAP = {
                "cpu":                   "cpu",
                "mem":                   "mem",
                "sched_latency_p99_ms":  "sched_lat",
                "tcp_retransmit_rate":   "tcp_retx",
                "oom_kills":             "oom_count",
                "cap_event":             "cap_events",
                "file_event":            "file_events",
                "restarts":              "restarts",
            }
            metric_overrides: dict[str, dict] = {}
            for node in raw.get("nodes", []):
                nid = node.get("id")
                if not nid:
                    continue
                ovr: dict[str, float] = {}
                for json_key, ds_key in METRIC_MAP.items():
                    val = node.get(json_key)
                    if val is not None:
                        ovr[ds_key] = float(val)
                if ovr:
                    metric_overrides[nid] = ovr

            # ── Build edge_overrides list ─────────────────────────────────────
            # JSON uses "from"/"to" and "causal"; compact uses "src"/"dst"/"is_causal"
            edge_overrides: list[dict] = []
            for edge in raw.get("edges", []):
                src = edge.get("from") or edge.get("src")
                dst = edge.get("to")   or edge.get("dst")
                if not src or not dst:
                    continue
                edge_overrides.append({
                    "src":          src,
                    "dst":          dst,
                    "latency_ms":   edge.get("latency_ms",   5.0),
                    "error_rate":   edge.get("error_rate",   0.0),
                    "request_rate": edge.get("request_rate", 100.0),
                    "is_causal":    bool(edge.get("causal", False)),
                })

            return {
                "name":             sid,
                "graph_label":      graph_label,
                "node_labels":      node_labels,
                "metric_overrides": metric_overrides,
                "edge_overrides":   edge_overrides,
            }

        except Exception as exc:
            logger.warning("Could not translate scenario %s: %s", filename, exc)
            return None

    def _generate(self) -> list[Data]:
        """Generate all graph samples from builtin + JSON scenarios."""
        rng = random.Random(self.seed)

        # Combine builtin + JSON scenarios
        scenarios = _BUILTIN_SCENARIOS + self._load_scenarios_from_json()
        logger.info("Generating %d samples from %d scenarios", self.num_samples, len(scenarios))

        data_list: list[Data] = []
        samples_per_scenario  = max(1, self.num_samples // len(scenarios))
        extra = self.num_samples - samples_per_scenario * len(scenarios)

        for i, sc in enumerate(scenarios):
            n = samples_per_scenario + (1 if i < extra else 0)
            for _ in range(n):
                # Augment scenario: randomly inject slight metric drift
                sc_aug = copy.deepcopy(sc)
                if self.augment:
                    for node_id, metrics in sc_aug.get("metric_overrides", {}).items():
                        for k, v in metrics.items():
                            if isinstance(v, float):
                                metrics[k] = v * rng.uniform(0.85, 1.15)
                data_list.append(_build_graph_sample(sc_aug, rng, self.augment))

        # Shuffle deterministically
        rng.shuffle(data_list)
        return data_list

    def _split_data(self) -> None:
        """Partition data into train / val / test."""
        n     = len(self._all_data)
        n_val = int(n * self.val_split)
        n_tst = int(n * self.test_split)
        n_trn = n - n_val - n_tst

        splits = {
            "train": self._all_data[:n_trn],
            "val":   self._all_data[n_trn: n_trn + n_val],
            "test":  self._all_data[n_trn + n_val:],
        }
        data, slices = self.collate(splits[self.split])
        self.data, self.slices = data, slices

    # ── InMemoryDataset interface ────────────────────────────────────────────

    @property
    def raw_file_names(self) -> list[str]:
        return []

    @property
    def processed_file_names(self) -> list[str]:
        return []

    def download(self) -> None:
        pass   # no download needed — data is generated

    def process(self) -> None:
        pass   # data generated in __init__

    def len(self) -> int:
        if self.slices is None:
            return 0
        return self.slices["x"].size(0) - 1

    def get(self, idx: int) -> Data:
        data = super().get(idx)
        return data

    # ── Convenience ─────────────────────────────────────────────────────────

    def class_weights(self) -> torch.Tensor:
        """
        Compute inverse-frequency class weights for imbalanced training.
        Returns tensor of shape (NUM_CLASSES,).
        """
        counts = torch.zeros(NUM_CLASSES)
        for i in range(len(self)):
            d = self.get(i)
            for c in d.y.tolist():
                counts[c] += 1
        weights = counts.sum() / (NUM_CLASSES * counts.clamp(min=1))
        return weights

    @staticmethod
    def node_ids() -> list[str]:
        return _NODE_IDS

    @staticmethod
    def feature_names() -> list[str]:
        return [
            "cpu_norm", "mem_norm", "sched_lat_p99_norm", "tcp_retx_norm",
            "oom_count_norm", "cap_events_norm", "syscall_rate_norm",
            "file_events_norm", "error_rate", "request_rate_norm",
            "latency_ms_norm", "restarts_norm", "replicas_norm",
            "is_critical", "is_external", "layer_bit0", "layer_bit1",
        ]
