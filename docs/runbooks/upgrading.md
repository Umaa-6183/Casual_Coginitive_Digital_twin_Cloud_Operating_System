# CCDT Upgrade Guide

**Version**: 1.0  
**Last Updated**: 2025-01-15  
**Applies to**: CCDT v1.x → v1.x patch/minor upgrades and major version upgrades

---

## Upgrade Strategy Overview

CCDT uses a **bottom-up** upgrade sequence. Always upgrade in this order:

```
Shared library (ccdt-shared) → Layer 1 → Layer 2 → Layer 3 → Layer 4 → API Gateway → Dashboard
```

Reverse the order for rollbacks.

**Why bottom-up?** Layer N+1 depends on Layer N's proto schema. Upgrading the producer before the consumer risks deserialization failures in production.

---

## Pre-Upgrade Checklist

```bash
# 1. Read the CHANGELOG for breaking changes
cat CHANGELOG.md | head -100

# 2. Backup current Helm values
helm get values ccdt -n ccdt > backups/ccdt-values-$(date +%Y%m%d).yaml

# 3. Backup RDS (snapshot)
aws rds create-db-cluster-snapshot \
  --db-cluster-identifier ccdt-prod \
  --db-cluster-snapshot-identifier ccdt-pre-upgrade-$(date +%Y%m%d)

# 4. Note current image tags (for rollback)
kubectl get deploy -n ccdt -o jsonpath=\
  '{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'

# 5. Verify all pods are healthy before starting
kubectl get pods -n ccdt | grep -v Running | grep -v Completed
# Should return empty (all running)

# 6. Check Kafka consumer lag is zero (no backlog)
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group ccdt-gnn-consumer | grep LAG
```

---

## Patch Upgrade (v1.x.y → v1.x.z)

Patch upgrades are bug fixes with no API or schema changes. Use rolling updates.

```bash
NEW_TAG="1.0.3"   # example

helm upgrade ccdt ./infra/helm/ccdt \
  --namespace ccdt \
  --set global.imageTag=$NEW_TAG \
  --reuse-values \
  --atomic \           # rollback automatically if any pod fails to become Ready
  --timeout 10m \
  --wait

# Verify
kubectl get pods -n ccdt -o wide
kubectl rollout status deployment/ccdt-layer2-cognitive -n ccdt
```

---

## Minor Upgrade (v1.x → v1.y)

Minor upgrades may include new Kafka topics, new proto fields (backwards-compatible), or new OPA policies. Follow the layer-by-layer sequence.

### Step 1 — Upgrade shared library

```bash
# Build and publish new ccdt-shared image
cd shared/
NEW_VERSION="1.1.0"
docker build -t ghcr.io/your-org/ccdt/shared:$NEW_VERSION .
docker push ghcr.io/your-org/ccdt/shared:$NEW_VERSION

# Update pyproject.toml version
sed -i "s/version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml
```

### Step 2 — Create new Kafka topics (if any)

```bash
# Check CHANGELOG for new topics
# Example: new topic ccdt.anomaly.scores added in v1.1
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --create --topic ccdt.anomaly.scores \
  --partitions 3 \
  --replication-factor 3 \
  --config retention.ms=604800000
```

### Step 3 — Upgrade Layer-1 (DaemonSet rolling update)

```bash
kubectl set image daemonset/ccdt-layer1-nervous \
  layer1-nervous=ghcr.io/your-org/ccdt/layer1-nervous:1.1.0 -n ccdt

kubectl rollout status daemonset/ccdt-layer1-nervous -n ccdt
kubectl logs -n ccdt -l app=ccdt-layer1-nervous --since=2m | grep -i "started\|error"
```

### Step 4 — Upgrade Layer-2 with canary (GNN model)

Minor upgrades may include a new GNN model checkpoint. Use canary deployment:

```bash
# Deploy canary (1 replica with new image, existing replicas keep old)
kubectl set image deployment/ccdt-layer2-cognitive \
  layer2-cognitive=ghcr.io/your-org/ccdt/layer2-cognitive:1.1.0 \
  -n ccdt \
  --record

# Watch canary pod
kubectl get pods -n ccdt -l app=ccdt-layer2-cognitive -w

# Monitor inference quality for 5 minutes
kubectl logs -n ccdt -l app=ccdt-layer2-cognitive --since=5m | \
  grep '"graph_confidence"' | jq '.graph_confidence' | \
  awk '{sum+=$1; n++} END {print "mean confidence:", sum/n}'
# Should remain > 0.70

# If healthy, complete rollout
kubectl rollout status deployment/ccdt-layer2-cognitive -n ccdt
```

### Step 5 — Upgrade Layer-3 with zero-downtime strategy

Guardian uses `Recreate` strategy (singleton). Schedule during low-incident window.

```bash
# Switch to human-in-loop BEFORE upgrade (prevents automated actions during restart)
kubectl set env deployment/ccdt-layer3-guardian -n ccdt AUTONOMY_MODE=human-in-loop
sleep 30   # allow in-flight actions to complete

# Upgrade
kubectl set image deployment/ccdt-layer3-guardian \
  layer3-guardian=ghcr.io/your-org/ccdt/layer3-guardian:1.1.0 \
  -n ccdt

kubectl rollout status deployment/ccdt-layer3-guardian -n ccdt

# Validate OPA policies loaded
kubectl logs -n ccdt -l app=ccdt-layer3-guardian --since=1m | \
  grep -i "opa\|policy.*loaded"

# Restore autonomy mode
kubectl set env deployment/ccdt-layer3-guardian -n ccdt AUTONOMY_MODE=supervised
```

### Step 6 — Upgrade Layer-4 (rolling, no downtime)

```bash
kubectl set image deployment/ccdt-layer4-copilot \
  layer4-copilot=ghcr.io/your-org/ccdt/layer4-copilot:1.1.0 \
  -n ccdt

kubectl rollout status deployment/ccdt-layer4-copilot -n ccdt

# Verify streaming works
curl -N -H "Authorization: Bearer $TOKEN" \
  "http://api-gateway:8000/api/v1/chat/stream" \
  -d '{"session_id": "test", "message": "health check"}' | head -3
```

### Step 7 — Upgrade API Gateway and Dashboard

```bash
kubectl set image deployment/ccdt-api-gateway \
  api-gateway=ghcr.io/your-org/ccdt/api-gateway:1.1.0 -n ccdt
kubectl set image deployment/ccdt-dashboard \
  dashboard=ghcr.io/your-org/ccdt/dashboard:1.1.0 -n ccdt
kubectl rollout status deployment/ccdt-api-gateway -n ccdt
kubectl rollout status deployment/ccdt-dashboard -n ccdt
```

### Step 8 — Post-upgrade validation

```bash
# Run smoke test suite
PYTHONPATH=/home/claude python3 -m pytest tests/e2e/test_health_endpoints.py -v

# Check all metrics are flowing
curl -s http://layer2-cognitive:8001/metrics | grep -c "^ccdt_"
# Should return > 20 metric lines

# Verify incident pipeline end-to-end
# (wait 30s for a heartbeat inference to appear)
kubectl logs -n ccdt -l app=ccdt-layer2-cognitive --since=1m | \
  grep '"is_heartbeat": true' | wc -l
# Should return >= 1
```

---

## Major Upgrade (v1 → v2)

Major upgrades may include breaking proto changes, new Kafka topic schemas, or renamed services. These require a **blue-green deployment**.

### Blue-Green Overview

```
Current (Blue)     New (Green)
─────────────      ──────────────
v1 Layer-1    →    v2 Layer-1 (new DaemonSet label)
v1 Layer-2    →    v2 Layer-2 (new Deployment)
v1 Layer-3    →    v2 Layer-3 (new Deployment)
v1 Kafka      →    shared or new cluster
Traffic: 100% Blue → gradually shift → 100% Green → delete Blue
```

### Step 1 — Deploy Green cluster alongside Blue

```bash
# Deploy green with separate Helm release name
helm install ccdt-green ./infra/helm/ccdt \
  --namespace ccdt-green \
  --create-namespace \
  --set global.imageTag=2.0.0 \
  --set global.kafkaBootstrap=ccdt-kafka.ccdt:9092 \  # shared Kafka (new topics)
  --set layer1.enabled=true \
  --set kafka.enabled=false \                          # reuse existing Kafka cluster
  -f values-v2.yaml
```

### Step 2 — Migrate Kafka topics

```bash
# Create v2 topics with new schema version
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --create --topic ccdt.ebpf.events.v2 --partitions 12 --replication-factor 3

# Mirror existing events to v2 topic during transition (MirrorMaker 2)
# See: infra/kafka/mirrormaker-v1-to-v2.yaml
```

### Step 3 — Shift traffic (10% → 50% → 100%)

```bash
# Update ingress to split traffic (requires nginx or Istio traffic splitting)
kubectl patch ingress ccdt-api-gateway -n ccdt --type merge \
  -p '{"metadata": {"annotations": {"nginx.ingress.kubernetes.io/canary": "true", "nginx.ingress.kubernetes.io/canary-weight": "10"}}}'

# Monitor error rates for 30 minutes before increasing weight
# If healthy:
kubectl patch ingress ccdt-api-gateway -n ccdt --type merge \
  -p '{"metadata": {"annotations": {"nginx.ingress.kubernetes.io/canary-weight": "50"}}}'
# After 30 more minutes, 100%:
kubectl patch ingress ccdt-api-gateway -n ccdt --type merge \
  -p '{"metadata": {"annotations": {"nginx.ingress.kubernetes.io/canary-weight": "100"}}}'
```

### Step 4 — Decommission Blue

```bash
# After 24 hours of stable Green operation
helm uninstall ccdt --namespace ccdt
kubectl delete namespace ccdt

# Rename green → production
kubectl label namespace ccdt-green environment=production
```

---

## Rollback Procedures

### Helm rollback (patch/minor upgrades)

```bash
# List recent releases
helm history ccdt -n ccdt

# Rollback to previous revision
helm rollback ccdt -n ccdt

# Rollback to specific revision
helm rollback ccdt 3 -n ccdt

# Verify rollback
kubectl rollout status deployment/ccdt-layer2-cognitive -n ccdt
```

### Manual image rollback (emergency)

```bash
# If Helm rollback is too slow, directly set images to previous tags
PREV_TAG="1.0.2"

kubectl set image deployment/ccdt-layer4-copilot   layer4-copilot=ghcr.io/your-org/ccdt/layer4-copilot:$PREV_TAG   -n ccdt
kubectl set image deployment/ccdt-layer3-guardian  layer3-guardian=ghcr.io/your-org/ccdt/layer3-guardian:$PREV_TAG  -n ccdt
kubectl set image deployment/ccdt-layer2-cognitive layer2-cognitive=ghcr.io/your-org/ccdt/layer2-cognitive:$PREV_TAG -n ccdt
kubectl set image daemonset/ccdt-layer1-nervous    layer1-nervous=ghcr.io/your-org/ccdt/layer1-nervous:$PREV_TAG    -n ccdt

# Wait for rollout
for d in ccdt-layer4-copilot ccdt-layer3-guardian ccdt-layer2-cognitive; do
  kubectl rollout status deployment/$d -n ccdt
done
```

---

## Proto Schema Compatibility Rules

CCDT uses protobuf for inter-layer messaging. Maintain these rules to ensure upgrade compatibility:

| Change type | Impact | Safe? |
|---|---|---|
| Add optional field (new field number) | New producers, old consumers ignore field | ✅ Safe |
| Remove field | Old consumers may fail if field was required | ❌ Unsafe |
| Rename field | Wire format unchanged (proto uses field numbers) | ✅ Safe |
| Change field number | Wire format BREAKS — all consumers fail | ❌ Never do this |
| Change field type (e.g., int32→int64) | May cause silent corruption | ❌ Unsafe |
| Add new enum value | Old consumers get unknown value | ⚠️ Careful |
| Add new message type | No impact on existing consumers | ✅ Safe |

**CCDT proto versioning**: Each `.proto` file has a `schema_ver` string field. Breaking changes require a new topic (`ccdt.ebpf.events.v2`) and a parallel consumer group during migration.

---

## Version Compatibility Matrix

| ccdt-shared | Layer-1 | Layer-2 | Layer-3 | Layer-4 | API GW |
|---|---|---|---|---|---|
| 1.0.x | 1.0.x | 1.0.x | 1.0.x | 1.0.x | 1.0.x |
| 1.1.x | 1.0.x–1.1.x | 1.1.x | 1.1.x | 1.0.x–1.1.x | 1.0.x–1.1.x |
| 2.0.x | 2.0.x | 2.0.x | 2.0.x | 2.0.x | 2.0.x |

---

## Upgrade SLA Targets

| Upgrade type | Planned downtime | Max allowed downtime |
|---|---|---|
| Patch (bug fix) | 0 (rolling) | 30 seconds |
| Minor (new features) | 0 (rolling) | 2 minutes |
| Major (breaking) | 0 (blue-green) | 5 minutes |
