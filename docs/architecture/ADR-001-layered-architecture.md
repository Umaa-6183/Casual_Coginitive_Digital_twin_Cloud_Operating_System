# ADR-001: Four-Layer Biological Architecture

**Status**: Accepted  
**Date**: 2024-12-01  
**Authors**: CCDT Platform Engineering  
**Supersedes**: —

---

## Context

We needed an architectural pattern for a system that simultaneously:
1. Observes a Kubernetes cluster at kernel-level granularity
2. Reasons about complex multi-service causal relationships
3. Takes autonomous remediation actions safely
4. Explains its decisions to human operators

We evaluated: monolithic anomaly detector, rules-based engine, ML-only pipeline, and the layered biological model.

## Decision

Adopt a **four-layer biologically-inspired architecture** where each layer has a strictly defined responsibility and communicates only with its adjacent layers via the Kafka event bus.

| Layer | Biological Analogy | Responsibility |
|---|---|---|
| 1 – Nervous System | Peripheral nerves | Sense kernel-level signals |
| 2 – Cognitive Core | Cerebral cortex | Pattern recognition + causal reasoning |
| 3 – Guardian | Motor cortex | Safe action execution |
| 4 – Co-Pilot | Language / prefrontal | Human communication |

## Consequences

**Positive**
- Clear separation of concerns: each layer can be tested, upgraded, or replaced independently
- Failure isolation: Layer-3 crashing does not affect Layer-1 observation
- Independent scaling: Layer-2 can have 2 replicas while Layer-1 runs as a DaemonSet
- Auditability: every layer publishes to Kafka, creating a complete event log

**Negative**
- Higher operational complexity than a single service
- Kafka adds end-to-end latency (~50ms per hop)
- More services to monitor, deploy, and debug

## Alternatives Considered

**Monolithic Python service**: Simpler ops, but single failure domain. Layer-1 requires Go for eBPF.

**Direct gRPC between layers**: Lower latency but tight coupling. A Guardian restart would break the observation pipeline.

**Event streaming without Kafka (Redis Streams)**: Simpler, but lacks Kafka's replay semantics needed for training data collection.
