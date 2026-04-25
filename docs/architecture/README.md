# CCDT Architecture Documentation

**Cognitive Digital Twin (CCDT)** is a Level-4 Autonomous AIOps Security Platform modelled on the biological nervous system. This directory contains all architecture decision records (ADRs), system diagrams, data flow specifications, and the security model.

---

## Document Index

| Document | Description |
|---|---|
| [system-overview.md](system-overview.md) | High-level four-layer biological architecture |
| [data-flow.md](data-flow.md) | Event flow from eBPF sensor to operator response |
| [security-model.md](security-model.md) | Threat model, trust boundaries, zero-trust design |
| [ADR-001-layered-architecture.md](ADR-001-layered-architecture.md) | Why we chose a layered biological model |
| [ADR-002-event-driven-kafka.md](ADR-002-event-driven-kafka.md) | Why Kafka over gRPC streaming or REST polling |
| [ADR-003-causal-gnn.md](ADR-003-causal-gnn.md) | Why Graph Attention Networks + do-calculus causal inference |
| [ADR-004-rl-guardian.md](ADR-004-rl-guardian.md) | Why Reinforcement Learning over rule-based remediation |
| [ADR-005-llm-copilot.md](ADR-005-llm-copilot.md) | Why Claude claude-sonnet-4 as the operator co-pilot |

---

## Four-Layer Biological Model

```
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 4: Co-Pilot Interface  (Claude API — "Language Centre")       │
│  FastAPI + SSE streaming + tool use + session management             │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 3: Guardian Layer       (RL + OPA — "Motor Cortex")          │
│  Stable-Baselines3 PPO + 5 Rego policies + Kubernetes executor       │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 2: Cognitive Core       (Causal GNN — "Cerebral Cortex")     │
│  PyTorch Geometric + GAT + do-calculus + counterfactual engine       │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 1: Nervous System       (eBPF sensors — "Peripheral Nerves") │
│  Go + libbpf/Cilium + 8 tracepoint categories + Kafka publisher      │
└─────────────────────────────────────────────────────────────────────┘
          ↕ Kafka Event Bus (4 topics, 3 partitions each)
```

---

## Key Design Principles

**1. Separation of sensing, reasoning, and acting**
Each layer has a single responsibility. Layer-1 only observes. Layer-2 only reasons. Layer-3 only acts. Layer-4 only communicates. This prevents any single failure from corrupting the entire pipeline.

**2. Fail-safe defaults**
Every layer degrades gracefully: if Layer-3 cannot reach Layer-2, it falls back to the last known good inference. If OPA is unreachable, a conservative local fallback evaluator blocks all high-risk actions.

**3. Ghost Preview before every action**
No Kubernetes API call is made without a Ghost Preview simulation. Ghost Preview runs the action in a dry-run sandbox and computes risk score, blast radius, MTTR delta, and OPA approval before touching the cluster.

**4. Human-in-the-loop for MEDIUM+ risk**
The autonomy mode (`supervised` / `human-in-loop` / `full-auto`) controls whether humans must approve actions. Default is `supervised`: auto-execute low risk, page human for MEDIUM+.

**5. Audit trail for every decision**
Every GNN inference, every Guardian action, and every Co-Pilot conversation is persisted with full provenance. This enables post-incident analysis and fine-tuning of the LLM.

---

## Technology Stack

| Component | Technology | Version |
|---|---|---|
| Layer-1 collector | Go + libbpf | Go 1.22 |
| eBPF programs | C (CO-RE) | kernel ≥ 5.8 |
| Event serialization | Protocol Buffers | proto3 |
| Event bus | Apache Kafka | 3.7 |
| Layer-2 GNN | PyTorch Geometric + GAT | PyG 2.5 |
| Causal inference | do-calculus (custom) | — |
| Layer-3 RL agent | Stable-Baselines3 PPO | 2.3 |
| Policy engine | OPA (Rego) | 0.63 |
| Layer-4 LLM | Anthropic Claude claude-sonnet-4 | API v1 |
| API Gateway | FastAPI + Uvicorn | FastAPI 0.111 |
| Dashboard | React 18 + TypeScript | Vite 5 |
| Container runtime | containerd | 1.7 |
| Orchestration | Kubernetes | 1.29+ |
| Service mesh | Cilium | 1.15 |
| Observability | Prometheus + Grafana | — |
| Infrastructure | Terraform + Helm | — |
| Cloud | AWS EKS | — |

---

## Repository Layout

```
ccdt/
├── services/
│   ├── layer1-nervous/      # Go eBPF collector (DaemonSet)
│   ├── layer2-cognitive/    # Python GNN inference service
│   ├── layer3-guardian/     # Python RL + OPA + executor
│   └── layer4-copilot/      # Python Claude API wrapper
├── apps/
│   ├── api-gateway/         # FastAPI REST + WebSocket gateway
│   └── dashboard/           # React operator UI
├── shared/
│   ├── proto/               # .proto definitions + generated shims
│   ├── schemas/             # JSON Schema validators
│   └── utils/               # Logging + Prometheus metrics
├── infra/
│   ├── kubernetes/          # Raw K8s manifests
│   ├── helm/                # Helm chart
│   └── terraform/           # AWS EKS + VPC + RDS
├── tests/
│   ├── unit/                # Isolated unit tests (no I/O)
│   ├── integration/         # Cross-layer integration tests
│   ├── e2e/                 # Full pipeline end-to-end tests
│   └── chaos/               # Fault injection / resilience tests
└── docs/
    ├── architecture/        # ← you are here
    ├── api/                 # API reference (OpenAPI + gRPC + Kafka)
    └── runbooks/            # Operational runbooks
```
