# Runbook: Incident Response

This runbook describes how to respond when CCDT detects and remediates a production incident. It covers both **automated** and **manual** intervention paths.

---

## Severity Classification

| Severity | GNN Confidence | Incident Type | Response SLA |
|---|---|---|---|
| CRITICAL | ≥ 0.85 | ATTACK or FAULT_ATTACK | Immediate (< 5 min) |
| HIGH | ≥ 0.70 | FAULT with blast_radius ≥ 3 | 15 minutes |
| MEDIUM | 0.50–0.70 | FAULT or PERFORMANCE | 30 minutes |
| LOW | < 0.50 | Any | Next business day |

---

## Phase 1: Detection (T+0 to T+30s)

CCDT automatically detects the incident. No operator action required unless:
- Dashboard shows a CRITICAL incident with `autonomy_mode=human-in-loop`
- PagerDuty fires a P1 alert (CRITICAL + ATTACK incidents always page)

**Check detection quality**:
```bash
# View the GNN inference that triggered the incident
curl -H "Authorization: Bearer $TOKEN" \
  https://ccdt.internal/api/v1/topology/inference/latest | jq .

# Expected output fields:
# .incident_type:          "FAULT" or "ATTACK"
# .graph_confidence:       ≥ 0.70
# .root_cause_node_name:   the affected service
# .nl_summary:             plain English explanation
```

**If confidence is borderline (0.70–0.75)**:
- Check `.top_features` — do they make sense for the incident type?
- If uncertain, manually investigate before approving Guardian actions

---

## Phase 2: Automated Remediation (T+30s to T+90s)

In `supervised` mode, CCDT will:
1. Run Ghost Preview to compute risk score
2. Check OPA policies (5 policies in parallel)
3. If risk ≤ 0.35 AND all OPA pass: auto-execute
4. If risk > 0.35 OR any OPA fail: send `approval_required` WebSocket event

**Monitor the action stream**:
```bash
# Watch action log in real time
kubectl logs -n ccdt-system deploy/layer3-guardian -f | grep -E "AUDIT|ERROR"
```

**Approve a pending action** (if in `human-in-loop` mode):
```bash
# Via API
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://ccdt.internal/api/v1/actions/approve/<audit_id> \
  -d '{"decision": "approve", "reason": "Risk is acceptable"}'

# Via Dashboard: Actions → Pending Approvals → Approve
```

**Deny an action** (if you disagree with CCDT):
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://ccdt.internal/api/v1/actions/approve/<audit_id> \
  -d '{"decision": "deny", "reason": "Root cause analysis looks wrong"}'
```

---

## Phase 3: Manual Investigation (Parallel to Automation)

Use the Co-Pilot interface while CCDT automates remediation:

1. Open the Dashboard → Co-Pilot tab
2. The incident is automatically injected into the conversation context
3. Ask questions:
   - "Show me the eBPF events from the last 5 minutes on payment-svc"
   - "What's the blast radius if we isolate auth-svc?"
   - "Why did you choose restart_pod over rollback_deployment?"
   - "What's the causal chain here?"

**Co-Pilot tool use** — the AI can call:
- `get_topology` → current cluster state
- `get_ebpf_events` → raw kernel events
- `run_ghost_preview` → simulate any action
- `propose_action` → queue an action for approval

---

## Phase 4: Attack Response (INCIDENT_ATTACK)

If incident_type is ATTACK or FAULT_ATTACK:

1. **Immediately escalate** to the security team (`#security-incidents`)
2. Do NOT dismiss the incident — preserve evidence
3. Check what CCDT proposes:
   - `isolate_container`: creates a deny-all NetworkPolicy for the attacker
   - `rotate_secret`: rotates all secrets accessible to the compromised service
   - `cordon_node`: prevents new pods from scheduling on the affected node
4. Review the `capability_events` in the raw eBPF data for the attack vector
5. After containment, run a full forensic investigation before removing isolation

---

## Phase 5: Verification (T+90s to T+5min)

CCDT automatically performs a post-action health check. Verify:

```bash
# Check that the action had the desired effect
curl -H "Authorization: Bearer $TOKEN" \
  https://ccdt.internal/api/v1/actions/history?limit=5 | jq '.entries[0] | {status, verified_effect, post_action_health}'

# Expected:
# "status": "SUCCEEDED"
# "verified_effect": true
# "post_action_health": > 0.90
```

If `verified_effect = false` or `post_action_health < 0.70`:
- CCDT will re-run GNN inference and may propose a follow-up action
- You can also manually propose an action via Co-Pilot

---

## Phase 6: Resolution

Mark the incident resolved when:
- `post_action_health > 0.90` for 5+ minutes
- No new FAULT/ATTACK nodes in the last 5 minutes
- All services have `ready_replicas_ratio = 1.0`

```bash
curl -X PATCH -H "Authorization: Bearer $TOKEN" \
  https://ccdt.internal/api/v1/incidents/<incident_id> \
  -d '{"state": "RESOLVED", "operator_notes": "OOM resolved by restart; memory limit increased"}'
```

---

## Rollback an Action

If a CCDT action made things worse:

```bash
# Via API — propose a rollback
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://ccdt.internal/api/v1/actions/execute \
  -d '{
    "action_name": "ROLLBACK_DEPLOYMENT",
    "target_node_name": "payment-svc",
    "target_namespace": "production",
    "parameters": {"revision": 0}
  }'

# Via kubectl (emergency manual rollback)
kubectl rollout undo deployment/payment-svc -n production
```

---

## False Positive Workflow

If CCDT fired a false positive:

1. Mark the incident as `FALSE_POSITIVE` via the Dashboard or API
2. This creates a negative fine-tuning example to improve future accuracy
3. If false positives are frequent (> 1/day), review GNN model performance:
   ```bash
   # Check recent precision / recall metrics
   curl https://ccdt.internal/metrics | grep ccdt_gnn_inference_precision
   ```
4. Consider retraining with updated labelled data
