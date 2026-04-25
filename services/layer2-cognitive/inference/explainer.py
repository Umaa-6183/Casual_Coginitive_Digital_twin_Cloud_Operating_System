"""
CCDT Layer-2 Cognitive Core — GNN Explainer
─────────────────────────────────────────────
Two complementary explainability methods:

1. AttentionRollout
   Rolls up attention weights across all 4 GATv2 layers using matrix
   multiplication.  Produces a per-node influence score: how much did
   each node influence the final root-cause prediction?

2. GradientFeatureImportance
   Computes input-gradient × feature (Integrated Gradients style) to
   identify which of the 17 node features drove the model's decision.
   Uses torch.autograd.grad for efficiency.

Both methods return JSON-serialisable dicts for the inference server.
"""
from __future__ import annotations

import logging
from typing import Optional

import torch
import torch.nn.functional as F
from torch import Tensor
from torch_geometric.data import Data

logger = logging.getLogger("ccdt.cognitive.explainer")

# Feature names for human-readable output (must match dataset.py)
FEATURE_NAMES = [
    "cpu_util",       "mem_util",       "sched_lat_p99",  "tcp_retx_rate",
    "oom_count",      "cap_events",     "syscall_rate",   "file_events",
    "error_rate",     "request_rate",   "latency_ms",     "restarts",
    "replica_count",  "is_critical",    "is_external",    "layer_bit0",
    "layer_bit1",
]

CLASS_NAMES = ["healthy", "fault", "attack"]


# ─── Attention Rollout ────────────────────────────────────────────────────────

class AttentionRollout:
    """
    Computes multi-layer attention rollout for GATv2.

    For each layer l, builds a dense (N × N) attention matrix from the
    sparse edge-wise attention coefficients, then multiplies them together
    across layers to propagate attention from inputs to output.

    Ref: Abnar & Zuidema 2020, "Quantifying Attention Flow in Transformers"
    """

    def __init__(self, num_heads: int = 8, discard_ratio: float = 0.9) -> None:
        self.num_heads = num_heads
        self.discard_ratio = discard_ratio

    def rollout(
        self,
        attn_weights: list[Tensor],   # per-layer: (E, heads) or (E,)
        edge_index:   Tensor,         # (2, E)
        num_nodes:    int,
    ) -> Tensor:
        """
        Compute attention rollout.

        Returns:
            influence_scores (N,) — how much attention flows to each node
            from the global input
        """
        # Build dense attention matrix per layer, average over heads
        rollout_mat = torch.eye(num_nodes, device=edge_index.device)

        for alpha in attn_weights:
            # alpha: (E, heads) or (E,)
            if alpha.dim() == 2:
                alpha_mean = alpha.mean(dim=-1)   # (E,)
            else:
                alpha_mean = alpha

            # Build dense N×N attention matrix
            A = torch.zeros(num_nodes, num_nodes, device=edge_index.device)
            src, dst = edge_index[0], edge_index[1]
            A.scatter_add_(0, dst.unsqueeze(1).expand(-1, 1),
                           alpha_mean.unsqueeze(1))

            # Row-normalise
            row_sum = A.sum(dim=1, keepdim=True).clamp(min=1e-8)
            A = A / row_sum

            # Discard bottom % of attention (noise floor)
            if self.discard_ratio > 0:
                flat = A.view(-1)
                threshold = flat.quantile(self.discard_ratio)
                A = torch.where(A >= threshold, A, torch.zeros_like(A))

            # Add residual + re-normalise (standard rollout)
            A = 0.5 * A + 0.5 * torch.eye(num_nodes, device=A.device)
            row_sum = A.sum(dim=1, keepdim=True).clamp(min=1e-8)
            A = A / row_sum

            # Accumulate via matrix product
            rollout_mat = torch.matmul(A, rollout_mat)

        # Sum each row → influence received from all inputs
        influence = rollout_mat.sum(dim=1)
        influence = influence / influence.sum().clamp(min=1e-8)
        return influence

    def explain(
        self,
        model_out:   dict[str, Tensor],
        edge_index:  Tensor,
        num_nodes:   int,
        node_ids:    list[str],
    ) -> dict:
        """
        Return attention rollout explanation as a JSON-serialisable dict.

        Returns:
            {
              "node_influence": {node_id: score, ...},
              "top_nodes": [{node_id, influence_score}, ...]  # top 5
            }
        """
        attn_weights = model_out.get("attn_weights", [])
        if not attn_weights:
            logger.warning(
                "No attention weights in model output — returning uniform scores")
            scores = {nid: round(1.0 / max(num_nodes, 1), 4)
                      for nid in node_ids}
            return {"node_influence": scores, "top_nodes": []}

        influence = self.rollout(attn_weights, edge_index, num_nodes)
        influence_np = influence.cpu().detach().numpy()

        node_influence = {
            nid: round(float(influence_np[i]), 4)
            for i, nid in enumerate(node_ids)
            if i < len(influence_np)
        }

        # Top-5 most influential nodes
        sorted_nodes = sorted(node_influence.items(), key=lambda x: -x[1])
        top_nodes = [
            {"node_id": nid, "influence_score": score}
            for nid, score in sorted_nodes[:5]
        ]

        return {
            "node_influence": node_influence,
            "top_nodes":      top_nodes,
        }


# ─── Gradient Feature Importance ─────────────────────────────────────────────

class GradientFeatureImportance:
    """
    Gradient × input feature importance for the attack class.

    For each node, computes: importance_i = |∂L/∂x_i| × x_i
    where L is the log-probability of the predicted class.

    Returns the feature names ranked by mean absolute importance across
    all nodes, plus per-node top-3 important features.
    """

    def __init__(self, target_class: int = 2) -> None:
        """
        Args:
            target_class: Class to explain (default 2 = attack).
        """
        self.target_class = target_class

    def explain(
        self,
        model,
        x:          Tensor,   # (N, F) — must have requires_grad=True
        edge_index: Tensor,
        edge_attr:  Optional[Tensor],
        node_ids:   list[str],
    ) -> dict:
        """
        Compute gradient-based feature importance.

        Returns:
            {
              "global_feature_importance": [{feature, importance}, ...],
              "node_feature_importance":   {node_id: [{feature, importance}], ...}
            }
        """
        model.eval()

        # Enable gradient computation on input
        x_req = x.detach().requires_grad_(True)

        out = model(x_req, edge_index, edge_attr)
        node_logits = out["node_logits"]

        # Compute gradients w.r.t. target class log-probability
        log_probs = F.log_softmax(node_logits, dim=-1)
        target_logp = log_probs[:, self.target_class].sum()

        try:
            grads = torch.autograd.grad(
                target_logp, x_req,
                create_graph=False,
                retain_graph=False,
            )[0]   # (N, F)
        except RuntimeError as exc:
            logger.warning(
                "Gradient computation failed: %s — returning zeros", exc)
            grads = torch.zeros_like(x)

        # Gradient × input
        importance = (grads.abs() * x_req.abs()).detach().cpu()   # (N, F)

        # Global: mean importance per feature across all nodes
        global_imp = importance.mean(dim=0).numpy()
        global_ranking = sorted(
            [
                {"feature": FEATURE_NAMES[i], "importance": round(
                    float(global_imp[i]), 5)}
                for i in range(len(FEATURE_NAMES))
                if i < len(global_imp)
            ],
            key=lambda d: -d["importance"],
        )

        # Per-node: top-3 features per node
        node_feat_imp: dict[str, list] = {}
        for n_idx, nid in enumerate(node_ids):
            if n_idx >= importance.size(0):
                continue
            node_imp = importance[n_idx].numpy()
            top3 = sorted(
                [
                    {"feature": FEATURE_NAMES[i], "importance": round(
                        float(node_imp[i]), 5)}
                    for i in range(len(FEATURE_NAMES))
                    if i < len(node_imp)
                ],
                key=lambda d: -d["importance"],
            )[:3]
            node_feat_imp[nid] = top3

        return {
            "target_class":              CLASS_NAMES[self.target_class],
            "global_feature_importance": global_ranking,
            "node_feature_importance":   node_feat_imp,
        }


# ─── Combined Explainer ───────────────────────────────────────────────────────

class CCDTExplainer:
    """
    Combines AttentionRollout + GradientFeatureImportance into a single
    explanation object for a given inference result.
    """

    def __init__(self, model, num_heads: int = 8) -> None:
        self.model = model
        self.attn_exp = AttentionRollout(num_heads=num_heads)
        self.grad_exp = GradientFeatureImportance(
            target_class=2)   # explain attack class

    def explain(
        self,
        x:          Tensor,
        edge_index: Tensor,
        edge_attr:  Optional[Tensor],
        node_ids:   list[str],
    ) -> dict:
        """
        Produce a complete explanation for a single graph inference.

        Returns JSON-serialisable dict with:
          - attention_rollout: node influence scores
          - feature_importance: global + per-node feature rankings
          - causal_chain:       top nodes ranked by combined score
        """
        self.model.eval()

        with torch.no_grad():
            out = self.model(x, edge_index, edge_attr)

        num_nodes = x.size(0)

        # ── Attention rollout ────────────────────────────────────────────
        attn_exp = self.attn_exp.explain(out, edge_index, num_nodes, node_ids)

        # ── Gradient feature importance ───────────────────────────────────
        grad_exp = self.grad_exp.explain(
            self.model, x, edge_index, edge_attr, node_ids)

        # ── Causal chain: combine rollout influence + attack probability ──
        node_probs = out.get("node_probs", F.softmax(
            out["node_logits"], dim=-1)).cpu()

        causal_chain = []
        for i, nid in enumerate(node_ids):
            if i >= node_probs.size(0):
                continue
            probs = node_probs[i]
            attack_p = float(probs[2])
            influence = attn_exp["node_influence"].get(nid, 0.0)
            # Combined causal score: geometric mean of attack prob + influence
            causal_score = (attack_p * influence) ** 0.5

            pred_class = CLASS_NAMES[int(probs.argmax())]
            causal_chain.append({
                "node":          nid,
                "causal_score":  round(causal_score, 4),
                "attack_prob":   round(attack_p, 4),
                "influence":     round(influence, 4),
                "predicted_class": pred_class,
            })

        causal_chain.sort(key=lambda d: -d["causal_score"])

        return {
            "attention_rollout":  attn_exp,
            "feature_importance": grad_exp,
            "causal_chain":       causal_chain,
        }
