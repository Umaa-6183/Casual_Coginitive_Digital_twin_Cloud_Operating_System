"""
CCDT Layer-3 Guardian — Cluster Incident Gymnasium Environment
═══════════════════════════════════════════════════════════════════════════════
Wraps the cluster state (from Layer-2 topology) as a Gymnasium environment
so a PPO agent can learn remediation policies.

Observation space (flat vector, 48-dim):
  Per-node features     (max 10 nodes × 4 features = 40)
    [cpu_pct, mem_pct, status_enc, incident_class]
  Global features       (8)
    [incident_severity, blast_radius_norm, node_count_norm,
     fault_node_ratio, attack_node_ratio, elapsed_steps_norm,
     actions_taken_norm, previous_action_success]

Action space (Discrete, 15):
   0  no_op                    — do nothing, observe
   1  isolate_container        — network-isolate the root-cause pod
   2  rollback_deployment      — kubectl rollout undo
   3  scale_down_replicas      — reduce replica count by 50%
   4  scale_up_replicas        — increase replicas to absorb load
   5  restart_pod              — delete pod (K8s recreates)
   6  cordon_node              — mark node unschedulable
   7  drain_node               — evict all pods from node
   8  apply_network_policy     — deny ingress to compromised namespace
   9  rotate_secrets           — trigger secret rotation
  10  kill_process             — SIGKILL specific PID
  11  increase_oom_threshold   — raise cgroup memory limit
  12  throttle_cpu             — apply cpu quota limit
  13  enable_debug_logging     — collect forensic data
  14  escalate_to_human        — page on-call; episode ends

Reward is provided by reward.py.
Episode terminates when:
  - All nodes reach healthy status  (success)
  - Max steps exceeded              (timeout)
  - Action 14 (escalate) taken      (human handoff)
  - Critical safety violation       (OPA blocked)
"""
from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Optional

import numpy as np
import gymnasium as gym
from gymnasium import spaces

logger = logging.getLogger("ccdt.guardian.env")

# ─── Constants ────────────────────────────────────────────────────────────────
MAX_NODES       = 10
NODE_FEAT_DIM   = 4       # [cpu, mem, status, class]
GLOBAL_FEAT_DIM = 8
OBS_DIM         = MAX_NODES * NODE_FEAT_DIM + GLOBAL_FEAT_DIM   # 48
NUM_ACTIONS     = 15
MAX_STEPS       = 50

# Status encoding for observation
STATUS_ENC = {"healthy": 0.0, "warning": 0.5, "critical": 1.0}
CLASS_ENC  = {"healthy": 0.0, "fault": 0.5, "attack": 1.0}

# Action names (index → string)
ACTION_NAMES = [
    "no_op",
    "isolate_container",
    "rollback_deployment",
    "scale_down_replicas",
    "scale_up_replicas",
    "restart_pod",
    "cordon_node",
    "drain_node",
    "apply_network_policy",
    "rotate_secrets",
    "kill_process",
    "increase_oom_threshold",
    "throttle_cpu",
    "enable_debug_logging",
    "escalate_to_human",
]

assert len(ACTION_NAMES) == NUM_ACTIONS


# ─── Node state dict used internally ─────────────────────────────────────────

def _make_node(
    node_id:     str,
    cpu:         float = 0.3,
    mem:         float = 0.4,
    status:      str   = "healthy",
    node_class:  str   = "healthy",
) -> dict:
    return {
        "id":     node_id,
        "cpu":    float(cpu),
        "mem":    float(mem),
        "status": status,
        "class":  node_class,
    }


# ─── Environment ─────────────────────────────────────────────────────────────

class ClusterIncidentEnv(gym.Env):
    """
    Gymnasium environment modelling cluster incident remediation.

    Can be initialised from a real topology dict (from Layer-2) or
    from one of the built-in synthetic incident scenarios.
    """

    metadata = {"render_modes": ["human", "ansi"]}

    def __init__(
        self,
        topology:        Optional[dict] = None,
        incident_type:   str            = "fault",    # fault | attack
        max_steps:       int            = MAX_STEPS,
        render_mode:     Optional[str]  = None,
    ) -> None:
        super().__init__()

        self._base_topology   = topology
        self._incident_type   = incident_type
        self._max_steps       = max_steps
        self.render_mode      = render_mode

        # Spaces
        self.observation_space = spaces.Box(
            low=0.0, high=1.0,
            shape=(OBS_DIM,),
            dtype=np.float32,
        )
        self.action_space = spaces.Discrete(NUM_ACTIONS)

        # Episode state (initialised in reset())
        self._nodes:        list[dict] = []
        self._step:         int        = 0
        self._done:         bool       = False
        self._prev_action:  int        = 0
        self._prev_success: bool       = True
        self._actions_taken: int       = 0
        self._escalated:    bool       = False

        # Action effect log (for Ghost Preview)
        self.action_log: list[dict] = []

    # ── Gymnasium API ──────────────────────────────────────────────────────────

    def reset(
        self,
        *,
        seed:    Optional[int] = None,
        options: Optional[dict] = None,
    ) -> tuple[np.ndarray, dict]:
        super().reset(seed=seed)

        self._step          = 0
        self._done          = False
        self._prev_action   = 0
        self._prev_success  = True
        self._actions_taken = 0
        self._escalated     = False
        self.action_log     = []

        if self._base_topology:
            self._nodes = self._load_from_topology(self._base_topology)
        else:
            self._nodes = self._generate_incident_nodes(self._incident_type)

        obs  = self._get_obs()
        info = self._get_info()
        return obs, info

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict]:
        assert not self._done, "Cannot step after episode is done. Call reset()."

        self._step          += 1
        self._prev_action    = action
        action_name          = ACTION_NAMES[int(action)]
        success              = True

        # ── Apply action effect to node states ────────────────────────────────
        success = self._apply_action(int(action))
        self._prev_success  = success
        if success:
            self._actions_taken += 1

        # Log action
        self.action_log.append({
            "step":    self._step,
            "action":  int(action),
            "name":    action_name,
            "success": success,
        })

        # ── Compute reward ────────────────────────────────────────────────────
        from rl.reward import compute_reward
        reward = compute_reward(
            nodes          = self._nodes,
            action         = int(action),
            action_success = success,
            step           = self._step,
            max_steps      = self._max_steps,
            incident_type  = self._incident_type,
        )

        # ── Termination conditions ────────────────────────────────────────────
        terminated = False
        truncated  = False

        if action == 14:                             # escalate_to_human
            self._escalated = True
            terminated = True

        elif self._all_healthy():
            terminated = True

        elif self._step >= self._max_steps:
            truncated = True

        self._done = terminated or truncated

        obs  = self._get_obs()
        info = self._get_info()
        info["action_name"]  = action_name
        info["action_success"] = success

        return obs, float(reward), terminated, truncated, info

    def render(self) -> Optional[str]:
        lines = [f"\n Step {self._step}/{self._max_steps}  incident={self._incident_type}"]
        lines.append("  Node               Status     Class      CPU   MEM")
        lines.append("  ─────────────────────────────────────────────────")
        for n in self._nodes:
            lines.append(
                f"  {n['id']:18s}  {n['status']:9s}  {n['class']:8s}"
                f"  {n['cpu']*100:4.0f}%  {n['mem']*100:4.0f}%"
            )
        result = "\n".join(lines)
        if self.render_mode == "human":
            print(result)
        return result

    def close(self) -> None:
        pass

    # ── Observation + info ────────────────────────────────────────────────────

    def _get_obs(self) -> np.ndarray:
        obs = np.zeros(OBS_DIM, dtype=np.float32)

        for i, n in enumerate(self._nodes[:MAX_NODES]):
            base = i * NODE_FEAT_DIM
            obs[base + 0] = float(n["cpu"])
            obs[base + 1] = float(n["mem"])
            obs[base + 2] = STATUS_ENC.get(n["status"], 0.5)
            obs[base + 3] = CLASS_ENC.get(n["class"],   0.5)

        # Global features
        g = MAX_NODES * NODE_FEAT_DIM
        n_nodes    = len(self._nodes)
        n_fault    = sum(1 for n in self._nodes if n["class"] == "fault")
        n_attack   = sum(1 for n in self._nodes if n["class"] == "attack")
        n_critical = sum(1 for n in self._nodes if n["status"] == "critical")
        inc_sev    = min(n_critical / max(n_nodes, 1), 1.0)

        obs[g + 0] = inc_sev
        obs[g + 1] = min(n_critical / max(n_nodes, 1), 1.0)    # blast radius norm
        obs[g + 2] = min(n_nodes / MAX_NODES, 1.0)
        obs[g + 3] = min(n_fault  / max(n_nodes, 1), 1.0)
        obs[g + 4] = min(n_attack / max(n_nodes, 1), 1.0)
        obs[g + 5] = self._step / self._max_steps
        obs[g + 6] = min(self._actions_taken / 20.0, 1.0)
        obs[g + 7] = 1.0 if self._prev_success else 0.0

        return obs

    def _get_info(self) -> dict:
        return {
            "step":          self._step,
            "incident_type": self._incident_type,
            "n_healthy":     sum(1 for n in self._nodes if n["status"] == "healthy"),
            "n_warning":     sum(1 for n in self._nodes if n["status"] == "warning"),
            "n_critical":    sum(1 for n in self._nodes if n["status"] == "critical"),
            "escalated":     self._escalated,
            "action_log":    list(self.action_log),
        }

    def _all_healthy(self) -> bool:
        return all(n["status"] == "healthy" for n in self._nodes)

    # ── Action effects ────────────────────────────────────────────────────────
    # Each action has a probabilistic effect on node states.
    # In production these are Ghost-Previewed before execution.

    def _apply_action(self, action: int) -> bool:
        """
        Apply action to node states. Returns True if the action had effect.
        Uses stochastic transitions so the agent learns robust policies.
        """
        rng = self.np_random
        nodes = self._nodes

        # Find root cause node (highest severity + incident class)
        incident_nodes = [n for n in nodes if n["class"] in ("fault", "attack")]
        root_cause     = incident_nodes[0] if incident_nodes else (nodes[0] if nodes else None)

        if action == 0:   # no_op
            # State degrades slightly if no action
            for n in nodes:
                if n["status"] == "warning":
                    if rng.random() < 0.1:
                        n["status"] = "critical"
            return True

        elif action == 1:  # isolate_container
            if root_cause:
                root_cause["status"] = "warning"
                root_cause["cpu"]    = max(root_cause["cpu"] - 0.3, 0.1)
                # Downstream nodes improve
                for n in nodes:
                    if n["id"] != root_cause["id"] and n["status"] == "critical":
                        n["status"] = "warning"
                return True
            return False

        elif action == 2:  # rollback_deployment
            if root_cause and root_cause["class"] == "fault":
                root_cause["status"] = "warning"
                root_cause["cpu"]    = max(root_cause["cpu"] - 0.4, 0.2)
                root_cause["mem"]    = max(root_cause["mem"] - 0.3, 0.3)
                if rng.random() < 0.7:
                    root_cause["class"] = "healthy"
                return True
            return False

        elif action == 3:  # scale_down_replicas
            # Reduce load pressure on critical nodes
            for n in nodes:
                if n["status"] in ("critical", "warning"):
                    n["cpu"] = max(n["cpu"] - 0.15, 0.05)
            return True

        elif action == 4:  # scale_up_replicas
            # Distribute load — reduces CPU on impacted nodes
            for n in nodes:
                if n["status"] == "critical":
                    n["cpu"] = max(n["cpu"] - 0.25, 0.1)
                    if n["cpu"] < 0.7:
                        n["status"] = "warning"
            return True

        elif action == 5:  # restart_pod
            if root_cause:
                if root_cause["class"] == "fault":
                    root_cause["cpu"]    = 0.3
                    root_cause["mem"]    = 0.4
                    root_cause["status"] = "healthy" if rng.random() < 0.6 else "warning"
                    root_cause["class"]  = "healthy"
                    return True
            return False

        elif action == 6:  # cordon_node
            # Prevents new pods — doesn't fix existing; reduces blast radius
            for n in nodes:
                if n["status"] == "critical":
                    n["status"] = "warning"
            return True

        elif action == 7:  # drain_node
            # Strong action: moves workloads off — slow but effective
            for n in nodes:
                if n["class"] in ("fault", "attack"):
                    n["cpu"]    = max(n["cpu"] - 0.5, 0.05)
                    n["mem"]    = max(n["mem"] - 0.4, 0.1)
                    n["status"] = "warning"
            return True

        elif action == 8:  # apply_network_policy
            if root_cause and root_cause["class"] == "attack":
                root_cause["cpu"]    = max(root_cause["cpu"] - 0.2, 0.1)
                root_cause["status"] = "warning"
                if rng.random() < 0.5:
                    root_cause["class"] = "fault"  # contained; not clean yet
                return True
            return False

        elif action == 9:  # rotate_secrets
            # Only useful for attack scenarios
            if self._incident_type == "attack":
                for n in nodes:
                    if n["class"] == "attack":
                        n["class"]  = "fault"   # attacker loses cred access
                        n["status"] = "warning"
                return True
            return False

        elif action == 10:  # kill_process
            if root_cause and root_cause["class"] == "attack":
                root_cause["cpu"]    = max(root_cause["cpu"] - 0.6, 0.05)
                root_cause["status"] = "warning"
                root_cause["class"]  = "fault"
                return True
            return False

        elif action == 11:  # increase_oom_threshold
            for n in nodes:
                if n["mem"] > 0.8:
                    n["mem"]    = max(n["mem"] - 0.3, 0.4)
                    n["status"] = "warning" if n["status"] == "critical" else n["status"]
            return True

        elif action == 12:  # throttle_cpu
            for n in nodes:
                if n["cpu"] > 0.7:
                    n["cpu"] = max(n["cpu"] - 0.3, 0.2)
            return True

        elif action == 13:  # enable_debug_logging
            # Forensic collection — no state change, always succeeds
            return True

        elif action == 14:  # escalate_to_human
            return True

        return False

    # ── Topology loading ──────────────────────────────────────────────────────

    def _load_from_topology(self, topo: dict) -> list[dict]:
        """Load node states from a Layer-2 topology dict."""
        nodes = []
        for n in topo.get("nodes", [])[:MAX_NODES]:
            nodes.append(_make_node(
                node_id    = n.get("id", "unknown"),
                cpu        = float(n.get("cpu", 30)) / 100.0,
                mem        = float(n.get("mem", 40)) / 100.0,
                status     = n.get("status", "healthy"),
                node_class = n.get("class", "healthy"),
            ))
        return nodes

    def _generate_incident_nodes(self, incident_type: str) -> list[dict]:
        """Generate a synthetic cluster incident for training."""
        rng = self.np_random

        if incident_type == "fault":
            return [
                _make_node("api-gw",       0.52, 0.61, "warning",  "healthy"),
                _make_node("order-svc",    0.94, 0.87, "critical", "fault"),
                _make_node("payment-svc",  0.67, 0.71, "warning",  "healthy"),
                _make_node("postgres",     0.91, 0.89, "critical", "fault"),
                _make_node("notify-svc",   0.73, 0.62, "warning",  "fault"),
                _make_node("redis",        0.18, 0.45, "healthy",  "healthy"),
            ]
        else:  # attack
            return [
                _make_node("api-gw",       0.52, 0.61, "warning",  "healthy"),
                _make_node("order-svc",    0.94, 0.87, "critical", "attack"),
                _make_node("payment-svc",  0.31, 0.44, "healthy",  "healthy"),
                _make_node("postgres",     0.91, 0.89, "critical", "fault"),
                _make_node("notify-svc",   0.73, 0.62, "warning",  "fault"),
                _make_node("redis",        0.18, 0.45, "healthy",  "healthy"),
            ]
