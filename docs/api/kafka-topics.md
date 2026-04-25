# CCDT Kafka Topics

## Topic Overview

| Topic | Partitions | Retention | Key | Producer | Consumers |
|---|---|---|---|---|---|
| `ccdt.ebpf.events` | 3 | 24h | `node_name` | Layer-1 collector | Layer-2 GNN |
| `ccdt.gnn.inference` | 3 | 7d | `inference_id` | Layer-2 GNN | Layer-3, Layer-4, Dashboard |
| `ccdt.guardian.actions` | 3 | 30d | `audit_id` | Layer-3 Guardian | Layer-4, Dashboard, Audit DB |
| `ccdt.incidents` | 1 | 90d | `incident_id` | Layer-2 + Layer-3 | Dashboard, PagerDuty webhook |

---

## ccdt.ebpf.events

**Purpose**: Raw eBPF event batches from every Kubernetes worker node.

**Schema**: `TypedEbpfBatch` (see `shared/proto/events.proto`)

**Key**: `node_name` — ensures all events from the same node go to the same partition (important for per-node causal ordering).

**Producer config**:
```yaml
acks: all
compression.type: lz4
max.message.bytes: 65536   # 64 KB
linger.ms: 100             # batch up to 100ms
batch.size: 32768          # 32 KB batch
retries: 5
retry.backoff.ms: 500
```

**Consumer group**: `ccdt-layer2-cognitive`

**Message rate**: 2–20 messages/sec/node (2 in steady state, 20 during incident)

**Sample message** (abridged JSON representation):
```json
{
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "node_name": "ip-10-0-1-42.us-east-1.compute.internal",
  "collector_id": "a3f7c2d1-...",
  "batch_ts": "2024-12-20T14:23:45.123Z",
  "schema_ver": "1.0",
  "type_counts": {"oom_kill": 3, "capability": 7, "tcp_retransmit": 2},
  "oom_kill_events": [
    {
      "meta": {"node_name": "ip-10-0-1-42", "pid": 5555, "comm": "java", "severity": "CRITICAL"},
      "victim_pid": 5555, "victim_comm": "java",
      "oom_score": 950, "victim_rss_bytes": 536870912
    }
  ]
}
```

---

## ccdt.gnn.inference

**Purpose**: GNN classification results — one per batch received, plus 5-second heartbeats.

**Schema**: `GnnInferenceResult` (see `shared/proto/graph.proto`)

**Key**: `inference_id` — random partition assignment.

**Message rate**: ~1/5s heartbeat + triggered on incident detection (up to 5/sec during active incidents).

**Heartbeat example** (healthy cluster):
```json
{
  "inference_id": "...",
  "incident_type": "NONE",
  "graph_confidence": 0.02,
  "is_heartbeat": true,
  "node_count": 47,
  "edge_count": 83,
  "inference_latency_ms": 38.5
}
```

**Incident example** (FAULT detected):
```json
{
  "inference_id": "...",
  "incident_type": "FAULT",
  "graph_confidence": 0.88,
  "root_cause_node_name": "payment-svc",
  "root_cause_confidence": 0.91,
  "blast_radius_count": 3,
  "severity": "high",
  "causal_chain": [{"node_name": "payment-svc", "causal_score": 0.91, "depth": 0}],
  "top_features": [{"feature_name": "oom_kill_rate", "importance": 0.82, "value": 0.85}]
}
```

---

## ccdt.guardian.actions

**Purpose**: Full audit trail of every remediation action (proposed, approved, executed, outcome).

**Schema**: `ActionResult` wrapping `ActionRequest` (see `shared/proto/actions.proto`)

**Retention**: 30 days (compliance requirement for change management).

**Sample message**:
```json
{
  "audit_id": "...",
  "status": "SUCCEEDED",
  "request": {
    "action_name": "ACTION_RESTART_POD",
    "target_node_name": "payment-svc-pod-abc123",
    "target_namespace": "production",
    "autonomy_mode": "AUTONOMY_SUPERVISED",
    "ghost_result": {
      "risk_score": 0.12, "risk_category": "RISK_VERY_LOW",
      "opa_approved": true, "affected_pod_count": 1
    }
  },
  "execution_duration_ms": 1250,
  "verified_effect": true,
  "post_action_health": 0.94,
  "approved_by": "operator:alice@company.com"
}
```

---

## ccdt.incidents

**Purpose**: Unified incident lifecycle events (detection → remediation → resolution).

**Schema**: Defined in `shared/schemas/incident.schema.json`

**Sample detection event**:
```json
{
  "incident_id": "...",
  "detected_at": "2024-12-20T14:23:45Z",
  "state": "ACTIVE",
  "severity": "HIGH",
  "incident_type": "FAULT",
  "root_cause_service": "payment-svc",
  "graph_confidence": 0.88,
  "blast_radius_count": 3,
  "nl_summary": "OOM kills in payment-svc causing cascading latency in api-gw and order-svc."
}
```

---

## Consumer Group Management

```bash
# List consumer group offsets
kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --group ccdt-layer2-cognitive --describe

# Reset offsets to replay 24h of events (for retraining)
kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --group ccdt-layer2-cognitive-replay \
  --topic ccdt.ebpf.events \
  --reset-offsets --to-datetime 2024-12-19T00:00:00.000 \
  --execute

# Lag monitoring (alert if lag > 5000)
kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --group ccdt-layer2-cognitive --describe | awk 'NR>1{print $5}'
```
