# ADR-003: Causal Graph Neural Networks for Incident Classification

**Status**: Accepted  
**Date**: 2024-12-10  
**Authors**: CCDT Platform Engineering

---

## Context

We needed a model to classify Kubernetes service graph states as HEALTHY / FAULT / ATTACK and identify root causes. Requirements:
- Model the graph structure of Kubernetes service dependencies
- Distinguish between correlated anomalies (all pods slow) and causal ones (one pod failing others)
- Provide explainability: "Why did you classify payment-svc as FAULT?"
- Real-time inference: < 100ms p99 on a 50-node graph

## Decision

Use a **4-layer Graph Attention Network (GAT)** with a custom **do-calculus causal inference engine** for root cause attribution.

### Model Architecture
```
Input: TopologySnapshot
  ├── Node features: 16-dim vector per pod
  │     [cpu, mem, net_rx, net_tx, disk_r, disk_w,
  │      restarts, ready_ratio, age, cap_rate,
  │      oom_rate, tcp_retransmit, sched_lat,
  │      sensitive_file, syscall_anomaly, execve_rate]
  └── Edge features: 4-dim vector per service link
        [request_rate, error_rate, latency_p99, causal_strength]

Architecture:
  GATConv(16→64, heads=4)  → ELU → Dropout(0.1)
  GATConv(256→128, heads=4) → ELU → Dropout(0.1)
  GATConv(512→64, heads=4)  → ELU
  GATConv(256→32, heads=1)  → ELU

Classification heads:
  Node classification:  Linear(32→4)   → UNKNOWN/HEALTHY/FAULT/ATTACK
  Graph classification: mean-pool → Linear(32→6) → incident type
  Root cause:           attention-weighted ranking
```

### Causal Attribution
Uses Pearl's **do-calculus** to compute `P(incident | do(node_i = healthy))` for each node. The node whose removal most reduces incident probability becomes the root cause.

## Consequences

**Positive**
- Graph structure captures service dependencies naturally
- GAT attention weights are interpretable (which neighbors influenced the classification)
- Do-calculus gives rigorous causal attribution (not just correlation)
- Counterfactual outputs enable "what if" reasoning in the Co-Pilot

**Negative**
- Requires PyTorch Geometric — complex dependency, GPU preferred for training
- Inference on CPU is ~40ms for 50-node graph (acceptable)
- Causal engine adds ~15ms overhead per inference
- Model must be retrained when service topology changes significantly

## Alternatives Considered

**LSTM on per-service time series**: No graph structure. Cannot detect cascade patterns.

**Simple anomaly detection (Isolation Forest)**: Fast but no root cause attribution.

**Vanilla GCN (no attention)**: Lower accuracy on heterogeneous graphs. GAT outperforms by ~8% F1.
