# CCDT Disaster Recovery Runbook

**Version**: 1.0  
**RTO Target**: 30 minutes (full platform)  
**RPO Target**: 5 minutes (event loss window)  
**Last Updated**: 2025-01-15  
**Owner**: Platform Engineering SRE

---

## DR Scenarios

| Scenario | RTO | RPO | Automated? |
|---|---|---|---|
| Single pod failure | 2 min | 0 | ✅ K8s self-healing |
| Single node failure | 5 min | 30s | ✅ K8s eviction |
| Kafka broker failure (1 of 3) | 5 min | 0 (replicated) | ✅ Kafka HA |
| Full AZ failure | 15 min | 1 min | Partial (manual steps) |
| Full cluster failure | 30 min | 5 min | ❌ Manual |
| Database corruption | 60 min | varies | ❌ Manual restore |
| Ransomware / full data loss | 4 hours | last backup | ❌ Manual |

---

## Pre-Requisites

Before any DR procedure, confirm you have:

```bash
# Cluster access
kubectl cluster-info
kubectl auth can-i '*' '*' --all-namespaces

# Terraform state (for full rebuild)
cd infra/terraform && terraform workspace show

# Helm values backup
helm get values ccdt -n ccdt > /tmp/ccdt-values-backup.yaml

# Anthropic API key (stored in 1Password: CCDT/Anthropic)
# AWS credentials with EKS + RDS + S3 access
```

---

## Scenario 1: Single Pod / Deployment Failure

**Detection**: PagerDuty alert `CcdtServiceDown` or `kubectl get pods -n ccdt`.

**Recovery** (automated by K8s):
```bash
# Verify K8s is self-healing
kubectl get pods -n ccdt -w

# If pod is stuck in CrashLoopBackOff > 5 min, intervene:
kubectl describe pod -n ccdt <pod-name>
kubectl logs -n ccdt <pod-name> --previous

# Force restart
kubectl delete pod -n ccdt <pod-name>
```

**Validation**: `curl -f http://<service>/health` returns 200.

---

## Scenario 2: Single Kafka Broker Failure

**Detection**: Alert `CcdtKafkaBrokerDown` or missing ISR replica.

CCDT Kafka is deployed as a 3-node KRaft cluster with `min.insync.replicas=2`. One broker can fail without data loss or write unavailability.

```bash
# Check which broker is down
kubectl get pods -n ccdt -l app.kubernetes.io/component=controller

# Check ISR for critical topics
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --describe --topic ccdt.ebpf.events | grep "Isr:"

# ISR should still have 2+ replicas. If only 1:
# → Writes will block until broker recovers
# → Do NOT increase min.insync.replicas (would make cluster read-only)

# Force dead broker pod to restart
kubectl delete pod -n ccdt <kafka-controller-N>
kubectl rollout status statefulset/ccdt-kafka-controller -n ccdt
```

**Validation**:
```bash
# Produce a test message
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-console-producer.sh --bootstrap-server localhost:9092 \
  --topic ccdt.ebpf.events <<< '{"test": "dr-validation"}'

# Verify it's consumable
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic ccdt.ebpf.events --from-beginning --max-messages 1 --timeout-ms 5000
```

---

## Scenario 3: Full Availability Zone Failure

If an entire AWS AZ goes down, pods scheduled there become unavailable. CCDT uses pod anti-affinity to spread across AZs, so this causes a partial outage.

**Detection**: Multiple `CcdtServiceDown` alerts firing simultaneously. AWS Health Dashboard shows AZ impact.

**Step 1 — Verify AZ failure scope**:
```bash
# Check which nodes are down
kubectl get nodes -o wide | grep -v Ready

# Check which AZ the failed nodes are in
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.topology\.kubernetes\.io/zone}{"\n"}{end}'
```

**Step 2 — Drain the affected AZ**:
```bash
# Get nodes in failed AZ
FAILED_AZ="us-east-1b"
FAILED_NODES=$(kubectl get nodes -l topology.kubernetes.io/zone=$FAILED_AZ -o name)

for node in $FAILED_NODES; do
  kubectl cordon $node
done
```

**Step 3 — Force reschedule**:
```bash
# Delete pods stuck on failed AZ nodes (K8s will reschedule)
kubectl delete pods -n ccdt \
  --field-selector spec.nodeName=<failed-node-1>
kubectl delete pods -n ccdt \
  --field-selector spec.nodeName=<failed-node-2>
```

**Step 4 — Scale up to compensate**:
```bash
# Temporarily increase replicas to compensate for reduced capacity
kubectl scale deployment/ccdt-layer2-cognitive -n ccdt --replicas=4
kubectl scale deployment/ccdt-layer4-copilot   -n ccdt --replicas=4
```

**Step 5 — Validate recovery**:
```bash
# All pods should be Running in surviving AZs
kubectl get pods -n ccdt -o wide | grep -v Terminating | grep Running

# Check Kafka has full ISR
kubectl exec -n ccdt ccdt-kafka-controller-0 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --describe --topic ccdt.gnn.inference

# Verify event flow
kubectl logs -n ccdt -l app=ccdt-layer2-cognitive --since=2m | \
  grep '"incident_type"' | tail -5
```

**Step 6 — Restore after AZ recovery**:
```bash
# Uncordon nodes
for node in $FAILED_NODES; do
  kubectl uncordon $node
done

# Restore replica counts
kubectl scale deployment/ccdt-layer2-cognitive -n ccdt --replicas=2
kubectl scale deployment/ccdt-layer4-copilot   -n ccdt --replicas=2
```

---

## Scenario 4: Full EKS Cluster Failure

**Prerequisites**: Terraform state is accessible. Helm chart + values are backed up.

**Estimated RTO**: 30 minutes

### Step 1 — Spin up new EKS cluster (Terraform)

```bash
cd infra/terraform

# Check if existing cluster state is salvageable
terraform show | grep -A3 'module.eks'

# If not, create new cluster in same VPC (preserves RDS and Kafka EBS volumes)
terraform apply -target=module.eks \
  -var="cluster_name=ccdt-prod-dr" \
  -var="vpc_id=$(terraform output vpc_id)" \
  -auto-approve

# Update kubeconfig
aws eks update-kubeconfig \
  --name ccdt-prod-dr \
  --region us-east-1 \
  --kubeconfig ~/.kube/ccdt-dr
export KUBECONFIG=~/.kube/ccdt-dr
```

### Step 2 — Restore Kafka data (if EBS volumes survive)

```bash
# Check if EBS volumes from Kafka PVCs still exist
aws ec2 describe-volumes \
  --filters "Name=tag:kubernetes.io/created-for/pvc/namespace,Values=ccdt" \
  --query 'Volumes[*].[VolumeId,State,Tags[?Key==`kubernetes.io/created-for/pvc/name`].Value|[0]]' \
  --output table

# If volumes exist, create PVs manually referencing the existing EBS volumes
# See: infra/kubernetes/kafka-pv-recovery.yaml.template
```

### Step 3 — Deploy CCDT platform

```bash
# Install cert-manager first
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=cert-manager -n cert-manager --timeout=120s

# Restore secrets
kubectl create namespace ccdt
kubectl create secret generic ccdt-secrets -n ccdt \
  --from-literal=anthropic-api-key="$(op read op://CCDT/Anthropic/api-key)" \
  --from-literal=jwt-secret="$(op read op://CCDT/JWT/secret)" \
  --from-literal=kafka-password="$(op read op://CCDT/Kafka/password)"

# Deploy platform
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install ccdt ./infra/helm/ccdt \
  -f /tmp/ccdt-values-backup.yaml \
  --namespace ccdt \
  --create-namespace \
  --set global.imageTag="$LAST_KNOWN_GOOD_TAG" \
  --timeout 15m \
  --wait
```

### Step 4 — Restore RDS state

```bash
# RDS Aurora was not in the EKS cluster, it should still be running
# Verify connectivity from new cluster
kubectl run -n ccdt db-test --image=postgres:15 --rm -it -- \
  psql "host=$(terraform output rds_endpoint) user=ccdt dbname=ccdt" \
  -c "SELECT COUNT(*) FROM incidents;"
```

### Step 5 — Validate full pipeline

```bash
# Run the health check suite
kubectl run -n ccdt e2e-smoke --image=python:3.11 --rm -it \
  -- python3 -m pytest /app/tests/e2e/test_health_endpoints.py -v

# Verify Kafka event flow (should see events within 30s)
kubectl logs -n ccdt -l app=ccdt-layer2-cognitive --since=2m | \
  grep '"is_heartbeat": true' | tail -3

# Check all layer health endpoints
for svc in layer1-nervous:9100 layer2-cognitive:8001 layer3-guardian:8002 layer4-copilot:8003; do
  echo -n "Health $svc: "
  kubectl exec -n ccdt deployment/ccdt-api-gateway -- \
    wget -qO- http://$svc/health 2>/dev/null | jq -r '.status' || echo "FAILED"
done
```

---

## Scenario 5: Database (RDS Aurora) Failure

**Detection**: Layer-3 Guardian logs `psycopg2.OperationalError`. Incident history not loading.

**Note**: Kafka is the primary event store. RDS stores only incident history and fine-tuning data. A temporary RDS failure does NOT stop CCDT from detecting and remediating incidents.

### Point-in-time restore

```bash
# Identify target restore time (e.g., 15 minutes before data corruption)
RESTORE_TIME="2025-01-15T14:30:00Z"

# Create new Aurora cluster from PITR
aws rds restore-db-cluster-to-point-in-time \
  --db-cluster-identifier ccdt-prod-pitr \
  --source-db-cluster-identifier ccdt-prod \
  --restore-to-time $RESTORE_TIME \
  --region us-east-1

# Wait for restore (typically 10-20 minutes)
aws rds wait db-cluster-available \
  --db-cluster-identifier ccdt-prod-pitr

# Get new endpoint
NEW_ENDPOINT=$(aws rds describe-db-clusters \
  --db-cluster-identifier ccdt-prod-pitr \
  --query 'DBClusters[0].Endpoint' --output text)

# Update CCDT to use new endpoint
kubectl set env deployment/ccdt-layer3-guardian -n ccdt \
  DATABASE_URL="postgresql://ccdt:$(op read op://CCDT/DB/password)@$NEW_ENDPOINT:5432/ccdt"
kubectl rollout restart deployment/ccdt-layer3-guardian -n ccdt
```

---

## Scenario 6: GNN Model Checkpoint Corruption

**Detection**: Layer-2 fails to start with `RuntimeError: model checkpoint invalid`.

**Recovery**:
```bash
# List available checkpoints in S3
aws s3 ls s3://ccdt-prod-models/checkpoints/ --recursive | sort

# Find last known good checkpoint
GOOD_CHECKPOINT=$(aws s3 ls s3://ccdt-prod-models/checkpoints/ | \
  grep 'gnn_checkpoint' | tail -1 | awk '{print $4}')

# Restore checkpoint
aws s3 cp s3://ccdt-prod-models/checkpoints/$GOOD_CHECKPOINT \
  /tmp/gnn_checkpoint.pt

# Update model PVC  
kubectl cp /tmp/gnn_checkpoint.pt \
  ccdt-layer2-cognitive-0:/app/checkpoints/gnn_checkpoint.pt -n ccdt

kubectl rollout restart deployment/ccdt-layer2-cognitive -n ccdt
kubectl rollout status  deployment/ccdt-layer2-cognitive -n ccdt
```

**If no valid checkpoint exists** — deploy untrained model (inference will be degraded):
```bash
# Download the pre-trained base model (stored as OCI artifact in GHCR)
docker pull ghcr.io/your-org/ccdt/gnn-base-model:latest
docker run --rm ghcr.io/your-org/ccdt/gnn-base-model:latest \
  cat /model/gnn_checkpoint.pt > /tmp/gnn_base.pt

kubectl cp /tmp/gnn_base.pt \
  ccdt-layer2-cognitive-0:/app/checkpoints/gnn_checkpoint.pt -n ccdt
kubectl rollout restart deployment/ccdt-layer2-cognitive -n ccdt
```

---

## DR Test Schedule

| Test | Frequency | Last Run | Owner |
|---|---|---|---|
| Pod failure simulation | Weekly | — | SRE on-call |
| Kafka broker restart | Monthly | — | Platform Eng |
| Full AZ failover | Quarterly | — | Platform Eng |
| Full cluster rebuild | Semi-annually | — | Platform Eng |
| DB point-in-time restore | Quarterly | — | Platform Eng |

**DR test procedure**: Use `tests/chaos/chaos_runner.py --suite resilience` for automated validation.

---

## Communication Templates

### Initial incident notification (Slack #ccdt-incidents)

```
🔴 CCDT DR Event — [INCIDENT TYPE]
Time: [UTC timestamp]
Scope: [Which layers/services affected]
Current status: [Investigating / Remediating / Recovering]
ETA for recovery: [estimate]
Incident commander: [name]
Bridge: [zoom/meet link]
```

### Resolution notification

```
✅ CCDT DR Resolved
Resolved at: [UTC timestamp]
Total duration: [X] minutes
Root cause: [brief description]
Impact: [what was affected, what wasn't]
Action items: [link to post-mortem]
```

---

## Post-DR Checklist

After any Scenario 3+ recovery:

- [ ] All pods Running and Ready
- [ ] Kafka all topics have `Isr: 3` (full replicas)
- [ ] Layer-2 producing heartbeat inferences (`is_heartbeat: true`)
- [ ] Layer-3 Guardian healthy, OPA policies loaded
- [ ] Layer-4 Co-Pilot responding to chat requests
- [ ] API Gateway returning 200 on `/health`
- [ ] Prometheus scraping all CCDT metrics
- [ ] PagerDuty alerts back to normal (no spurious firing)
- [ ] Incident post-mortem scheduled within 48 hours
- [ ] DR test documented with findings
