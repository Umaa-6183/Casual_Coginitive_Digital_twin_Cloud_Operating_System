"""
CCDT Layer-2 Cognitive Core — Causal Graph Neural Network
═══════════════════════════════════════════════════════════════════════════════
Architecture:
  • 4 × GATv2Conv layers (8 attention heads each, hidden_dim=128)
  • Edge-feature projection for latency / error-rate / request-rate
  • CausalLoss = CrossEntropy(node) + CrossEntropy(graph) + DAG regularisation
  • Soft adjacency matrix A is learned alongside GNN weights
  • DAG regularisation: h(A) = trace(exp(A ⊙ A)) − n  (Zheng et al. NOTEARS)

Incident classification:
  Node level:   healthy | fault | attack
  Graph level:  healthy | fault | attack

Inputs (per graph):
  x         [N, node_feat_dim]   node feature matrix
  edge_index [2, E]              directed edges (COO format)
  edge_attr  [E, edge_feat_dim]  edge features (latency, error_rate, req_rate)
  batch      [N]                 batch assignment vector
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from torch_geometric.nn import GATv2Conv, global_mean_pool, global_max_pool
from torch_geometric.utils import to_dense_adj, add_self_loops


# ─── Constants ────────────────────────────────────────────────────────────────
NUM_NODE_CLASSES = 3   # healthy=0, fault=1, attack=2
NUM_GRAPH_CLASSES = 3   # healthy=0, fault=1, attack=2
NODE_FEAT_DIM = 17  # number of input node features
EDGE_FEAT_DIM = 4   # number of input edge features
HIDDEN_DIM = 128
NUM_HEADS = 8
NUM_LAYERS = 4
DROPOUT = 0.1


# ─── Edge Feature Encoder ─────────────────────────────────────────────────────

class EdgeEncoder(nn.Module):
    """Projects raw edge features to HIDDEN_DIM for use in GATv2 attention."""

    def __init__(self, in_dim: int = EDGE_FEAT_DIM, out_dim: int = HIDDEN_DIM) -> None:
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(in_dim, out_dim),
            nn.LayerNorm(out_dim),
            nn.ReLU(inplace=True),
            nn.Linear(out_dim, out_dim),
        )

    def forward(self, edge_attr: Tensor) -> Tensor:
        return self.mlp(edge_attr)


# ─── Causal GNN ──────────────────────────────────────────────────────────────

class CausalGNN(nn.Module):
    """
    4-layer GATv2 Causal Graph Neural Network.

    Returns:
        node_logits  [N, 3]     per-node classification logits
        graph_logits [B, 3]     per-graph classification logits
        attn_weights list[Tensor]  attention weights from each layer (for explainability)
        A_soft       [N, N]     learned soft adjacency (for DAG regularisation)
    """

    def __init__(
        self,
        node_feat_dim:  int = NODE_FEAT_DIM,
        edge_feat_dim:  int = EDGE_FEAT_DIM,
        hidden_dim:     int = HIDDEN_DIM,
        num_heads:      int = NUM_HEADS,
        num_layers:     int = NUM_LAYERS,
        num_node_cls:   int = NUM_NODE_CLASSES,
        num_graph_cls:  int = NUM_GRAPH_CLASSES,
        dropout:        float = DROPOUT,
        max_nodes:      int = 64,    # for soft adjacency matrix
    ) -> None:
        super().__init__()

        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.dropout = dropout
        self.max_nodes = max_nodes

        # ── Node feature projection ──────────────────────────────────────────
        self.node_encoder = nn.Sequential(
            nn.Linear(node_feat_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.ReLU(inplace=True),
        )

        # ── Edge feature encoder ─────────────────────────────────────────────
        self.edge_encoder = EdgeEncoder(edge_feat_dim, hidden_dim)

        # ── GATv2 convolution layers ─────────────────────────────────────────
        # Each layer: hidden_dim → hidden_dim (concat=False → output is hidden_dim)
        self.convs = nn.ModuleList()
        self.norms = nn.ModuleList()
        for _ in range(num_layers):
            self.convs.append(
                GATv2Conv(
                    in_channels=hidden_dim,
                    out_channels=hidden_dim // num_heads,  # per-head dim
                    heads=num_heads,
                    edge_dim=hidden_dim,
                    dropout=dropout,
                    concat=True,   # concat heads → hidden_dim
                    add_self_loops=True,
                )
            )
            # After concat: num_heads * (hidden_dim // num_heads) = hidden_dim
            self.norms.append(nn.LayerNorm(hidden_dim))

        # ── Node classification head ─────────────────────────────────────────
        self.node_head = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim // 2, num_node_cls),
        )

        # ── Graph classification head ─────────────────────────────────────────
        # Concatenates mean-pool + max-pool → 2 * hidden_dim
        self.graph_head = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, num_graph_cls),
        )

        # ── Learnable soft adjacency (for DAG causal structure) ───────────────
        # A_soft[i,j] = probability that node i causally affects node j
        self.A_logits = nn.Parameter(
            torch.randn(max_nodes, max_nodes) * 0.01
        )

        # ── Causal attention router ───────────────────────────────────────────
        # Modulates node embeddings by their causal importance
        self.causal_gate = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.Sigmoid(),
        )

        self._init_weights()

    def _init_weights(self) -> None:
        """Xavier initialisation for linear layers."""
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def get_soft_adjacency(self, n: int) -> Tensor:
        """
        Return the soft adjacency matrix for a graph with n nodes.
        Uses sigmoid to keep values in (0, 1).
        """
        A = torch.sigmoid(self.A_logits[:n, :n])
        # Zero diagonal (no self-causation in DAG)
        A = A * (1 - torch.eye(n, device=A.device))
        return A

    def forward(
        self,
        x:          Tensor,             # [N, node_feat_dim]
        edge_index: Tensor,             # [2, E]
        edge_attr:  Optional[Tensor],   # [E, edge_feat_dim]
        batch:      Optional[Tensor],   # [N]
    ) -> Tuple[Tensor, Tensor, list, Tensor]:

        N = x.size(0)
        device = x.device

        if batch is None:
            batch = torch.zeros(N, dtype=torch.long, device=device)

        # ── Node + edge encoding ─────────────────────────────────────────────
        h = self.node_encoder(x)           # [N, hidden_dim]

        if edge_attr is not None:
            e = self.edge_encoder(edge_attr)   # [E, hidden_dim]
        else:
            E = edge_index.size(1)
            e = torch.zeros(E, self.hidden_dim, device=device)

        # ── GATv2 message passing ────────────────────────────────────────────
        attn_weights_list = []
        for conv, norm in zip(self.convs, self.norms):
            h_new, (_, attn) = conv(h, edge_index,
                                    edge_attr=e, return_attention_weights=True)
            h_new = norm(h_new)
            h_new = F.relu(h_new)
            h_new = F.dropout(h_new, p=self.dropout, training=self.training)
            h = h + h_new   # residual connection
            attn_weights_list.append(attn)

        # ── Causal gating ─────────────────────────────────────────────────────
        gate = self.causal_gate(h)      # [N, hidden_dim]
        h = h * gate                    # element-wise modulation

        # ── Node classification ──────────────────────────────────────────────
        node_logits = self.node_head(h)     # [N, 3]

        # ── Graph classification (mean + max pooling) ─────────────────────────
        g_mean = global_mean_pool(h, batch)     # [B, hidden_dim]
        g_max = global_max_pool(h, batch)      # [B, hidden_dim]
        g = torch.cat([g_mean, g_max], dim=1)  # [B, 2*hidden_dim]
        graph_logits = self.graph_head(g)           # [B, 3]

        # ── Soft adjacency ───────────────────────────────────────────────────
        n_capped = min(N, self.max_nodes)
        A_soft = self.get_soft_adjacency(n_capped)   # [n_capped, n_capped]

        return node_logits, graph_logits, attn_weights_list, A_soft


# ─── CausalLoss ───────────────────────────────────────────────────────────────

class CausalLoss(nn.Module):
    """
    Combined loss for causal GNN training:

        L = λ_node  · CE(node_logits, node_labels)
          + λ_graph · CE(graph_logits, graph_label)
          + λ_dag   · h(A)

    DAG regularisation term (NOTEARS):
        h(A) = trace(exp(A ⊙ A)) − n

    h(A) = 0 iff A is a DAG. Penalises cycles in the learned causal structure.
    """

    def __init__(
        self,
        lambda_node:  float = 1.0,
        lambda_graph: float = 1.0,
        lambda_dag:   float = 0.01,
        node_weights: Optional[Tensor] = None,
        graph_weights: Optional[Tensor] = None,
    ) -> None:
        super().__init__()
        self.lambda_node = lambda_node
        self.lambda_graph = lambda_graph
        self.lambda_dag = lambda_dag

        self.node_ce = nn.CrossEntropyLoss(
            weight=node_weights, label_smoothing=0.05)
        self.graph_ce = nn.CrossEntropyLoss(
            weight=graph_weights, label_smoothing=0.05)

    def dag_penalty(self, A: Tensor) -> Tensor:
        """
        Compute h(A) = trace(exp(A ⊙ A)) − n.
        Uses matrix_exp which is differentiable through autograd.
        """
        n = A.size(0)
        A2 = A * A                          # element-wise square
        expm = torch.linalg.matrix_exp(A2)   # matrix exponential
        h = torch.trace(expm) - n
        return h

    def forward(
        self,
        node_logits:  Tensor,          # [N, 3]
        graph_logits: Tensor,          # [B, 3]
        A_soft:       Tensor,          # [n, n] soft adjacency
        node_labels:  Tensor,          # [N] long
        graph_labels: Tensor,          # [B] long
    ) -> Tuple[Tensor, dict]:

        loss_node = self.node_ce(node_logits, node_labels)
        loss_graph = self.graph_ce(graph_logits, graph_labels)
        loss_dag = self.dag_penalty(A_soft)

        total = (
            self.lambda_node * loss_node
            + self.lambda_graph * loss_graph
            + self.lambda_dag * loss_dag
        )

        return total, {
            "loss_total":  total.item(),
            "loss_node":   loss_node.item(),
            "loss_graph":  loss_graph.item(),
            "loss_dag":    loss_dag.item(),
            "dag_h":       loss_dag.item(),
        }


# ─── Inference helpers ────────────────────────────────────────────────────────

@torch.no_grad()
def predict(
    model:      CausalGNN,
    x:          Tensor,
    edge_index: Tensor,
    edge_attr:  Optional[Tensor] = None,
    batch:      Optional[Tensor] = None,
    device:     str = "cpu",
) -> dict:
    """
    Run a single forward pass and return structured predictions.

    Returns:
        node_probs     [N, 3]  softmax probabilities per node
        node_classes   [N]     argmax class per node
        graph_probs    [B, 3]  softmax probabilities per graph
        graph_class    [B]     argmax class per graph
        A_soft         [n, n]  learned soft adjacency
        attn_weights   list    per-layer attention weights
    """
    model.eval()
    model.to(device)

    x = x.to(device)
    edge_index = edge_index.to(device)
    if edge_attr is not None:
        edge_attr = edge_attr.to(device)
    if batch is not None:
        batch = batch.to(device)

    node_logits, graph_logits, attn_weights, A_soft = model(
        x, edge_index, edge_attr, batch
    )

    node_probs = F.softmax(node_logits, dim=-1)
    graph_probs = F.softmax(graph_logits, dim=-1)

    return {
        "node_probs":   node_probs.cpu(),
        "node_classes": node_probs.argmax(dim=-1).cpu(),
        "graph_probs":  graph_probs.cpu(),
        "graph_class":  graph_probs.argmax(dim=-1).cpu(),
        "A_soft":       A_soft.cpu(),
        "attn_weights": [a.cpu() for a in attn_weights],
    }


def find_root_cause(
    node_probs:  Tensor,     # [N, 3]
    node_ids:    list[str],  # node labels for output
    target_class: int = 2,   # 0=healthy, 1=fault, 2=attack
) -> Tuple[str, float, list[Tuple[str, float]]]:
    """
    Identify root cause node by highest probability for target_class.

    Returns:
        root_cause_id      node id with highest target-class probability
        root_cause_conf    its probability
        ranked_nodes       all nodes sorted by target-class prob descending
    """
    probs = node_probs[:, target_class]   # [N]
    ranked_idx = probs.argsort(descending=True).tolist()
    ranked = [(node_ids[i], probs[i].item()) for i in ranked_idx]

    root_cause_id = ranked[0][0]
    root_cause_conf = ranked[0][1]
    return root_cause_id, root_cause_conf, ranked


# ─── Model factory ───────────────────────────────────────────────────────────

def build_model(cfg: dict | None = None) -> CausalGNN:
    """
    Build a CausalGNN model from an optional config dict.
    Uses sensible production defaults when cfg is None.
    """
    defaults = {
        "node_feat_dim": NODE_FEAT_DIM,
        "edge_feat_dim": EDGE_FEAT_DIM,
        "hidden_dim":    HIDDEN_DIM,
        "num_heads":     NUM_HEADS,
        "num_layers":    NUM_LAYERS,
        "num_node_cls":  NUM_NODE_CLASSES,
        "num_graph_cls": NUM_GRAPH_CLASSES,
        "dropout":       DROPOUT,
        "max_nodes":     64,
    }
    if cfg:
        defaults.update(cfg)
    return CausalGNN(**defaults)


def load_checkpoint(path: str, device: str = "cpu") -> CausalGNN:
    """Load a trained CausalGNN from a checkpoint file."""
    ckpt = torch.load(path, map_location=device)
    cfg = ckpt.get("model_config", {})
    model = build_model(cfg)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    return model


def save_checkpoint(
    model:  CausalGNN,
    path:   str,
    epoch:  int,
    metrics: dict,
) -> None:
    """Save model + config + training metadata to a checkpoint file."""
    torch.save({
        "model_state_dict": model.state_dict(),
        "model_config": {
            "node_feat_dim": model.node_encoder[0].in_features,
            "hidden_dim":    model.hidden_dim,
            "num_heads":     model.num_heads,
            "num_layers":    model.num_layers,
            "dropout":       model.dropout,
            "max_nodes":     model.max_nodes,
        },
        "epoch":   epoch,
        "metrics": metrics,
    }, path)


# Backward compatibility (used by trainer)
