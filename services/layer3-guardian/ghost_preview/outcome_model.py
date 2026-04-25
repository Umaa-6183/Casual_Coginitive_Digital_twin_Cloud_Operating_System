"""
CCDT Layer-3 Guardian — Action Outcome Model
═══════════════════════════════════════════════════════════════════════════════
Predicts the expected outcome of each remediation action on a given cluster
state without actually executing it.

Two prediction strategies:
  1. RuleOutcomeModel   Deterministic rule table keyed by (action, node_class,
                        status). Fast, interpretable, requires no training.
                        Used in production as the primary model.

  2. StatisticalOutcomeModel  (future)  Trained on historical action outcomes
                        from executed remediations. Learns complex interactions.

Outcome structure:
  node_deltas   Per-node state changes predicted by the action
  mttr_delta    Expected change in time-to-recover (minutes)
  risk_score    Risk of the action causing additional harm (0-1)
  side_effects  Human-readable list of potential side effects
  confidence    Model confidence in this prediction (0-1)
"""
from __future__ import annotations

import logging
from copy import deepcopy
from typing import Optional

from ghost_preview.state_cloner import ClusterSnapshot, NodeSnapshot

logger = logging.getLogger("ccdt.guardian.outcome_model")


# ─── Outcome data class ───────────────────────────────────────────────────────

class ActionOutcome:
    """Predicted outcome of applying an action to the cluster."""

    __slots__ = (
        "action_id", "action_name",
        "node_deltas", "mttr_delta_min",
        "risk_score", "side_effects",
        "confidence", "predicted_snapshot",
    )

    def __init__(
        self,
        action_id:   int,
        action_name: str,
        node_deltas: dict[str, dict],
        mttr_delta_min:  float,
        risk_score:  float,
        side_effects: list[str],
        confidence:  float,
        predicted_snapshot: Optional[ClusterSnapshot] = None,
    ) -> None:
        self.action_id          = action_id
        self.action_name        = action_name
        self.node_deltas        = node_deltas
        self.mttr_delta_min     = mttr_delta_min
        self.risk_score         = risk_score
        self.side_effects       = side_effects
        self.confidence         = confidence
        self.predicted_snapshot = predicted_snapshot

    def to_dict(self) -> dict:
        return {
            "action_id":       self.action_id,
            "action_name":     self.action_name,
            "node_deltas":     self.node_deltas,
            "mttr_delta_min":  round(self.mttr_delta_min, 1),
            "risk_score":      round(self.risk_score, 3),
            "side_effects":    self.side_effects,
            "confidence":      round(self.confidence, 3),
        }


# ─── Rule-based outcome model ─────────────────────────────────────────────────

# Each entry: (action_id) → rule dict
# Rule fields:
#   applies_to   set of node classes that benefit from this action
#   cpu_delta    CPU reduction fraction
#   mem_delta    MEM reduction fraction
#   status_map   {current_status → predicted_status}
#   class_map    {current_class  → predicted_class}
#   mttr_delta   minutes saved (negative = faster recovery)
#   risk_score   base risk
#   side_effects text list

_ACTION_RULES: dict[int, dict] = {
    0: {  # no_op
        "applies_to": set(),
        "cpu_delta":  0.0, "mem_delta": 0.0,
        "status_map": {}, "class_map": {},
        "mttr_delta": 5.0,    # doing nothing costs time
        "risk_score": 0.0,
        "side_effects": [],
    },
    1: {  # isolate_container
        "applies_to": {"fault", "attack"},
        "cpu_delta":  -0.30, "mem_delta": -0.10,
        "status_map": {"critical": "warning", "warning": "warning"},
        "class_map":  {},
        "mttr_delta": -8.0,
        "risk_score": 0.25,
        "side_effects": ["Container isolated — dependent services may fail",
                         "Network traffic dropped for isolated pod"],
    },
    2: {  # rollback_deployment
        "applies_to": {"fault"},
        "cpu_delta":  -0.40, "mem_delta": -0.30,
        "status_map": {"critical": "warning", "warning": "healthy"},
        "class_map":  {"fault": "healthy"},
        "mttr_delta": -15.0,
        "risk_score": 0.15,
        "side_effects": ["Previous image deployed — may introduce older bugs",
                         "Rolling restart will cause brief latency spike (~30s)"],
    },
    3: {  # scale_down_replicas
        "applies_to": {"fault", "attack", "healthy"},
        "cpu_delta":  -0.15, "mem_delta": -0.05,
        "status_map": {"critical": "warning"},
        "class_map":  {},
        "mttr_delta": -3.0,
        "risk_score": 0.10,
        "side_effects": ["Reduced capacity — if load is high, may worsen latency"],
    },
    4: {  # scale_up_replicas
        "applies_to": {"fault"},
        "cpu_delta":  -0.25, "mem_delta": -0.05,
        "status_map": {"critical": "warning", "warning": "healthy"},
        "class_map":  {},
        "mttr_delta": -10.0,
        "risk_score": 0.08,
        "side_effects": ["Increased pod count — higher resource consumption",
                         "New pods may take 30-90s to become ready"],
    },
    5: {  # restart_pod
        "applies_to": {"fault"},
        "cpu_delta":  -0.60, "mem_delta": -0.50,
        "status_map": {"critical": "healthy", "warning": "healthy"},
        "class_map":  {"fault": "healthy"},
        "mttr_delta": -12.0,
        "risk_score": 0.20,
        "side_effects": ["Brief downtime during pod restart (~5-15s)",
                         "In-flight requests will fail during restart"],
    },
    6: {  # cordon_node
        "applies_to": {"fault", "attack"},
        "cpu_delta":  -0.05, "mem_delta": -0.02,
        "status_map": {"critical": "warning"},
        "class_map":  {},
        "mttr_delta": -2.0,
        "risk_score": 0.12,
        "side_effects": ["No new pods will be scheduled on this node",
                         "Existing pods continue running — not evicted"],
    },
    7: {  # drain_node
        "applies_to": {"fault", "attack"},
        "cpu_delta":  -0.80, "mem_delta": -0.70,
        "status_map": {"critical": "warning", "warning": "healthy"},
        "class_map":  {"fault": "healthy", "attack": "fault"},
        "mttr_delta": -18.0,
        "risk_score": 0.40,
        "side_effects": ["All pods evicted — significant disruption",
                         "Stateful workloads may lose data if not using PVCs",
                         "Takes 2-5 minutes for all pods to migrate"],
    },
    8: {  # apply_network_policy
        "applies_to": {"attack"},
        "cpu_delta":  -0.20, "mem_delta": 0.0,
        "status_map": {"critical": "warning"},
        "class_map":  {"attack": "fault"},
        "mttr_delta": -10.0,
        "risk_score": 0.18,
        "side_effects": ["Ingress to namespace blocked — legitimate traffic affected",
                         "Network policy may conflict with existing policies"],
    },
    9: {  # rotate_secrets
        "applies_to": {"attack"},
        "cpu_delta":  0.0, "mem_delta": 0.0,
        "status_map": {},
        "class_map":  {"attack": "fault"},
        "mttr_delta": -5.0,
        "risk_score": 0.10,
        "side_effects": ["All services using rotated secrets will restart",
                         "May cause brief auth failures during rotation"],
    },
    10: {  # kill_process
        "applies_to": {"attack"},
        "cpu_delta":  -0.70, "mem_delta": -0.30,
        "status_map": {"critical": "warning"},
        "class_map":  {"attack": "fault"},
        "mttr_delta": -8.0,
        "risk_score": 0.30,
        "side_effects": ["Process killed — pod may restart if managed by K8s",
                         "Forensic data may be lost after process termination"],
    },
    11: {  # increase_oom_threshold
        "applies_to": {"fault"},
        "cpu_delta":  0.0, "mem_delta": -0.30,
        "status_map": {"critical": "warning", "warning": "healthy"},
        "class_map":  {},
        "mttr_delta": -6.0,
        "risk_score": 0.05,
        "side_effects": ["Higher memory limit — potential impact on other pods on same node"],
    },
    12: {  # throttle_cpu
        "applies_to": {"fault", "attack"},
        "cpu_delta":  -0.35, "mem_delta": 0.0,
        "status_map": {"critical": "warning"},
        "class_map":  {},
        "mttr_delta": -4.0,
        "risk_score": 0.12,
        "side_effects": ["CPU throttling will increase request latency",
                         "SLA may be breached during throttling period"],
    },
    13: {  # enable_debug_logging
        "applies_to": set(),
        "cpu_delta":  0.05, "mem_delta": 0.03,   # slight overhead
        "status_map": {},
        "class_map":  {},
        "mttr_delta": -2.0,   # helps diagnosis
        "risk_score": 0.02,
        "side_effects": ["Increased log volume — may fill disk if not rotated",
                         "Slight CPU overhead from additional logging"],
    },
    14: {  # escalate_to_human
        "applies_to": set(),
        "cpu_delta":  0.0, "mem_delta": 0.0,
        "status_map": {},
        "class_map":  {},
        "mttr_delta": 15.0,   # human response time adds delay
        "risk_score": 0.0,
        "side_effects": ["On-call engineer paged — median response time ~15 min"],
    },
}


class RuleOutcomeModel:
    """
    Predicts action outcomes using a deterministic rule table.
    No training required. O(1) prediction time.
    """

    def predict(
        self,
        action_id: int,
        snapshot:  ClusterSnapshot,
    ) -> ActionOutcome:
        """
        Predict the outcome of applying action_id to the cluster snapshot.
        Returns an ActionOutcome with the predicted post-action state.
        """
        from rl.env import ACTION_NAMES
        action_name = ACTION_NAMES[action_id] if action_id < len(ACTION_NAMES) else f"action_{action_id}"
        rule = _ACTION_RULES.get(action_id, _ACTION_RULES[0])

        # Apply rules to each node
        predicted = snapshot.clone()
        node_deltas: dict[str, dict] = {}
        total_mttr_delta = rule["mttr_delta"]

        for node in predicted.nodes:
            delta = {"cpu_before": node.cpu, "mem_before": node.mem,
                     "status_before": node.status, "class_before": node.node_class}

            if node.node_class in rule["applies_to"] or not rule["applies_to"]:
                # Apply CPU/MEM deltas
                node.cpu = max(0.05, min(1.0, node.cpu + rule["cpu_delta"]))
                node.mem = max(0.05, min(1.0, node.mem + rule["mem_delta"]))

                # Apply status transition
                new_status = rule["status_map"].get(node.status, node.status)
                node.status = new_status

                # Apply class transition
                new_class = rule["class_map"].get(node.node_class, node.node_class)
                node.node_class = new_class

            delta["cpu_after"]    = node.cpu
            delta["mem_after"]    = node.mem
            delta["status_after"] = node.status
            delta["class_after"]  = node.node_class
            node_deltas[node.id]  = delta

        # Cascade effects on downstream nodes via causal edges
        for inc_node in snapshot.incident_nodes():
            for edge in snapshot.get_edges_from(inc_node.id):
                target = predicted.get_node(edge.dst)
                if target and target.status == "critical" and target.node_class == "fault":
                    # Fixing the upstream helps the downstream
                    if rule["class_map"].get("fault") == "healthy":
                        target.status     = "warning"
                        target.node_class = "fault"  # still recovering

        # Estimate confidence based on whether incident type matches action
        confidence = 0.85
        if action_id in {8, 9, 10} and snapshot.incident_type != "attack":
            confidence = 0.40   # attack actions on fault incidents are less reliable
        elif action_id in {2, 5} and snapshot.incident_type == "attack":
            confidence = 0.50   # rollback/restart less effective for attacks

        return ActionOutcome(
            action_id          = action_id,
            action_name        = action_name,
            node_deltas        = node_deltas,
            mttr_delta_min     = total_mttr_delta,
            risk_score         = rule["risk_score"],
            side_effects       = list(rule["side_effects"]),
            confidence         = confidence,
            predicted_snapshot = predicted,
        )

    def predict_all(
        self,
        snapshot:  ClusterSnapshot,
        action_ids: Optional[list[int]] = None,
    ) -> list[ActionOutcome]:
        """
        Predict outcomes for multiple actions and return sorted by MTTR improvement.
        """
        if action_ids is None:
            action_ids = list(range(len(_ACTION_RULES)))

        outcomes = [self.predict(aid, snapshot) for aid in action_ids]
        outcomes.sort(key=lambda o: o.mttr_delta_min)   # most negative = best
        return outcomes
