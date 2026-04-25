# Runbook: Deployment Guide

Complete guide for deploying and upgrading CCDT in a production Kubernetes cluster.

---

## Prerequisites

| Requirement | Minimum Version | Check Command |
|---|---|---|
| Kubernetes | 1.29 | `kubectl version` |
| Helm | 3.14 | `helm version` |
| Terraform | 1.8 | `terraform version` |
| AWS CLI | 2.15 | `aws --version` |
| kubectl context | Target cluster | `kubectl config current-context` |
| Linux kernel (nodes) | 5.8 (BTF enabled) | `uname -r` |
| Anthropic API key | — | `echo $ANTHROPIC_API_KEY` |

---

## First-Time Installation

### Step 1: Infrastructure (Terraform)
```bash
cd infra/terraform
terraform init
terraform plan -var="cluster_name=prod-ccdt" -var="region=us-east-1"
terraform apply

# Outputs: eks_cluster_endpoint, rds_endpoint, kafka_bootstrap_servers
```

### Step 2: Configure kubectl
```bash
aws eks update-kubeconfig --name prod-ccdt --region us-east-1
kubectl get nodes  # verify cluster access
```

### Step 3: Create Secrets
```bash
# Anthropic API key
kubectl create secret generic ccdt-anthropic \
  --from-literal=api_key="$ANTHROPIC_API_KEY" \
  -n ccdt-system

# Database credentials (from Terraform output)
kubectl create secret generic ccdt-db \
  --from-literal=url="postgresql://ccdt:$(terraform output -raw db_password)@$(terraform output -raw rds_endpoint)/ccdt" \
  -n ccdt-system

# JWT public key (for API Gateway)
kubectl create configmap ccdt-jwt-pubkey \
  --from-file=public.pem=./certs/jwt-public.pem \
  -n ccdt-system
```

### Step 4: Install Helm Chart
```bash
cd infra/helm
helm dependency update ./ccdt
helm install ccdt ./ccdt \
  --namespace ccdt-system \
  --create-namespace \
  --values values.yaml \
  --values values-production.yaml \
  --set layer1.image.tag="1.0.0" \
  --set layer2.image.tag="1.0.0" \
  --set layer3.image.tag="1.0.0" \
  --set layer4.image.tag="1.0.0" \
  --timeout=10m \
  --wait
```

### Step 5: Verify Installation
```bash
# All pods should be Running
kubectl get pods -n ccdt-system

# Expected output:
# NAME                                  READY   STATUS    RESTARTS
# layer1-nervous-collector-<hash>-<id>  1/1     Running   0        (one per node)
# layer2-cognitive-<hash>               1/1     Running   0
# layer2-cognitive-<hash>               1/1     Running   0
# layer3-guardian-<hash>                1/1     Running   0
# layer4-copilot-<hash>                 1/1     Running   0
# ccdt-api-gateway-<hash>               1/1     Running   0
# ccdt-api-gateway-<hash>               1/1     Running   0
# ccdt-dashboard-<hash>                 1/1     Running   0
# opa-<hash>                            1/1     Running   0

# Check Layer-2 is producing inferences
kubectl logs -n ccdt-system deploy/layer2-cognitive --tail=20 | grep "inference"

# Check Guardian is in supervised mode
kubectl exec -n ccdt-system deploy/layer3-guardian -- env | grep AUTONOMY_MODE
# Expected: AUTONOMY_MODE=supervised

# Run smoke test
curl https://ccdt.internal/api/v1/health | jq .
```

---

## Upgrading CCDT

### Rolling Upgrade (zero downtime)
```bash
# Update image tags in values-production.yaml, then:
helm upgrade ccdt ./ccdt \
  --namespace ccdt-system \
  --values values.yaml \
  --values values-production.yaml \
  --set layer2.image.tag="1.1.0" \
  --set layer3.image.tag="1.1.0" \
  --timeout=10m \
  --wait

# Verify rollout
kubectl rollout status deploy/layer2-cognitive -n ccdt-system
kubectl rollout status deploy/layer3-guardian  -n ccdt-system
kubectl rollout status deploy/layer4-copilot   -n ccdt-system
```

### Pre-Upgrade Checklist
- [ ] Put Guardian into `human-in-loop` mode during upgrade window
- [ ] Check no active incidents are in `REMEDIATING` state
- [ ] Take a backup of the Aurora database
- [ ] Notify on-call operators via `#ccdt-platform` Slack

### Rollback
```bash
# If upgrade fails
helm rollback ccdt -n ccdt-system
kubectl rollout status deploy/layer3-guardian -n ccdt-system
```

---

## Upgrading OPA Policies

OPA policies are loaded as a Kubernetes ConfigMap. They can be updated without restarting Guardian:

```bash
# Edit the Rego policies
vim infra/kubernetes/layer3-guardian/opa-configmap.yaml

# Apply the update
kubectl apply -f infra/kubernetes/layer3-guardian/opa-configmap.yaml

# OPA reloads automatically (no restart needed)
# Verify policy loaded:
kubectl logs -n ccdt-system deploy/layer3-guardian --tail=10 | grep "policy"
```

---

## Upgrading the GNN Model

```bash
# 1. Upload new model weights to S3
aws s3 cp model_v2.pt s3://ccdt-models/layer2/model_v2.pt

# 2. Update model path in ConfigMap
kubectl edit configmap ccdt-layer2-config -n ccdt-system
# Change: MODEL_PATH=/models/model_v1.pt
# To:     MODEL_PATH=/models/model_v2.pt

# 3. Restart Layer-2 to pick up new model
kubectl rollout restart deploy/layer2-cognitive -n ccdt-system
kubectl rollout status  deploy/layer2-cognitive -n ccdt-system

# 4. Watch first 5 inferences to verify model is working
kubectl logs -n ccdt-system deploy/layer2-cognitive -f | grep "inference_id" | head -5
```

---

## Environment Variables Reference

| Service | Variable | Default | Description |
|---|---|---|---|
| All | `LOG_LEVEL` | `INFO` | Python logging level |
| All | `LOG_FORMAT` | `json` | `json` or `pretty` |
| Layer-1 | `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | Kafka brokers |
| Layer-1 | `EBPF_FLUSH_INTERVAL_MS` | `100` | Ring buffer flush interval |
| Layer-2 | `MODEL_PATH` | `/models/model.pt` | GNN model weights |
| Layer-2 | `INFERENCE_INTERVAL_S` | `5` | Heartbeat interval |
| Layer-2 | `MIN_CONFIDENCE_PUBLISH` | `0.0` | Min confidence to publish |
| Layer-3 | `AUTONOMY_MODE` | `supervised` | `human-in-loop` / `supervised` / `full-auto` |
| Layer-3 | `OPA_URL` | `http://opa:8181` | OPA server URL |
| Layer-3 | `MIN_CONFIDENCE_ACT` | `0.70` | Min GNN confidence to act |
| Layer-4 | `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Claude model ID |
| Layer-4 | `MAX_TOKENS` | `4096` | Max response tokens |
| API GW | `AUTH_DISABLED` | `false` | Disable JWT auth (dev only) |
| API GW | `CORS_ORIGINS` | `https://ccdt.internal` | Allowed CORS origins |
