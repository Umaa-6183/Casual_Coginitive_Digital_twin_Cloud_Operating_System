# CCDT Data Flow Specification

## Overview

This document specifies the exact data contract between each CCDT layer: message schemas, Kafka topic assignments, serialization formats, throughput targets, and error handling.

---

## Layer-1 → Kafka: eBPF Event Flow

### Source
- **Producers**: One Go collector per Kubernetes worker node (DaemonSet)
- **Trigger**: eBPF ring buffer flush (every 100ms or when buffer reaches 500 events)

### Message Format
```protobuf
TypedEbpfBatch {
    batch_id:     string    // UUID4
    node_name:    string    // K8s node name (Kafka partition key)
    collector_id: string    // UUID4 (collector instance)
    batch_ts:     string    // RFC3339 wall clock timestamp
    schema_ver:   string    // "1.0"

    capability_events:     []CapabilityEvent     // CAP_* denials
    oom_kill_events:       []OomKillEvent        // OOM kills
    tcp_retransmit_events: []TcpRetransmitEvent  // TCP retransmits
    sched_latency_events:  []SchedLatencyEvent   // Scheduler latency
    file_access_events:    []FileAccessEvent     // Sensitive file access
    syscall_events:        []SyscallEvent        // Anomalous syscalls
    execve_events:         []ExecveEvent         // New process spawns
    network_events:        []NetworkConnectEvent // Network connections

    type_counts: map<string, int32>  // Event counts per type
}
```

### Kafka Configuration
| Property | Value |
|---|---|
| Topic | `ccdt.ebpf.events` |
| Partitions | 3 |
| Replication factor | 3 |
| Partition key | `node_name` (ensures per-node ordering) |
| Retention | 24 hours |
| Max message size | 64 KB |
| Compression | lz4 |
| Producer acks | `all` (strongest durability) |

### Throughput
- **Normal**: ~2 batches/sec/node (200ms flush interval)
- **Incident**: Up to 20 batches/sec/node (100ms flush, full batches)
- **Cluster-wide** (50 nodes): ~1000 batches/sec peak

---

## Kafka → Layer-2: GNN Inference Flow

### Consumer Configuration
| Property | Value |
|---|---|
| Consumer group | `ccdt-layer2-cognitive` |
| Auto offset reset | `latest` (only live events) |
| Max poll records | 50 |
| Session timeout | 30s |
| Heartbeat interval | 10s |

### Processing Pipeline
```
TypedEbpfBatch (consumed)
    ↓
normalise_batch()         # extract per-pod feature deltas
    ↓
topology_builder.get()    # fetch current TopologySnapshot from K8s Watch API
    ↓
feature_extractor()       # compute 16-dim NodeFeatures per pod
    ↓
edge_feature_extractor()  # compute 4-dim EdgeFeatures from K8s Service graph
    ↓
gat_forward()             # 4-layer GAT, returns node embeddings + graph logits
    ↓
classify_nodes()          # per-node: HEALTHY / FAULT / ATTACK
    ↓
find_root_cause()         # do-calculus causal attribution
    ↓
compute_counterfactuals() # "if node X were healthy, P(incident) = ?"
    ↓
build_GnnInferenceResult()
    ↓
Kafka: ccdt.gnn.inference
```

### Output Message
```protobuf
GnnInferenceResult {
    inference_id:           string    // UUID4 (Kafka key)
    timestamp:              string    // RFC3339
    incident_type:          enum      // NONE | FAULT | ATTACK | FAULT_ATTACK | ...
    graph_confidence:       float32   // 0.0 – 1.0
    root_cause_node_id:     string
    root_cause_node_name:   string
    root_cause_confidence:  float32
    blast_radius_node_ids:  []string
    blast_radius_count:     int32
    node_classifications:   []TopologyNode  // per-node class + confidence
    causal_chain:           []CausalChainNode
    top_features:           []TopFeature   // top 10 most important features
    counterfactuals:        []CounterfactualResult
    inference_latency_ms:   float32
    node_count:             int32
    edge_count:             int32
    is_heartbeat:           bool      // true = healthy cluster, no incident
    schema_ver:             string    // "1.0"
}
```

### Kafka Configuration
| Property | Value |
|---|---|
| Topic | `ccdt.gnn.inference` |
| Partitions | 3 |
| Partition key | `inference_id` |
| Retention | 7 days |
| Publish rate | 1 per 5s heartbeat; triggered on batch receipt |

---

## Kafka → Layer-3: Guardian Action Flow

### Consumer Configuration
| Property | Value |
|---|---|
| Consumer group | `ccdt-layer3-guardian` |
| Min confidence threshold | 0.70 (ignores low-confidence inferences) |
| Heartbeat passthrough | false (heartbeats are dropped) |

### Processing Pipeline
```
GnnInferenceResult (consumed, confidence ≥ 0.70)
    ↓
rl_agent.select_action()    # PPO policy → ActionName + parameters
    ↓
ghost_preview.simulate()    # dry-run sandbox: risk score, blast radius, OPA
    ↓
opa_evaluator.evaluate()    # 5 Rego policies in parallel
    ↓
if approved:
    k8s_executor.execute()  # Kubernetes API call
else:
    approval_request()      # Send to human-in-loop queue
    ↓
build_ActionResult()
    ↓
Kafka: ccdt.guardian.actions
```

### Output Message
```protobuf
ActionResult {
    audit_id:               string    // UUID4 (Kafka key)
    request:                ActionRequest
    status:                 enum      // PENDING | APPROVED | EXECUTING | SUCCEEDED | ...
    message:                string
    error_detail:           string    // populated on failure
    requested_at:           string
    executed_at:            string
    completed_at:           string
    execution_duration_ms:  float32
    k8s_resource_version:   string
    verified_effect:        bool      // true = health check passed after action
    post_action_health:     float32   // 0.0 – 1.0
    autonomy_mode:          string
    approved_by:            string    // "operator:alice" or "rl-policy"
    incident_id:            string
    schema_ver:             string
}
```

### Kafka Configuration
| Property | Value |
|---|---|
| Topic | `ccdt.guardian.actions` |
| Partitions | 3 |
| Partition key | `audit_id` |
| Retention | 30 days (compliance) |

---

## Unified Incident Topic

### Publisher
Both Layer-2 (on first detection) and Layer-3 (on resolution) publish to this topic.

| Property | Value |
|---|---|
| Topic | `ccdt.incidents` |
| Partitions | 1 |
| Partition key | `incident_id` |
| Retention | 90 days |

---

## Service-to-Service REST/gRPC

Layer-4 Co-Pilot and the API Gateway call other layers directly over HTTP:

| Caller | Target | Endpoint | Purpose |
|---|---|---|---|
| Layer-4 | Layer-2 | `GET /topology` | Fetch cluster topology for LLM context |
| Layer-4 | Layer-2 | `GET /infer/latest` | Fetch most recent inference result |
| Layer-4 | Layer-3 | `POST /actions/preview` | Ghost Preview without execution |
| Layer-4 | Layer-3 | `POST /actions/propose` | Propose action (awaiting human approval) |
| API Gateway | Layer-2 | `GET /topology` | Dashboard topology view |
| API Gateway | Layer-3 | `GET /actions/history` | Dashboard action history |
| API Gateway | Layer-4 | `POST /chat` | Forward operator chat messages |
| API Gateway | Layer-4 | `POST /chat/stream` | Streaming operator chat |

---

## Error Handling Matrix

| Scenario | Layer | Behaviour |
|---|---|---|
| Kafka broker down | Layer-1 | Buffer events in-memory (max 10k), retry with backoff |
| GNN inference timeout | Layer-3 | Use last known inference if < 60s old; else block all actions |
| OPA sidecar down | Layer-3 | Local fallback evaluator (conservative: block HIGH+) |
| K8s API 429 | Layer-3 | Retry with exponential backoff (max 5 attempts) |
| K8s API 403 | Layer-3 | Dead-letter to `ccdt.incidents`; alert operator |
| Claude API down | Layer-4 | Return error to operator; session remains active |
| Claude API 529 | Layer-4 | Retry 3× with 5s delay; inform operator of delay |
| Guardian timeout from Co-Pilot | Layer-4 | Warn operator; block action proposals |
