# CCDT Security Model

## Threat Model

CCDT is a **privileged security platform** with:
- **Read access** to all pod logs and metrics across every namespace
- **Execute access** to restart, scale, isolate, or drain any workload
- **Write access** to NetworkPolicies and ResourceQuotas
- **Persistent kernel access** via eBPF programs on every node

This makes CCDT itself a high-value target. A compromised CCDT could be used to exfiltrate cluster credentials, disable security controls, or cause deliberate outages.

---

## Trust Boundaries

```
┌─────────────────────────────────────────────────────────────────────┐
│  UNTRUSTED ZONE                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ Operator     │  │ External     │  │ Monitored workloads      │  │
│  │ browser      │  │ webhooks     │  │ (any namespace)          │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────────────────┘  │
│         │                 │                                          │
│  ═══════╪═════════════════╪══════════  TRUST BOUNDARY               │
│         ▼                 ▼                                          │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  CCDT TRUSTED ZONE (namespace: ccdt-system)                  │   │
│  │                                                              │   │
│  │  API Gateway ──JWT──► Layer-4 Co-Pilot                       │   │
│  │      │                    │                                  │   │
│  │      │              Layer-3 Guardian ──RBAC──► K8s API       │   │
│  │      │                    │                                  │   │
│  │      └──────────► Layer-2 GNN ◄──── Layer-1 eBPF             │   │
│  │                                                              │   │
│  │  ═══════════════════════════════  INNER TRUST BOUNDARY       │   │
│  │                                                              │   │
│  │  Kafka (ccdt-infra)   OPA sidecar   Aurora RDS               │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Authentication & Authorization

### Operator Access (API Gateway)
- **Mechanism**: JWT (RS256) signed by an external IdP (Okta / Google Workspace)
- **Token lifetime**: 15 minutes
- **Refresh**: Short-lived refresh token via secure cookie
- **Claims required**: `sub` (operator ID), `groups` (rbac groups)

### Service-to-Service Authentication
All inter-service calls within `ccdt-system` use **mTLS** via Cilium service mesh:
- Each service has a unique SPIFFE SVID
- Certificate rotation: every 24 hours
- No service can impersonate another

### Kubernetes RBAC

Layer-3 Guardian Service Account (`ccdt-guardian`):
```yaml
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list", "patch"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["networkpolicies"]
    verbs: ["create", "delete", "get", "list"]
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "patch"]
```

Layer-1 Collector Service Account (`ccdt-nervous`):
```yaml
rules:
  - apiGroups: [""]
    resources: ["pods", "nodes", "namespaces"]
    verbs: ["get", "list", "watch"]  # read-only
```

---

## OPA Policy Enforcement (Layer-3)

Before executing any Kubernetes action, Guardian evaluates **5 mandatory Rego policies** in parallel. All 5 must return `allow = true` or the action is blocked.

### Policy 1: blast_radius
```rego
# Block if action would affect more than 10 pods
allow if {
    input.ghost_result.affected_pod_count <= 10
    input.ghost_result.risk_score < 0.75
}
```

### Policy 2: cpu_threshold
```rego
# Block scale-down if current CPU utilisation < 20%
allow if {
    not is_scale_down
}
allow if {
    is_scale_down
    input.current_cpu_utilization > 0.20
}
```

### Policy 3: lateral_movement
```rego
# Block if same target received an action in the last 5 minutes
allow if {
    not recent_action_on_target
}
recent_action_on_target if {
    some action in input.action_history
    action.target_node_name == input.request.target_node_name
    action.completed_at_unix > (input.now_unix - 300)
}
```

### Policy 4: rate_limit
```rego
# Block if more than 10 actions have been executed in the last 60 seconds
allow if {
    count_recent_actions < 10
}
count_recent_actions := count([a |
    some a in input.action_history
    a.completed_at_unix > (input.now_unix - 60)
])
```

### Policy 5: working_hours
```rego
# In human-in-loop mode, disruptive actions require working hours approval
allow if {
    input.autonomy_mode != "human-in-loop"
}
allow if {
    input.autonomy_mode == "human-in-loop"
    is_working_hours
}
is_working_hours if {
    hour := time.clock(time.now_ns())[0]
    hour >= 8
    hour <= 18
}
```

---

## eBPF Security Considerations

Layer-1 runs eBPF programs in the kernel. Mitigations:

| Risk | Mitigation |
|---|---|
| eBPF program exploiting kernel bug | CO-RE programs verified by kernel eBPF verifier; BTF type safety |
| Collector process privilege escalation | Runs in restricted securityContext (no `privileged: true`; uses `CAP_BPF` + `CAP_PERFMON` only) |
| Ring buffer data exfiltration | All data published to Kafka stays within cluster VPC; mTLS on all Kafka connections |
| eBPF map memory exhaustion | Per-map size limits set in eBPF program; kernel OOM kills process before kernel exhaustion |

### Required Linux Capabilities (Layer-1 only)
```yaml
securityContext:
  capabilities:
    add:
      - BPF
      - PERFMON
      - SYS_RESOURCE   # required for rlimit adjustments
    drop:
      - ALL
  readOnlyRootFilesystem: true
  runAsNonRoot: false   # eBPF requires UID 0 for program loading
  runAsUser: 0
```

---

## Secrets Management

| Secret | Storage | Rotation |
|---|---|---|
| Anthropic API key | Kubernetes Secret (sealed with SealedSecrets) | Manual; auto-rotate monthly |
| Kafka mTLS certificates | cert-manager (Let's Encrypt) | Automatic, 90-day certs |
| Database credentials | AWS Secrets Manager (rotated by RDS) | Automatic, 30 days |
| JWT public key (IdP) | ConfigMap (public key only) | On IdP key rotation |
| Guardian K8s ServiceAccount token | Auto-mounted by K8s | 1-hour TTL (token projection) |

---

## Network Policies

CCDT enforces a default-deny network policy within `ccdt-system`:

```yaml
# Default deny all ingress + egress
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: ccdt-system
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
```

Exceptions are explicitly granted per service. Layer-3 Guardian, for example, is only allowed to reach the Kubernetes API server and the OPA sidecar.

---

## Audit Logging

All CCDT decisions are logged with full provenance:

```json
{
  "ts": "2024-12-20T14:23:45Z",
  "level": "INFO",
  "audit": true,
  "service": "layer3-guardian",
  "audit_event": "guardian_action",
  "audit_id": "a1b2c3d4-...",
  "actor": "rl-policy",
  "target": "production/payment-svc-pod-abc123",
  "action": "restart_pod",
  "outcome": "SUCCEEDED",
  "risk_score": 0.12,
  "opa_approved": true,
  "operator_approved_by": "alice@company.com",
  "inference_id": "e5f6...",
  "incident_id": "g7h8..."
}
```

Audit logs are:
- Written to stdout (collected by Fluent Bit → CloudWatch Logs)
- Retained for 1 year
- Immutable (CloudWatch log groups with resource-based policies blocking deletion)
- Searchable via CloudWatch Insights

---

## Incident Response for CCDT Itself

If CCDT is suspected to be compromised:

1. **Immediately**: Set `AUTONOMY_MODE=human-in-loop` via K8s ConfigMap patch
2. **Within 5 minutes**: Review all actions in the last 24h via `GET /actions/history`
3. **Within 30 minutes**: Rotate all secrets (Anthropic key, DB credentials, mTLS certs)
4. **If confirmed compromise**: Scale Layer-3 Guardian to 0 replicas; manually review all NetworkPolicies created by CCDT
5. **Post-incident**: Review eBPF programs for unauthorized modifications; check DaemonSet image hashes

See [runbooks/disaster-recovery.md](../runbooks/disaster-recovery.md) for the full procedure.
