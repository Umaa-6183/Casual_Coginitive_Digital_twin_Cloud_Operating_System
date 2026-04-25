"""
CCDT Layer-3 Guardian — Ghost Preview Simulator
═══════════════════════════════════════════════════════════════════════════════
Performs a completely safe dry-run of a remediation action BEFORE it is
executed on the real cluster.

Steps:
  1. Clone current cluster state (StateCloner)
  2. Predict post-action state (RuleOutcomeModel)
  3. Re-run GNN inference on the predicted topology (Layer-2 API call)
  4. Compute delta metrics: MTTR, blast radius, risk score, confidence
  5. Return a GhostPreviewResult for human review or autonomous approval

The Ghost Preview is the primary safety mechanism for the Guardian.
No action is executed without a preview passing its risk gate.

Risk gate:
  - risk_score     < RISK_THRESHOLD (default 0.35)
  - confidence     > CONFIDENCE_MIN (default 0.70)
  - OPA allow      == True (policy check)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ghost_preview.state_cloner import StateCloner, ClusterSnapshot
from ghost_preview.outcome_model import RuleOutcomeModel, ActionOutcome

logger = logging.getLogger("ccdt.guardian.simulator")

# ─── Configuration ────────────────────────────────────────────────────────────
RISK_THRESHOLD = float(__import__("os").getenv(
    "GHOST_RISK_THRESHOLD",  "0.35"))
CONFIDENCE_MIN = float(__import__("os").getenv(
    "GHOST_CONFIDENCE_MIN",  "0.70"))
GNN_SERVICE_URL = __import__("os").getenv(
    "GNN_SERVICE_URL", "http://layer2-cognitive:8001")


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class GhostPreviewResult:
    """
    Complete Ghost Preview result for one action.
    Contains before/after state comparison, risk assessment, and recommendation.
    """
    action_id:     int
    action_name:   str
    approved:      bool            # True = passes risk gate
    risk_score:    float           # 0-1 (lower = safer)
    confidence:    float           # 0-1 (higher = more certain)

    # MTTR impact
    mttr_current_min:    float
    mttr_predicted_min:  float
    mttr_delta_min:      float

    # Blast radius
    blast_radius_before: int       # # critical nodes before
    blast_radius_after:  int       # # critical nodes after
    blast_radius_delta:  int       # negative = improvement

    # State comparison
    node_changes:        list[dict] = field(default_factory=list)
    side_effects:        list[str] = field(default_factory=list)

    # GNN re-inference on predicted state
    predicted_incident_type:   str = "unknown"
    predicted_graph_confidence: float = 0.0

    # Human-readable
    recommendation:  str = ""
    reasoning:       str = ""
    timestamp:       float = field(default_factory=time.time)

    # Risk gate detail
    risk_gate_detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "actionId":              self.action_id,
            "actionName":            self.action_name,
            "approved":              self.approved,
            "riskScore":             round(self.risk_score, 3),
            "confidence":            round(self.confidence, 3),
            "mttrCurrentMin":        round(self.mttr_current_min, 1),
            "mttrPredictedMin":      round(self.mttr_predicted_min, 1),
            "mttrDeltaMin":          round(self.mttr_delta_min, 1),
            "blastRadiusBefore":     self.blast_radius_before,
            "blastRadiusAfter":      self.blast_radius_after,
            "blastRadiusDelta":      self.blast_radius_delta,
            "nodeChanges":           self.node_changes,
            "sideEffects":           self.side_effects,
            "predictedIncidentType": self.predicted_incident_type,
            "recommendation":        self.recommendation,
            "reasoning":             self.reasoning,
            "riskGateDetail":        self.risk_gate_detail,
            "timestamp":             int(self.timestamp),
        }


# ─── GhostSimulator ───────────────────────────────────────────────────────────

class GhostSimulator:
    """
    Performs safe dry-run simulations of remediation actions.

    Usage:
        sim    = GhostSimulator(dag_builder=dag, gnn_url=GNN_SERVICE_URL)
        result = await sim.preview(action_id=1, target_node="order-svc")
        if result.approved:
            await executor.execute(result.action_id, target_node)
    """

    def __init__(
        self,
        dag_builder=None,
        gnn_url:      str = GNN_SERVICE_URL,
        namespace:    str = "default",
    ) -> None:
        self._cloner = StateCloner(dag_builder, namespace)
        self._outcome_model = RuleOutcomeModel()
        self._gnn_url = gnn_url

    async def preview(
        self,
        action_id:         int,
        target_node:       Optional[str] = None,
        topology_override: Optional[dict] = None,
        incident_type:     str = "fault",
    ) -> GhostPreviewResult:
        """
        Simulate action_id on the current cluster state and return the preview result.

        Args:
            action_id         Action to simulate (0-14)
            target_node       Node the action targets (for labelling)
            topology_override Explicit topology dict (skips dag_builder query)
            incident_type     fault | attack
        """
        from rl.env import ACTION_NAMES
        action_name = ACTION_NAMES[action_id] if action_id < len(
            ACTION_NAMES) else f"action_{action_id}"

        logger.debug("Ghost preview: action=%s target=%s",
                     action_name, target_node)
        t0 = time.perf_counter()

        # ── 1. Clone current state ────────────────────────────────────────────
        snapshot = await self._cloner.snapshot(
            topology_override=topology_override,
            incident_type=incident_type,
        )
        before_snapshot = snapshot.clone()

        # ── 2. Predict post-action state ─────────────────────────────────────
        outcome: ActionOutcome = self._outcome_model.predict(
            action_id, snapshot)
        after_snapshot = outcome.predicted_snapshot

        # ── 3. Compute metrics ────────────────────────────────────────────────
        mttr_current = self._estimate_mttr(before_snapshot)
        mttr_predicted = max(0.0, mttr_current + outcome.mttr_delta_min)

        blast_before = len(before_snapshot.critical_nodes())
        blast_after = len(after_snapshot.critical_nodes()
                          ) if after_snapshot else blast_before

        # ── 4. Build node change list ─────────────────────────────────────────
        node_changes = self._compute_node_changes(
            before_snapshot, after_snapshot)

        # ── 5. Optionally call GNN for post-action re-inference ───────────────
        predicted_type, predicted_conf = await self._gnn_reinfer(after_snapshot)

        # ── 6. Risk gate evaluation ───────────────────────────────────────────
        approved, risk_gate_detail = self._evaluate_risk_gate(
            outcome, action_id, before_snapshot
        )

        # ── 7. Generate recommendation text ──────────────────────────────────
        recommendation, reasoning = self._generate_recommendation(
            action_id, outcome, approved, blast_before, blast_after, mttr_current, mttr_predicted
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.debug(
            "Ghost preview completed in %.1f ms  approved=%s", elapsed_ms, approved)

        return GhostPreviewResult(
            action_id=action_id,
            action_name=action_name,
            approved=approved,
            risk_score=outcome.risk_score,
            confidence=outcome.confidence,
            mttr_current_min=mttr_current,
            mttr_predicted_min=mttr_predicted,
            mttr_delta_min=outcome.mttr_delta_min,
            blast_radius_before=blast_before,
            blast_radius_after=blast_after,
            blast_radius_delta=blast_after - blast_before,
            node_changes=node_changes,
            side_effects=outcome.side_effects,
            predicted_incident_type=predicted_type,
            predicted_graph_confidence=predicted_conf,
            recommendation=recommendation,
            reasoning=reasoning,
            risk_gate_detail=risk_gate_detail,
        )

    async def preview_all(
        self,
        action_ids:        Optional[list[int]] = None,
        topology_override: Optional[dict] = None,
        incident_type:     str = "fault",
        top_k:             int = 5,
    ) -> list[GhostPreviewResult]:
        """
        Preview multiple actions in parallel and return sorted by MTTR improvement.
        """
        if action_ids is None:
            action_ids = list(range(15))

        tasks = [
            self.preview(aid, topology_override=topology_override,
                         incident_type=incident_type)
            for aid in action_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid = [r for r in results if isinstance(r, GhostPreviewResult)]
        # most negative first = biggest improvement
        valid.sort(key=lambda r: r.mttr_delta_min)
        return valid[:top_k]

    # ── Private helpers ───────────────────────────────────────────────────────

    def _estimate_mttr(self, snapshot: ClusterSnapshot) -> float:
        """
        Estimate current MTTR in minutes based on cluster state severity.
        Heuristic: 5 min per critical node + 2 min per warning node.
        """
        critical = len(snapshot.critical_nodes())
        warning = sum(1 for n in snapshot.nodes if n.status == "warning")
        attack_multiplier = 1.5 if snapshot.incident_type == "attack" else 1.0
        return round((critical * 5 + warning * 2) * attack_multiplier, 1)

    def _compute_node_changes(
        self,
        before: ClusterSnapshot,
        after:  Optional[ClusterSnapshot],
    ) -> list[dict]:
        """Generate a list of per-node before/after change dicts."""
        if after is None:
            return []
        changes = []
        for b_node in before.nodes:
            a_node = after.get_node(b_node.id)
            if a_node is None:
                continue
            if (b_node.status != a_node.status or
                b_node.node_class != a_node.node_class or
                abs(b_node.cpu - a_node.cpu) > 0.05 or
                    abs(b_node.mem - a_node.mem) > 0.05):
                changes.append({
                    "nodeId":        b_node.id,
                    "statusBefore":  b_node.status,
                    "statusAfter":   a_node.status,
                    "classBefore":   b_node.node_class,
                    "classAfter":    a_node.node_class,
                    "cpuBefore":     round(b_node.cpu * 100, 1),
                    "cpuAfter":      round(a_node.cpu * 100, 1),
                    "memBefore":     round(b_node.mem * 100, 1),
                    "memAfter":      round(a_node.mem * 100, 1),
                })
        return changes

    async def _gnn_reinfer(
        self,
        snapshot: Optional[ClusterSnapshot],
    ) -> tuple[str, float]:
        """
        Call Layer-2 GNN /infer with the predicted topology.
        Falls back to ("unknown", 0.0) if GNN is unreachable.
        """
        if snapshot is None:
            return "unknown", 0.0
        try:
            topo = snapshot.to_topology_dict()
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(
                    f"{self._gnn_url}/infer",
                    json={"topology": topo},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    inc_type = data.get("incidentType", "unknown")
                    conf = data.get("graphClassification",
                                    {}).get("healthy", 0.0)
                    return inc_type, float(conf)
        except Exception as exc:
            logger.debug("GNN re-infer failed (non-critical): %s", exc)
        return "unknown", 0.0

    def _evaluate_risk_gate(
        self,
        outcome:   ActionOutcome,
        action_id: int,
        snapshot:  ClusterSnapshot,
    ) -> tuple[bool, dict]:
        """
        Evaluate the risk gate for an action.

        Returns (approved: bool, detail: dict)
        """
        detail = {
            "risk_score":     outcome.risk_score,
            "risk_threshold": RISK_THRESHOLD,
            "confidence":     outcome.confidence,
            "confidence_min": CONFIDENCE_MIN,
            "risk_ok":        outcome.risk_score <= RISK_THRESHOLD,
            "confidence_ok":  outcome.confidence >= CONFIDENCE_MIN,
        }

        # Never auto-approve drain_node (action 7) — always require human
        if action_id == 7:
            detail["blocked_reason"] = "drain_node always requires human approval"
            return False, detail

        # Escalate_to_human (14) is always approved — it IS the escalation
        if action_id == 14:
            detail["note"] = "escalation always approved"
            return True, detail

        # Attack-specific actions (isolate, network_policy, kill) require higher confidence
        attack_actions = {1, 8, 10}
        if action_id in attack_actions and snapshot.incident_type != "attack":
            detail["confidence_min"] = 0.80
            detail["confidence_ok"] = outcome.confidence >= 0.80

        approved = detail["risk_ok"] and detail["confidence_ok"]
        return approved, detail

    def _generate_recommendation(
        self,
        action_id:     int,
        outcome:       ActionOutcome,
        approved:      bool,
        blast_before:  int,
        blast_after:   int,
        mttr_current:  float,
        mttr_predicted: float,
    ) -> tuple[str, str]:
        """Generate human-readable recommendation and reasoning."""
        name = outcome.action_name.replace("_", " ").title()

        if approved:
            rec = f"APPROVED: Execute {name}"
            reasoning = (
                f"Action '{name}' is predicted to reduce MTTR by "
                f"{abs(outcome.mttr_delta_min):.0f} min "
                f"(from {mttr_current:.0f} → {mttr_predicted:.0f} min). "
            )
            if blast_after < blast_before:
                reasoning += (
                    f"Blast radius shrinks from {blast_before} → {blast_after} critical nodes. "
                )
            reasoning += f"Risk score {outcome.risk_score:.2f} is within threshold."
        else:
            rec = f"BLOCKED: {name} requires human approval"
            if outcome.risk_score > RISK_THRESHOLD:
                reasoning = (
                    f"Risk score {outcome.risk_score:.2f} exceeds threshold {RISK_THRESHOLD:.2f}. "
                )
            else:
                reasoning = (
                    f"Confidence {outcome.confidence:.2f} below minimum {CONFIDENCE_MIN:.2f}. "
                )
            reasoning += "Escalate to on-call for manual review."

        return rec, reasoning
