# ADR-004: Reinforcement Learning Guardian Over Rule-Based Remediation

**Status**: Accepted  
**Date**: 2024-12-15  
**Authors**: CCDT Platform Engineering

---

## Context

We needed a policy for selecting Kubernetes remediation actions (restart_pod, scale_up, rollback, isolate_container, etc.) given a GNN inference result. Options considered:
- Hardcoded rule tree ("if OOM then restart_pod")
- Supervised learning (learn from historical incidents)
- Reinforcement learning (learn from outcomes)

## Decision

Use **Stable-Baselines3 PPO** (Proximal Policy Optimization) with a custom Gymnasium environment.

### RL Environment
```python
Observation space (Box, 64-dim):
  [incident_type_onehot (6),
   graph_confidence,
   root_cause_class_onehot (4),
   root_cause_confidence,
   top_feature_values (16),
   blast_radius_count_normalized,
   recent_action_history (16),  # last 8 actions × 2
   autonomy_mode_onehot (3),
   time_of_day_sin_cos (2),
   cluster_health_score]

Action space (Discrete, 20):
  ACTION_RESTART_POD, ACTION_SCALE_UP_REPLICAS, ACTION_ROLLBACK_DEPLOYMENT,
  ACTION_ISOLATE_CONTAINER, ACTION_APPLY_NETWORK_POLICY,
  ACTION_ROTATE_SECRET, ACTION_CORDON_NODE, ... (10 more)

Reward function:
  +3.0  if action resolved incident (post_action_health > 0.90)
  +1.5  if action improved health (0.70 < health < 0.90)
  +0.5  if action had no effect (health unchanged)
  -1.0  if action worsened health
  -2.0  if action caused OPA violation
  -3.0  if action triggered cascading failure
  -0.1  per second MTTR penalty (encourages fast resolution)
  +0.2  if operator approved without modification
  -0.5  if operator reversed the action
```

### Ghost Preview Integration
Every RL-selected action is run through **Ghost Preview** before execution. Ghost Preview runs a dry-run in a sandboxed simulation and computes:
- `risk_score`: 0.0–1.0
- `blast_radius`: number of affected pods
- `mttr_delta_seconds`: estimated MTTR change
- OPA policy evaluation result

If `risk_score > 0.60` or OPA rejects, the action is escalated to human approval regardless of the RL Q-value.

## Consequences

**Positive**
- RL policy learns from actual outcomes (post-action health, operator feedback)
- Adapts to cluster-specific patterns without re-coding rules
- PPO is stable and sample-efficient for discrete action spaces
- Ghost Preview acts as a safety net independent of the RL policy

**Negative**
- Cold start: requires ~10k incidents to train a reliable policy
- Exploration during training can cause suboptimal actions in production → mitigated by starting in `human-in-loop` mode
- Reward shaping is non-trivial and requires domain expertise

## Alternatives Considered

**Hardcoded rules**: Fast to implement, but cannot generalise. "if OOM then restart" is wrong when OOM is caused by a memory leak that will immediately recur.

**Supervised learning on historical incidents**: Cannot learn from novel situations and requires labelled data that is often unavailable.
