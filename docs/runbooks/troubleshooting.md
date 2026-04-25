# CCDT Troubleshooting Guide

**Version**: 1.0  
**Audience**: Platform Engineers, SRE, On-Call  
**Last Updated**: 2025-01-15

---

## Quick Diagnostic Checklist

Run this first for any CCDT issue:

```bash
# 1. Platform health
kubectl get pods -n ccdt -o wide
kubectl get events -n ccdt --sort-by='.lastTimestamp' | tail -30

# 2. Kafka topic health
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --describe --topic ccdt.ebpf.events

# 3. Consumer group lag (critical for detecting backlog)
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group ccdt-gnn-consumer

# 4. Service metrics
curl -s http://layer2-cognitive:8001/metrics | grep -E "ccdt_gnn|ccdt_layer2"
curl -s http://layer3-guardian:8002/metrics | grep -E "ccdt_guardian"
curl -s http://layer4-copilot:8003/metrics  | grep -E "ccdt_copilot"
```

---

## Layer-by-Layer Symptoms & Fixes

### Layer-1 — eBPF Nervous System

#### `layer1-nervous-collector` pod keeps OOMKilling

**Symptoms**: Pod restarts with OOMKilled exit code. `/metrics` unavailable.

**Cause**: Ring buffer or perf buffer sizes too large for node memory.

**Fix**:
```bash
# Check current memory usage
kubectl top pod -n ccdt -l app=ccdt-layer1-nervous

# Reduce ring buffer (restart triggers DaemonSet rolling update)
kubectl set env daemonset/ccdt-layer1-nervous -n ccdt \
  RING_BUFFER_SIZE_MB=32 \
  PERF_BUFFER_PAGES=256

# Or via Helm
helm upgrade ccdt ./infra/helm/ccdt -n ccdt \
  --set layer1.ringBufferSizeMb="32" \
  --set layer1.perfBufferPages="256"
```

---

#### No events appearing on `ccdt.ebpf.events`

**Diagnostic**:
```bash
# Check collector is running
kubectl get pods -n ccdt -l app=ccdt-layer1-nervous

# Check Kafka producer errors
kubectl logs -n ccdt -l app=ccdt-layer1-nervous --since=5m | grep -i "kafka\|error\|warn"

# Verify Kafka connectivity from pod
kubectl exec -n ccdt -it $(kubectl get pod -n ccdt -l app=ccdt-layer1-nervous -o name | head -1) \
  -- sh -c 'nc -zv $KAFKA_BOOTSTRAP_SERVERS'
```

**Common causes**:
- Kafka `KAFKA_BOOTSTRAP_SERVERS` env var wrong → check `configmap/ccdt-config`
- eBPF programs failed to load → check for `bpf_prog_load failed` in logs
- Node kernel < 5.8 → eBPF CO-RE not supported; upgrade kernel

**Fix for eBPF load failure**:
```bash
# Check kernel version
kubectl debug -n ccdt node/<node-name> -it --image=busybox \
  -- sh -c 'uname -r'

# Minimum required: 5.8.0
# Recommended: 5.15+ for full CO-RE support
```

---

#### High `sched_latency_p99_us` alerts firing

**Symptoms**: `ccdt_layer1_sched_latency_p99_us > 50000` (50ms).

**Cause**: CPU saturation on the node hosting the Layer-1 DaemonSet pod.

**Investigation**:
```bash
# Check node CPU usage
kubectl top node <affected-node>

# Check which pods are on that node
kubectl get pods -n ccdt --field-selector spec.nodeName=<affected-node> -o wide

# Check if it's a noisy neighbour
kubectl top pod --all-namespaces --field-selector spec.nodeName=<affected-node>
```

**Fix**: Cordon node and drain; Layer-1 DaemonSet will restart on healthy nodes.

---

### Layer-2 — Causal GNN Cognitive Core

#### GNN inference latency > 500ms (SLO breach)

**Symptoms**: `ccdt_gnn_inference_latency_seconds_p99 > 0.5`. Alerts: `CcdtGnnLatencyHigh`.

**Diagnostic**:
```bash
# Check GNN pod resource usage
kubectl top pod -n ccdt -l app=ccdt-layer2-cognitive

# Check if GPU is being used (if available)
kubectl exec -n ccdt deployment/ccdt-layer2-cognitive -- \
  python3 -c "import torch; print(torch.cuda.is_available())"

# Check Kafka consumer lag (events queued = backlog)
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group ccdt-gnn-consumer | grep ccdt.ebpf.events
```

**Fixes by root cause**:

| Root cause | Fix |
|---|---|
| Kafka event backlog > 10K | Scale Layer-2: `kubectl scale deploy/ccdt-layer2-cognitive --replicas=4 -n ccdt` |
| CPU bottleneck | Increase `OMP_NUM_THREADS` env var or request more CPU |
| Model too large | Reduce GNN hidden dim (requires retraining; interim: add replicas) |
| Python GIL contention | Switch to `ProcessPoolExecutor` for inference (restart required) |

---

#### `GnnInferenceResult` deserialization errors in Layer-3

**Symptoms**: `ccdt_guardian_kafka_errors_total{error="deserialization"}` counter increasing.

**Cause**: Schema mismatch — Layer-2 published a proto that Layer-3 can't parse.

**Diagnostic**:
```bash
# Check Layer-2 version
kubectl get deploy/ccdt-layer2-cognitive -n ccdt -o jsonpath='{.spec.template.spec.containers[0].image}'

# Check Layer-3 version  
kubectl get deploy/ccdt-layer3-guardian -n ccdt -o jsonpath='{.spec.template.spec.containers[0].image}'

# They must use the same proto schema version
# Check shared library version
kubectl exec -n ccdt deployment/ccdt-layer2-cognitive -- \
  pip show ccdt-shared | grep Version
```

**Fix**: Rolling update both Layer-2 and Layer-3 to the same image tag.

---

#### GNN always classifies everything as HEALTHY (false negatives)

**Symptoms**: No incidents detected even when `kubectl top pod` shows saturation.

**Cause**: Model checkpoint is stale, or feature normalization is wrong (different training vs prod distribution).

**Investigation**:
```bash
# Pull a sample inference result
curl -s http://layer2-cognitive:8001/infer \
  -H "Content-Type: application/json" \
  -d '{"batch_id": "test", "node_name": "debug"}' | jq '.top_features'

# Check model checkpoint age
ls -la /app/checkpoints/
```

**Fix**: Trigger model retrain from recent data, then rolling update with new checkpoint.

---

### Layer-3 — Guardian (RL + OPA)

#### OPA blocking all actions (`opa_violations_total` high)

**Symptoms**: All Ghost Previews return `opa_approved: false`. Guardian actions are stuck in `DENIED`.

**Diagnostic**:
```bash
# Check OPA decision logs
kubectl logs -n ccdt -l app=ccdt-layer3-guardian -c opa --since=10m | \
  grep '"allow":false' | head -20

# Check which policy is blocking
curl -s http://layer3-guardian:8002/actions/history | \
  jq '.actions[] | select(.status == "DENIED") | .opa_violations'
```

**Common OPA violations and fixes**:

| Violation | Cause | Fix |
|---|---|---|
| `policy.blast_radius_exceeds_threshold` | Action would affect > 20% of pods | Switch to scoped action (namespace only) |
| `policy.action_rate_exceeded` | > 5 actions/minute | Wait for rate limit window (60s) |
| `policy.unauthorized_namespace` | Target ns not in allowlist | Add ns to `rego/guardian_policies.rego` |
| `policy.business_hours_restriction` | Action outside allowed window | Override with manual approval |
| `policy.lateral_movement_risk` | Too many actions on same node | Use `AUTONOMY_MODE=human-in-loop` temporarily |

**Emergency OPA bypass** (production incidents only, requires approval):
```bash
# Temporarily set fallback-only mode (skips OPA gRPC, uses embedded allow-all policy)
kubectl set env deployment/ccdt-layer3-guardian -n ccdt \
  OPA_FALLBACK_LOCAL=true OPA_FALLBACK_ALLOW=true
# IMPORTANT: Revert after incident resolution
```

---

#### Ghost Preview always returns risk_score > 0.35 (safe actions blocked)

**Symptoms**: Guardian proposes actions but all are blocked at Ghost Preview stage.

**Diagnostic**:
```bash
# Call Ghost Preview directly
curl -s -X POST http://layer3-guardian:8002/actions/preview \
  -H "Content-Type: application/json" \
  -d '{
    "action_name": "RESTART_POD",
    "target_namespace": "production",
    "target_node_name": "payment-svc-pod-abc",
    "trigger_confidence": 0.91
  }' | jq '.risk_score, .risk_category, .recommendation_reason'
```

**Causes and fixes**:
- `GNN service unreachable` → Ghost Preview can't re-run post-action inference → fix GNN connectivity
- `Stale topology` → StateCloner sees all nodes as FAULT → wait for GNN to recover
- `Risk threshold too low` → increase `GHOST_RISK_THRESHOLD=0.50` temporarily

---

#### RL agent selecting `no_op` (action 0) repeatedly

**Symptoms**: Guardian acknowledges incidents but never executes remediation. `ccdt_guardian_actions_total{action="no_op"}` high.

**Cause**: RL agent observation space doesn't match what the model was trained on (feature dimension mismatch).

**Investigation**:
```bash
# Get current observation
curl -s http://layer3-guardian:8002/debug/observation | jq '.obs_shape, .obs_stats'

# Should be shape [48] with values in [0, 1]
# If values are NaN or > 1, normalization is broken
```

**Fix**: Check `VecNormalize` statistics file matches checkpoint:
```bash
kubectl exec -n ccdt deployment/ccdt-layer3-guardian -- \
  ls -la /app/checkpoints/
# guardian_ppo_final.zip and guardian_ppo_final_vecnormalize.pkl must have same timestamp
```

---

### Layer-4 — Co-Pilot

#### Co-Pilot SSE stream cuts off after first token

**Symptoms**: Browser receives first SSE chunk, then connection drops.

**Cause**: `uvicorn` worker timeout or reverse proxy (nginx/ALB) closing idle connections.

**Fix**:
```bash
# Increase uvicorn keepalive
kubectl set env deployment/ccdt-layer4-copilot -n ccdt \
  UVICORN_TIMEOUT_KEEP_ALIVE=75 \
  UVICORN_TIMEOUT=600

# If behind ALB: add annotation to ingress
kubectl annotate ingress ccdt-api-gateway -n ccdt \
  nginx.ingress.kubernetes.io/proxy-read-timeout=600 \
  nginx.ingress.kubernetes.io/proxy-send-timeout=600
```

---

#### "AuthenticationError: Invalid API Key" from Anthropic

**Diagnostic**:
```bash
# Check secret is mounted correctly
kubectl exec -n ccdt deployment/ccdt-layer4-copilot -- \
  sh -c 'echo $ANTHROPIC_API_KEY | cut -c1-15'
# Should print: sk-ant-api03-

# Verify secret in K8s
kubectl get secret ccdt-secrets -n ccdt -o jsonpath='{.data.anthropic-api-key}' | \
  base64 -d | cut -c1-15
```

**Fix**:
```bash
# Rotate API key
kubectl create secret generic ccdt-secrets -n ccdt \
  --from-literal=anthropic-api-key='sk-ant-api03-NEW_KEY' \
  --dry-run=client -o yaml | kubectl apply -f -

# Restart Co-Pilot to pick up new key
kubectl rollout restart deployment/ccdt-layer4-copilot -n ccdt
```

---

#### Co-Pilot context too large → `anthropic.BadRequestError: prompt too long`

**Symptoms**: Chat requests fail with 400 error for incidents with long history.

**Cause**: System prompt + session history exceeds 200K token context window.

**Fix**:
```bash
# Reduce rolling window
kubectl set env deployment/ccdt-layer4-copilot -n ccdt \
  MAX_HISTORY_TURNS=10   # down from 20

# Or clear affected session
curl -X DELETE http://api-gateway:8000/api/v1/sessions/<session_id> \
  -H "Authorization: Bearer $TOKEN"
```

---

### API Gateway

#### 503 errors on all endpoints

**Diagnostic**:
```bash
# Check upstream health
kubectl exec -n ccdt deployment/ccdt-api-gateway -- \
  wget -qO- http://layer4-copilot:8003/health

# Check gateway logs
kubectl logs -n ccdt -l app=ccdt-api-gateway --since=5m | grep -i "error\|upstream"

# Check if Redis session store is healthy
kubectl exec -n ccdt -it $(kubectl get pod -n ccdt -l app=redis-master -o name) -- \
  redis-cli ping
```

---

#### JWT validation always failing (401 Unauthorized)

```bash
# Decode the JWT being sent (without validation)
echo "<jwt_token>" | cut -d. -f2 | base64 -d 2>/dev/null | jq .

# Check exp field — is it in the past?
# Check aud field — must match gateway's expected audience

# Verify JWT_SECRET matches what the token was signed with
kubectl get secret ccdt-secrets -n ccdt -o jsonpath='{.data.jwt-secret}' | base64 -d
```

---

## Kafka Deep Dives

### Consumer lag accumulating (> 50K messages)

```bash
# Per-partition lag breakdown
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group ccdt-gnn-consumer

# Identify which partitions are lagging
# If lag is only on certain partitions → potential consumer skew

# Emergency: increase GNN consumer replicas (must not exceed partition count=12)
kubectl scale deploy/ccdt-layer2-cognitive -n ccdt --replicas=6
```

### Topic retention full (disk pressure on Kafka nodes)

```bash
# Check disk usage
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  df -h /bitnami/kafka/data

# Reduce retention for high-volume topics
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-configs.sh --bootstrap-server localhost:9092 \
  --alter --entity-type topics \
  --entity-name ccdt.ebpf.events \
  --add-config retention.ms=43200000   # 12 hours instead of 24
```

---

## Metrics Reference

| Metric | Alert Threshold | Meaning |
|---|---|---|
| `ccdt_layer1_ebpf_events_total` | rate drops to 0 | Collector stopped; check DaemonSet |
| `ccdt_gnn_inference_latency_seconds{quantile="0.99"}` | > 0.5s | GNN overloaded; scale up |
| `ccdt_gnn_graph_confidence` | < 0.5 consistently | Model degraded; retrain |
| `ccdt_guardian_actions_total{status="DENIED"}` | > 10/5m | OPA over-restricting; check policies |
| `ccdt_guardian_ghost_preview_seconds{quantile="0.99"}` | > 2s | GNN unreachable from Ghost Preview |
| `ccdt_copilot_tokens_input_total` (rate) | > 100K/min | Runaway session; check for loops |
| `ccdt_copilot_errors_total` | any spike | Check Anthropic API status |
| `ccdt_api_gateway_requests_total{status=~"5.."}` | > 1% rate | Backend failures; check upstreams |

---

## Log Search Patterns

```bash
# Find all ATTACK incidents in last 1 hour
kubectl logs -n ccdt -l app=ccdt-layer2-cognitive --since=1h | \
  grep '"incident_type": "ATTACK"' | jq -r '.inference_id + " " + .root_cause_node_name'

# Find Guardian OPA denials
kubectl logs -n ccdt -l app=ccdt-layer3-guardian --since=1h | \
  grep '"AUDIT:guardian_action"' | grep '"outcome": "DENIED"'

# Find Co-Pilot errors
kubectl logs -n ccdt -l app=ccdt-layer4-copilot --since=1h | \
  grep '"level": "ERROR"' | jq '{ts, msg, session_id}'

# Find slow GNN inferences (> 200ms)
kubectl logs -n ccdt -l app=ccdt-layer2-cognitive --since=1h | \
  grep '"latency_ms"' | jq 'select(.latency_ms > 200)'
```

---

## Emergency Procedures

### Full platform restart (ordered)

```bash
# Order matters: restart from Layer-4 down to Layer-1
kubectl rollout restart deployment/ccdt-layer4-copilot   -n ccdt && \
  kubectl rollout status  deployment/ccdt-layer4-copilot  -n ccdt
kubectl rollout restart deployment/ccdt-layer3-guardian  -n ccdt && \
  kubectl rollout status  deployment/ccdt-layer3-guardian -n ccdt
kubectl rollout restart deployment/ccdt-layer2-cognitive -n ccdt && \
  kubectl rollout status  deployment/ccdt-layer2-cognitive -n ccdt
kubectl rollout restart daemonset/ccdt-layer1-nervous    -n ccdt && \
  kubectl rollout status  daemonset/ccdt-layer1-nervous   -n ccdt
```

### Disable Guardian autonomous actions (human-in-loop mode)

```bash
kubectl set env deployment/ccdt-layer3-guardian -n ccdt AUTONOMY_MODE=human-in-loop
kubectl rollout status deployment/ccdt-layer3-guardian -n ccdt
echo "Guardian is now in human-in-loop mode. All actions require explicit approval."
```

### Re-enable full auto after incident

```bash
kubectl set env deployment/ccdt-layer3-guardian -n ccdt AUTONOMY_MODE=supervised
kubectl rollout status deployment/ccdt-layer3-guardian -n ccdt
```
