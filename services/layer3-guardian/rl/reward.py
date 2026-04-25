"""
CCDT Layer-3 Guardian — Shaped Reward Function
═══════════════════════════════════════════════════════════════════════════════
Reward shaping design principles:

  1. Restoration reward   Positive reward proportional to improvement in
                          node status (critical→warning = +5, warning→healthy = +8)

  2. Action efficiency    Penalty for redundant or ineffective actions to
                          encourage minimal intervention

  3. Safety penalties     Large negative rewards for dangerous actions on
                          healthy nodes (over-remediation is harmful)

  4. Incident-type bonus  Attack scenarios reward secret rotation and
                          network isolation more highly than pod restarts

  5. MTTR incentive       Per-step penalty encourages faster resolution

  6. Terminal bonuses     Large positive reward for full recovery,
                          moderate penalty for timeout, small penalty for
                          human escalation (some incidents warrant it)

Reward scale:
  Full recovery (5 nodes)    +50 to +80
  Per step penalty           -0.5
  Ineffective action         -2.0
  Safe no-op                 +0.1
  Human escalation           -5.0 (not terrible — sometimes correct)
  Timeout                    -20.0
  OPA violation (illegal)    -25.0
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("ccdt.guardian.reward")

# ─── Reward constants ──────────────────────────────────────────────────────────
R_CRITICAL_TO_WARNING  = 5.0
R_WARNING_TO_HEALTHY   = 8.0
R_HEALTHY_MAINTAINED   = 0.1     # small reward for each healthy node per step
R_FULL_RECOVERY_BONUS  = 30.0   # bonus when ALL nodes are healthy
R_PER_STEP_PENALTY     = -0.5   # MTTR incentive — faster is better
R_INEFFECTIVE_ACTION   = -2.0   # action had no effect
R_OVER_REMEDIATION     = -3.0   # aggressive action on healthy node
R_ESCALATION           = -5.0   # escalated to human
R_TIMEOUT              = -20.0  # episode timed out
R_OPA_VIOLATION        = -25.0  # OPA blocked the action

# Per-incident-type action bonus table
# Rows = incident type, cols = action_id → bonus multiplier
ATTACK_BONUS_ACTIONS = {8, 9, 10, 1}   # network_policy, rotate_secrets, kill_process, isolate
FAULT_BONUS_ACTIONS  = {2, 5, 11, 4}   # rollback, restart, oom_threshold, scale_up
ATTACK_BONUS         = 2.0
FAULT_BONUS          = 2.0


def compute_reward(
    nodes:          list[dict],
    action:         int,
    action_success: bool,
    step:           int,
    max_steps:      int,
    incident_type:  str,                  # fault | attack
    prev_nodes:     Optional[list[dict]] = None,
    opa_violated:   bool                  = False,
) -> float:
    """
    Compute shaped reward for one transition.

    Args:
        nodes           Current node states (after action)
        action          Action index taken
        action_success  Whether the action succeeded
        step            Current episode step
        max_steps       Maximum episode steps
        incident_type   fault | attack
        prev_nodes      Node states before action (optional, for delta reward)
        opa_violated    Whether OPA blocked this action

    Returns:
        Scalar reward value.
    """
    reward = 0.0

    # ── OPA violation ─────────────────────────────────────────────────────────
    if opa_violated:
        return R_OPA_VIOLATION

    # ── Per-step MTTR penalty ─────────────────────────────────────────────────
    reward += R_PER_STEP_PENALTY

    # ── Action effectiveness ──────────────────────────────────────────────────
    if not action_success:
        reward += R_INEFFECTIVE_ACTION

    # ── Node status delta reward ──────────────────────────────────────────────
    n_healthy  = sum(1 for n in nodes if n["status"] == "healthy")
    n_warning  = sum(1 for n in nodes if n["status"] == "warning")
    n_critical = sum(1 for n in nodes if n["status"] == "critical")

    if prev_nodes is not None:
        prev_critical = sum(1 for n in prev_nodes if n["status"] == "critical")
        prev_warning  = sum(1 for n in prev_nodes if n["status"] == "warning")

        delta_critical = prev_critical - n_critical   # positive = improvement
        delta_warning  = prev_warning  - n_warning    # positive = fewer warning

        reward += delta_critical * R_CRITICAL_TO_WARNING
        reward += delta_warning  * R_WARNING_TO_HEALTHY
    else:
        # Without prev_nodes, reward proportional to healthy fraction
        n_nodes = max(len(nodes), 1)
        reward += n_healthy / n_nodes * 2.0

    # ── Healthy node maintenance bonus ────────────────────────────────────────
    reward += n_healthy * R_HEALTHY_MAINTAINED

    # ── Incident-type action bonus ────────────────────────────────────────────
    if incident_type == "attack" and action in ATTACK_BONUS_ACTIONS and action_success:
        reward += ATTACK_BONUS
    elif incident_type == "fault" and action in FAULT_BONUS_ACTIONS and action_success:
        reward += FAULT_BONUS

    # ── Over-remediation penalty ──────────────────────────────────────────────
    # Penalise aggressive actions (isolate, drain, kill) on already-healthy clusters
    aggressive_actions = {1, 7, 10, 8}   # isolate, drain, kill, network_policy
    if action in aggressive_actions and n_critical == 0:
        reward += R_OVER_REMEDIATION

    # ── No-op on healthy cluster ──────────────────────────────────────────────
    if action == 0 and n_critical == 0 and n_warning == 0:
        reward += 0.5   # correct decision to do nothing

    # ── Terminal conditions ────────────────────────────────────────────────────
    all_healthy = n_critical == 0 and n_warning == 0
    if all_healthy:
        # Bonus scales with speed of recovery
        speed_bonus = max(0, (max_steps - step) / max_steps) * 20.0
        reward += R_FULL_RECOVERY_BONUS + speed_bonus

    elif action == 14:   # escalate_to_human
        reward += R_ESCALATION

    elif step >= max_steps:
        reward += R_TIMEOUT

    return float(reward)


# ─── Reward shaping utilities ─────────────────────────────────────────────────

def compute_episode_stats(episode_rewards: list[float]) -> dict:
    """Compute episode-level statistics from step rewards."""
    if not episode_rewards:
        return {}

    import numpy as np
    arr = np.array(episode_rewards)
    return {
        "episode_return":    float(arr.sum()),
        "episode_length":    len(arr),
        "mean_step_reward":  float(arr.mean()),
        "min_step_reward":   float(arr.min()),
        "max_step_reward":   float(arr.max()),
        "positive_steps":    int((arr > 0).sum()),
        "negative_steps":    int((arr < 0).sum()),
    }


def action_reward_table() -> dict:
    """
    Return a lookup table of expected reward contribution per action.
    Used by Ghost Preview to explain predicted outcomes.
    """
    return {
        0:  {"name": "no_op",                "expected_reward_healthy": 0.5,  "expected_reward_incident": -0.5},
        1:  {"name": "isolate_container",    "expected_reward_healthy": -3.0, "expected_reward_incident": 7.0},
        2:  {"name": "rollback_deployment",  "expected_reward_healthy": -1.0, "expected_reward_incident": 9.0},
        3:  {"name": "scale_down_replicas",  "expected_reward_healthy": -0.5, "expected_reward_incident": 4.0},
        4:  {"name": "scale_up_replicas",    "expected_reward_healthy": -0.5, "expected_reward_incident": 6.0},
        5:  {"name": "restart_pod",          "expected_reward_healthy": -1.5, "expected_reward_incident": 8.0},
        6:  {"name": "cordon_node",          "expected_reward_healthy": -2.0, "expected_reward_incident": 5.0},
        7:  {"name": "drain_node",           "expected_reward_healthy": -4.0, "expected_reward_incident": 7.0},
        8:  {"name": "apply_network_policy", "expected_reward_healthy": -3.0, "expected_reward_incident": 8.0},
        9:  {"name": "rotate_secrets",       "expected_reward_healthy": -1.0, "expected_reward_incident": 9.0},
        10: {"name": "kill_process",         "expected_reward_healthy": -4.0, "expected_reward_incident": 7.0},
        11: {"name": "increase_oom_threshold","expected_reward_healthy":-0.5, "expected_reward_incident": 5.0},
        12: {"name": "throttle_cpu",         "expected_reward_healthy": -1.0, "expected_reward_incident": 4.0},
        13: {"name": "enable_debug_logging", "expected_reward_healthy": 0.0,  "expected_reward_incident": 0.0},
        14: {"name": "escalate_to_human",    "expected_reward_healthy": -6.0, "expected_reward_incident": -5.0},
    }
