# Runbook: Scaling Guide

How to scale CCDT components under increased load.

---

## When to Scale

| Signal | Component | Action |
|---|---|---|
| GNN inference latency p99 > 100ms | Layer-2 | Scale up replicas |
| Guardian queue depth > 50 pending actions | Layer-3 | Investigate (single-instance by design) |
| Co-Pilot API latency > 5s (non-Claude) | Layer-4 | Scale up replicas |
| API Gateway request latency p99 > 500ms | API Gateway | Scale up replicas |
| Kafka consumer lag > 5000 messages | Layer-2 | Scale up replicas + increase partitions |
| Layer-1 CPU > 70% on any node | Layer-1 | Reduce eBPF event rate or increase node CPU |

---

## Scaling Layer-2 (GNN)

Layer-2 is stateless — scale horizontally:
```bash
# Scale to 4 replicas
kubectl scale deploy/layer2-cognitive -n ccdt-system --replicas=4

# Or update Helm values:
helm upgrade ccdt ./ccdt -n ccdt-system --set layer2.replicas=4

# Verify all replicas are consuming from Kafka
kubectl exec -n ccdt-infra kafka-0 -- kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group ccdt-layer2-cognitive --describe
```

If scaling horizontally doesn't help (CPU-bound on large topologies):
```bash
# Increase CPU limits
kubectl patch deploy layer2-cognitive -n ccdt-system --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/cpu","value":"4000m"}]'
```

---

## Scaling Layer-3 (Guardian)

**Layer-3 is intentionally single-instance** to prevent concurrent conflicting actions. Do NOT scale to multiple replicas.

If Layer-3 is a bottleneck:
1. Check if Ghost Preview is the bottleneck: `kubectl logs deploy/layer3-guardian | grep "sim_duration"`
2. Reduce Ghost Preview simulation depth in config
3. Consider caching OPA policy evaluations (OPA has built-in caching)

---

## Scaling Kafka

If consumer lag is growing:
```bash
# Increase partitions (requires Kafka broker restart):
# 1. Increase layer2.replicaFactor in values.yaml
# 2. Manually rebalance consumer group after partition increase

# Quick fix: restart consumers to trigger rebalance
kubectl rollout restart deploy/layer2-cognitive -n ccdt-system
```

---

## HPA Configuration

Layer-2 uses a Horizontal Pod Autoscaler:
```yaml
# infra/kubernetes/layer2-cognitive/hpa.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  minReplicas: 2
  maxReplicas: 8
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
```
