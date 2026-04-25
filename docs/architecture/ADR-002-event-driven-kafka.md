# ADR-002: Kafka Event Bus Over gRPC Streaming

**Status**: Accepted  
**Date**: 2024-12-05  
**Authors**: CCDT Platform Engineering

---

## Context

We needed inter-layer communication between the Go collector (Layer-1) and the Python GNN (Layer-2), and between the GNN and the Guardian (Layer-3). Requirements:
- Ordered delivery within a partition (per-node ordering for eBPF events)
- Replay capability for ML training data collection
- Backpressure: Layer-2 should be able to fall behind without losing Layer-1 events
- At-least-once delivery with deduplication at the consumer

## Decision

Use **Apache Kafka 3.7** with 4 topics, 3 partitions each (except `ccdt.incidents` with 1 partition).

Topic | Key | Consumers
------|-----|----------
`ccdt.ebpf.events` | `node_name` | Layer-2 GNN
`ccdt.gnn.inference` | `inference_id` | Layer-3, Layer-4, Dashboard
`ccdt.guardian.actions` | `audit_id` | Layer-4, Dashboard, Audit DB
`ccdt.incidents` | `incident_id` | Dashboard, PagerDuty webhook

## Consequences

**Positive**
- Replay: training dataset built by replaying `ccdt.ebpf.events` at any time
- Backpressure: Layer-1 is never blocked by a slow Layer-2
- Fan-out: Layer-4 and Dashboard can both consume `ccdt.gnn.inference` independently
- Durability: 24h–90d retention configurable per topic

**Negative**
- Adds ~50ms median latency per hop (vs ~5ms for direct gRPC)
- Kafka cluster requires 3 brokers for HA (significant resource cost)
- Ordering is per-partition only: cross-node events may arrive out-of-order

## Alternatives Considered

**gRPC streaming**: Lower latency, strongly typed, but tight coupling and no replay.

**NATS JetStream**: Simpler operations, but less mature ecosystem for Python consumers.

**Redis Streams**: Operationally simpler, but no replication guarantees in Redis Community Edition.
