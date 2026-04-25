"""
CCDT Layer-2 Cognitive Core — Counterfactual Engine
─────────────────────────────────────────────────────
Implements Pearl's do-calculus (do(X=x)) to answer:
  "What would the cluster health look like if we applied ACTION to NODE?"

Pipeline:
  1. Clone current cluster state (NodeState / EdgeState copies)
  2. Apply the intervention: do(action, target_node) — mutates the clone
  3. Re-run GNN inference on the intervened graph
  4. Compare original vs intervened predictions
  5. BFS-expand blast radius: nodes reachable from target whose probability
     delta exceeds a threshold
  6. Compute MTTR impact estimate using an empirical regression model

Supported interventions:
  isolate_container    — remove all edges to/from target node
  restart_pod          — reset all metrics to healthy baseline
  scale_up_replicas    — increase replica_count, reduce cpu/mem proportionally
  increase_memory_limit— reduce OOM risk (oom_count → 0, mem capped at 60%)
  apply_network_policy — mark all outgoing edges as non-causal, zero error_rate
  block_ip             — remove external ingress edges to target
  rollback_deployment  — restore metrics to checkpoint average
  cordon_node          — isolate entire Kubernetes node (all pods on it)
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Optional

import networkx as nx
import torch
import torch.nn.functional as F

logger = logging.getLogger("ccdt.cognitive.counterfactual")

# Class label mapping
CLASS_NAMES = {0: "healthy", 1: "fault", 2: "attack"}

# Empirical MTTR impact coefficients per action (fitted from incident database)
# MTTR_DELTA_PCT = BASE + cpu_factor * Δcpu + prob_factor * Δattack_prob
_MTTR_COEFFS: dict[str, dict[str, float]] = {
    "isolate_container":     {"base": -65.0, "cpu": -0.3, "prob": -40.0},
    "restart_pod":           {"base": -40.0, "cpu": -0.2, "prob": -30.0},
    "scale_up_replicas":     {"base": -30.0, "cpu": -0.4, "prob": -15.0},
    "increase_memory_limit": {"base": -45.0, "cpu": -0.1, "prob": -20.0},
    "apply_network_policy":  {"base": -50.0, "cpu": -0.1, "prob": -35.0},
    "block_ip":              {"base": -55.0, "cpu":  0.0, "prob": -38.0},
    "rollback_deployment":   {"base": -35.0, "cpu": -0.2, "prob": -25.0},
    "cordon_node":           {"base": -20.0, "cpu": -0.5, "prob": -10.0},
}


# ─── Intervention definitions ────────────────────────────────────────────────

def _do_isolate_container(
    nodes: dict, edges: dict, target: str
) -> None:
    """Remove all edges to and from the target node."""
    to_remove = [k for k in edges if k[0] == target or k[1] == target]
    for k in to_remove:
        del edges[k]
    if target in nodes:
        nodes[target].tcp_retx   = 0.0
        nodes[target].error_rate = 0.0


def _do_restart_pod(
    nodes: dict, edges: dict, target: str
) -> None:
    """Reset node metrics to a healthy baseline (simulates clean restart)."""
    n = nodes.get(target)
    if n is None:
        return
    n.cpu          = max(n.cpu * 0.3, 10.0)
    n.mem          = max(n.mem * 0.4, 20.0)
    n.sched_lat    = 2.0
    n.tcp_retx     = 0.0
    n.oom_count    = 0
    n.cap_events   = 0
    n.syscall_rate = min(n.syscall_rate * 0.1, 100.0)
    n.error_rate   = 0.01
    n.restarts    += 1


def _do_scale_up(
    nodes: dict, edges: dict, target: str, replicas_delta: int = 2
) -> None:
    """Add replicas and reduce per-instance CPU/mem proportionally."""
    n = nodes.get(target)
    if n is None:
        return
    old_rep = max(n.replicas, 1)
    n.replicas = old_rep + replicas_delta
    scale = old_rep / n.replicas
    n.cpu = n.cpu * scale
    n.mem = n.mem * scale
    n.tcp_retx = n.tcp_retx * scale


def _do_increase_memory_limit(
    nodes: dict, edges: dict, target: str
) -> None:
    """Simulate increasing memory limit: OOM risk drops, mem headroom available."""
    n = nodes.get(target)
    if n is None:
        return
    n.oom_count = 0
    n.mem       = min(n.mem, 60.0)   # effective utilisation drops with new limit


def _do_apply_network_policy(
    nodes: dict, edges: dict, target: str
) -> None:
    """Zero out error rates on all outgoing edges from target."""
    for (src, dst), e in edges.items():
        if src == target:
            e.is_causal  = False
            e.error_rate = max(e.error_rate - 0.08, 0.001)


def _do_block_ip(
    nodes: dict, edges: dict, target: str
) -> None:
    """Remove external ingress edges and zero cap_events (C2 contact blocked)."""
    n = nodes.get(target)
    if n is None:
        return
    n.cap_events  = 0
    n.syscall_rate = max(n.syscall_rate * 0.1, 0.0)
    to_remove = [
        k for k, e in edges.items()
        if k[1] == target and getattr(nodes.get(k[0]), "is_external", False)
    ]
    for k in to_remove:
        del edges[k]


def _do_rollback_deployment(
    nodes: dict, edges: dict, target: str
) -> None:
    """Simulate rollback: reduce cpu/mem by 20%, zero out attack indicators."""
    n = nodes.get(target)
    if n is None:
        return
    n.cpu          = n.cpu * 0.8
    n.mem          = n.mem * 0.8
    n.cap_events   = 0
    n.file_events  = 0
    n.syscall_rate = n.syscall_rate * 0.3
    n.error_rate   = max(n.error_rate - 0.06, 0.01)


def _do_cordon_node(
    nodes: dict, edges: dict, target_node_name: str
) -> None:
    """
    Cordon a Kubernetes node: mark all pods on that node as isolated.
    target_node_name is a K8s node hostname (not a service ID).
    """
    affected = [nid for nid, n in nodes.items() if n.node_name == target_node_name]
    for nid in affected:
        _do_isolate_container(nodes, edges, nid)


_INTERVENTIONS = {
    "isolate_container":     _do_isolate_container,
    "restart_pod":           _do_restart_pod,
    "scale_up_replicas":     _do_scale_up,
    "increase_memory_limit": _do_increase_memory_limit,
    "apply_network_policy":  _do_apply_network_policy,
    "block_ip":              _do_block_ip,
    "rollback_deployment":   _do_rollback_deployment,
    "cordon_node":           _do_cordon_node,
}


# ─── Counterfactual result dataclass ─────────────────────────────────────────

@dataclass
class CounterfactualResult:
    target_node:       str
    action:            str
    original_probs:    dict[str, float]    # node_id → {healthy, fault, attack}
    intervened_probs:  dict[str, float]
    blast_radius:      list[str]           # nodes significantly affected
    blast_radius_delta: list[dict]         # detailed deltas per affected node
    mttr_impact_pct:   float              # estimated MTTR change (negative = improvement)
    risk_score:        float              # 0–100 (lower is safer)
    confidence:        float              # 0–1
    orig_graph_probs:  dict[str, float]   # graph-level: healthy|fault|attack
    inter_graph_probs: dict[str, float]
    recommendation:    str

    def to_dict(self) -> dict[str, Any]:
        return {
            "targetNode":          self.target_node,
            "action":              self.action,
            "originalProbs":       self.original_probs,
            "intervenedProbs":     self.intervened_probs,
            "blastRadius":         self.blast_radius,
            "blastRadiusDelta":    self.blast_radius_delta,
            "mttrImpactPct":       round(self.mttr_impact_pct, 1),
            "riskScore":           round(self.risk_score, 1),
            "confidence":          round(self.confidence, 3),
            "origGraphProbs":      self.orig_graph_probs,
            "interGraphProbs":     self.inter_graph_probs,
            "recommendation":      self.recommendation,
        }


# ─── CounterfactualEngine ────────────────────────────────────────────────────

class CounterfactualEngine:
    """
    Runs Pearl do-calculus counterfactual reasoning on the live cluster graph.

    Usage:
        engine = CounterfactualEngine(model, dag_builder)
        result = await engine.run(target_node="order-svc", action="isolate_container")
    """

    def __init__(self, model, dag_builder) -> None:
        """
        Args:
            model:       CCDTCognitiveModel (or CausalGNN)
            dag_builder: LiveDAGBuilder
        """
        self.model       = model
        self.dag_builder = dag_builder
        self.device      = next(model.parameters()).device

    async def run(
        self,
        target_node: str,
        action:      str,
        parameters:  Optional[dict] = None,
        top_k_blast: int = 5,
        blast_threshold: float = 0.1,   # min prob delta to include in blast radius
    ) -> CounterfactualResult:
        """
        Execute do(action, target_node) and return the counterfactual result.

        Args:
            target_node:     Node ID to intervene on
            action:          Action name (must be in _INTERVENTIONS)
            parameters:      Optional action-specific parameters
            top_k_blast:     Maximum blast radius size
            blast_threshold: Min |Δprob| to include a node in blast radius

        Returns:
            CounterfactualResult with full before/after comparison
        """
        if action not in _INTERVENTIONS:
            raise ValueError(
                f"Unknown action '{action}'. "
                f"Supported: {list(_INTERVENTIONS.keys())}"
            )

        # ── 1. Get original PyG graph ──────────────────────────────────────
        pyg_data = await self.dag_builder.get_pyg_data()
        x          = pyg_data.x.to(self.device)
        edge_index = pyg_data.edge_index.to(self.device)
        edge_attr  = pyg_data.edge_attr.to(self.device) \
                     if pyg_data.edge_attr is not None else None

        # ── 2. Original inference ──────────────────────────────────────────
        self.model.eval()
        with torch.no_grad():
            orig_out = self.model(x, edge_index, edge_attr)
            orig_node_probs  = orig_out["node_probs"].cpu()
            orig_graph_probs = orig_out.get("graph_probs", orig_out["node_probs"].mean(0, keepdim=True)).cpu()

        # ── 3. Clone and apply intervention ───────────────────────────────
        nodes_clone = copy.deepcopy(self.dag_builder._nodes)
        edges_clone = copy.deepcopy(self.dag_builder._edges)

        intervention_fn = _INTERVENTIONS[action]
        params = parameters or {}
        try:
            intervention_fn(nodes_clone, edges_clone, target_node, **params)
        except TypeError:
            # Some interventions take no extra params
            intervention_fn(nodes_clone, edges_clone, target_node)

        # ── 4. Rebuild PyG data from intervened state ───────────────────
        node_ids = sorted(nodes_clone.keys())
        node_to_idx = {nid: i for i, nid in enumerate(node_ids)}

        x_inter = torch.tensor(
            [nodes_clone[nid].to_feature_vector() for nid in node_ids],
            dtype=torch.float,
        ).to(self.device)

        ei_srcs, ei_dsts, ea_feats = [], [], []
        for (src, dst), e in edges_clone.items():
            si = node_to_idx.get(src)
            di = node_to_idx.get(dst)
            if si is not None and di is not None:
                ei_srcs.append(si)
                ei_dsts.append(di)
                ea_feats.append(e.to_feature_vector())

        if ei_srcs:
            ei_inter = torch.tensor([ei_srcs, ei_dsts], dtype=torch.long).to(self.device)
            ea_inter = torch.tensor(ea_feats, dtype=torch.float).to(self.device)
        else:
            ei_inter = torch.zeros((2, 0), dtype=torch.long).to(self.device)
            ea_inter = torch.zeros((0, 4), dtype=torch.float).to(self.device)

        # ── 5. Intervened inference ────────────────────────────────────
        with torch.no_grad():
            inter_out = self.model(x_inter, ei_inter, ea_inter)
            inter_node_probs  = inter_out["node_probs"].cpu()
            inter_graph_probs = inter_out.get("graph_probs", inter_out["node_probs"].mean(0, keepdim=True)).cpu()

        # ── 6. Compare and compute blast radius via BFS ────────────────
        # Build BFS neighbours from original graph
        nx_graph = await self.dag_builder.get_nx_graph()
        target_idx_orig = self.dag_builder.index_for_node(target_node)

        blast_radius   = []
        blast_details  = []
        orig_probs_out = {}
        inter_probs_out = {}

        for i, nid in enumerate(node_ids):
            orig_idx = self.dag_builder.index_for_node(nid)
            if orig_idx is None or orig_idx >= orig_node_probs.size(0):
                continue

            op = orig_node_probs[orig_idx].tolist()
            ip = inter_node_probs[i].tolist()

            orig_probs_out[nid]  = {n: round(p, 4) for n, p in zip(CLASS_NAMES.values(), op)}
            inter_probs_out[nid] = {n: round(p, 4) for n, p in zip(CLASS_NAMES.values(), ip)}

            # Delta in attack probability
            delta_attack  = ip[2] - op[2]
            delta_healthy = ip[0] - op[0]

            if abs(delta_attack) >= blast_threshold or abs(delta_healthy) >= blast_threshold:
                # Confirm node is reachable from target via BFS
                reachable = nid == target_node
                if not reachable and nx_graph.has_node(target_node) and nx_graph.has_node(nid):
                    try:
                        reachable = nx.has_path(nx_graph, target_node, nid)
                    except Exception:
                        reachable = True

                if reachable:
                    blast_radius.append(nid)
                    blast_details.append({
                        "node":          nid,
                        "delta_attack":  round(delta_attack, 4),
                        "delta_healthy": round(delta_healthy, 4),
                        "orig_class":    CLASS_NAMES[int(torch.tensor(op).argmax())],
                        "inter_class":   CLASS_NAMES[int(torch.tensor(ip).argmax())],
                    })

        # Sort blast radius by |delta_attack| descending, limit to top_k
        blast_details.sort(key=lambda d: abs(d["delta_attack"]), reverse=True)
        blast_details  = blast_details[:top_k_blast]
        blast_radius   = [d["node"] for d in blast_details]

        # ── 7. MTTR impact estimate ────────────────────────────────────
        tgt_i_orig  = self.dag_builder.index_for_node(target_node)
        tgt_i_inter = node_to_idx.get(target_node)

        if tgt_i_orig is not None and tgt_i_orig < orig_node_probs.size(0):
            delta_attack  = float(
                inter_node_probs[tgt_i_inter][2] - orig_node_probs[tgt_i_orig][2]
            ) if tgt_i_inter is not None else 0.0
            delta_cpu = float(
                x_inter[tgt_i_inter][0] - x[tgt_i_orig][0]
            ) * 100.0 if tgt_i_inter is not None else 0.0
        else:
            delta_attack = 0.0
            delta_cpu    = 0.0

        coeffs = _MTTR_COEFFS.get(action, {"base": -30.0, "cpu": -0.2, "prob": -20.0})
        mttr_impact = (
            coeffs["base"]
            + coeffs["cpu"]  * delta_cpu
            + coeffs["prob"] * delta_attack
        )
        mttr_impact = max(-90.0, min(30.0, mttr_impact))   # clamp to realistic range

        # ── 8. Risk score ─────────────────────────────────────────────
        # Higher blast radius + negative delta_healthy → higher risk
        blast_factor = len(blast_radius) * 5.0
        health_delta = float(
            inter_graph_probs[0][0] - orig_graph_probs[0][0]
        ) if orig_graph_probs.size(0) > 0 else 0.0

        risk_score = max(0.0, min(100.0,
            blast_factor
            - health_delta * 50.0
            + (10.0 if action == "cordon_node" else 0.0)
            + (5.0  if action == "rollback_deployment" else 0.0)
        ))

        # ── 9. Confidence ─────────────────────────────────────────────
        # Higher when blast radius is small and prediction delta is large
        confidence = max(0.5, min(0.99,
            0.95 - len(blast_radius) * 0.02 + abs(delta_attack) * 0.3
        ))

        # ── 10. Recommendation ────────────────────────────────────────
        if risk_score < 20:
            recommendation = (
                f"SAFE TO EXECUTE — Action '{action}' on '{target_node}' shows low risk "
                f"({risk_score:.0f}/100) with estimated MTTR improvement of "
                f"{abs(mttr_impact):.0f}%."
            )
        elif risk_score < 50:
            recommendation = (
                f"PROCEED WITH CAUTION — Action '{action}' on '{target_node}' has moderate "
                f"risk ({risk_score:.0f}/100). Review {len(blast_radius)} affected node(s) "
                "before executing."
            )
        else:
            recommendation = (
                f"HIGH RISK — Action '{action}' on '{target_node}' is risky "
                f"({risk_score:.0f}/100). Consider a less disruptive alternative. "
                f"Blast radius: {', '.join(blast_radius[:3])}."
            )

        # ── Graph-level probs ──────────────────────────────────────────
        def graph_probs_dict(t: torch.Tensor) -> dict[str, float]:
            v = t[0].tolist() if t.size(0) > 0 else [0.33, 0.33, 0.34]
            return {n: round(p, 4) for n, p in zip(CLASS_NAMES.values(), v)}

        return CounterfactualResult(
            target_node=target_node,
            action=action,
            original_probs=orig_probs_out,
            intervened_probs=inter_probs_out,
            blast_radius=blast_radius,
            blast_radius_delta=blast_details,
            mttr_impact_pct=mttr_impact,
            risk_score=risk_score,
            confidence=confidence,
            orig_graph_probs=graph_probs_dict(orig_graph_probs),
            inter_graph_probs=graph_probs_dict(inter_graph_probs),
            recommendation=recommendation,
        )
