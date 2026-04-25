# CCDT Operational Runbooks

Runbooks for on-call SREs and security engineers operating the CCDT platform.

| Runbook | Use when |
|---|---|
| [incident-response.md](incident-response.md) | CCDT detects a production incident |
| [deployment-guide.md](deployment-guide.md) | Deploying or upgrading CCDT |
| [scaling-guide.md](scaling-guide.md) | Scaling CCDT components under load |
| [disaster-recovery.md](disaster-recovery.md) | CCDT itself is compromised or down |
| [alert-playbook.md](alert-playbook.md) | Responding to specific Prometheus alerts |

## On-Call Contacts

| Role | Contact |
|---|---|
| Platform Engineering | `#ccdt-platform` Slack |
| Security Incident | `#security-incidents` Slack + PagerDuty P1 |
| Anthropic API issues | `https://status.anthropic.com` |

## Quick Reference

```bash
# Check CCDT system health
kubectl get pods -n ccdt-system

# View recent actions
kubectl logs -n ccdt-system deploy/layer3-guardian --tail=50 | grep AUDIT

# Emergency: disable all autonomous actions
kubectl set env deploy/layer3-guardian -n ccdt-system AUTONOMY_MODE=human-in-loop

# Emergency: pause Guardian entirely
kubectl scale deploy/layer3-guardian -n ccdt-system --replicas=0

# View Co-Pilot logs
kubectl logs -n ccdt-system deploy/layer4-copilot --tail=100

# Check Kafka consumer lag
kubectl exec -n ccdt-infra kafka-0 -- kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group ccdt-layer2-cognitive --describe
```
