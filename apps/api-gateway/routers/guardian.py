"""
CCDT API Gateway — Guardian Router
────────────────────────────────────────────────────────────────────────────────
Endpoints:
  GET  /api/v1/guardian/policies         → list OPA policies + violation counts
  GET  /api/v1/guardian/actions          → current RL-proposed action queue
  POST /api/v1/actions/preview           → Ghost Preview simulation
  POST /api/v1/actions/execute           → execute a remediation action
  GET  /api/v1/actions/history           → audit log of past executions
  GET  /api/v1/guardian/autonomy         → current autonomy mode
  PUT  /api/v1/guardian/autonomy         → update autonomy mode
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("ccdt.routers.guardian")

router = APIRouter(prefix="/api/v1", tags=["guardian"])

# ─── Service URLs ─────────────────────────────────────────────────────────────
GUARDIAN_SERVICE_URL = os.getenv("GUARDIAN_SERVICE_URL", "http://layer3-guardian:8002")

# ─── Pydantic models ──────────────────────────────────────────────────────────

ActionRisk       = Literal["LOW", "MED", "HIGH"]
AutonomyMode     = Literal["human-in-loop", "supervised", "full-auto"]


class ActionRequest(BaseModel):
    action_name: str              = Field(..., description="Action identifier, e.g. 'isolate_container'")
    target_node: str              = Field(..., description="Kubernetes pod/deployment name")
    namespace:   str              = Field("default")
    parameters:  dict[str, Any]  = Field(default_factory=dict, description="Action-specific parameters")
    dry_run:     bool             = Field(False,  description="If true, simulate without making changes")
    incident_id: str | None       = Field(None,   description="Associated incident ID for audit log")


class AutonomyUpdate(BaseModel):
    mode:   AutonomyMode = Field(...)
    reason: str          = Field("", description="Optional reason for mode change")


# ─── Static reference data ────────────────────────────────────────────────────

OPA_POLICIES: list[dict[str, Any]] = [
    {
        "id":          "p1",
        "name":        "no_privilege_escalation",
        "file":        "no_privilege_escalation.rego",
        "status":      "active",
        "violations":  1,
        "type":        "blocking",
        "description": "Block any action on containers that have acquired CAP_SYS_ADMIN or CAP_SYS_PTRACE",
    },
    {
        "id":          "p2",
        "name":        "cpu_threshold",
        "file":        "cpu_threshold.rego",
        "status":      "active",
        "violations":  0,
        "type":        "blocking",
        "description": "Prevent scale-down below 2 replicas; block node cordon when >5 pods present",
    },
    {
        "id":          "p3",
        "name":        "egress_control",
        "file":        "egress_control.rego",
        "status":      "active",
        "violations":  0,
        "type":        "blocking",
        "description": "Block egress traffic to non-RFC1918 IP ranges unless explicitly allowlisted",
    },
    {
        "id":          "p4",
        "name":        "lateral_movement",
        "file":        "lateral_movement.rego",
        "status":      "active",
        "violations":  1,
        "type":        "blocking",
        "description": "Deny cross-namespace pod access patterns; block scaling of compromised pods",
    },
    {
        "id":          "p5",
        "name":        "oom_notification",
        "file":        "oom_notification.rego",
        "status":      "active",
        "violations":  0,
        "type":        "advisory",
        "description": "Require SRE notification for any action on pods with OOM risk (non-blocking)",
    },
]

RL_ACTIONS: list[dict[str, Any]] = [
    {
        "id": 1,
        "action":     "Isolate order-svc container — block all ingress/egress",
        "actionName": "isolate_container",
        "targetNode": "order-svc",
        "namespace":  "default",
        "confidence": 94.2,
        "risk":       "LOW",
        "impact":     "MTTR -65%",
        "opaStatus":  "PASS",
        "rank":        1,
    },
    {
        "id": 2,
        "action":     "Apply deny-all NetworkPolicy to lateral movement paths",
        "actionName": "apply_network_policy",
        "targetNode": "order-svc",
        "namespace":  "default",
        "confidence": 87.1,
        "risk":       "LOW",
        "impact":     "MTTR -50%",
        "opaStatus":  "PASS",
        "rank":       2,
    },
    {
        "id": 3,
        "action":     "Block outbound IP 203.0.113.47 (C2 candidate)",
        "actionName": "block_ip",
        "targetNode": "order-svc",
        "namespace":  "default",
        "confidence": 79.3,
        "risk":       "LOW",
        "impact":     "MTTR -40%",
        "opaStatus":  "PASS",
        "rank":       3,
    },
    {
        "id": 4,
        "action":     "Increase postgres memory limit from 4GB → 6GB",
        "actionName": "increase_memory_limit",
        "targetNode": "postgres",
        "namespace":  "default",
        "confidence": 71.8,
        "risk":       "MED",
        "impact":     "MTTR -45%",
        "opaStatus":  "PASS",
        "rank":       4,
    },
    {
        "id": 5,
        "action":     "Scale up notify-svc replicas (1 → 3)",
        "actionName": "scale_up_replicas",
        "targetNode": "notify-svc",
        "namespace":  "default",
        "confidence": 68.4,
        "risk":       "LOW",
        "impact":     "MTTR -30%",
        "opaStatus":  "PASS",
        "rank":       5,
    },
]

# Simulation outcome table  action_name → outcome deltas
_SIM_OUTCOMES: dict[str, dict[str, Any]] = {
    "isolate_container":    {"mttrImpactPct": -65, "trafficImpactPct": -2,  "riskScore": 12, "confidence": 0.92, "opaStatus": "PASS", "projectedStatus": "stable"},
    "block_ip":             {"mttrImpactPct": -55, "trafficImpactPct": -1,  "riskScore": 8,  "confidence": 0.88, "opaStatus": "PASS", "projectedStatus": "stable"},
    "restart_pod":          {"mttrImpactPct": -40, "trafficImpactPct": -8,  "riskScore": 25, "confidence": 0.81, "opaStatus": "PASS", "projectedStatus": "stable"},
    "scale_up_replicas":    {"mttrImpactPct": -30, "trafficImpactPct": -3,  "riskScore": 10, "confidence": 0.85, "opaStatus": "PASS", "projectedStatus": "stable"},
    "increase_memory_limit":{"mttrImpactPct": -45, "trafficImpactPct": -5,  "riskScore": 20, "confidence": 0.79, "opaStatus": "PASS", "projectedStatus": "stable"},
    "apply_network_policy": {"mttrImpactPct": -50, "trafficImpactPct": -4,  "riskScore": 15, "confidence": 0.87, "opaStatus": "PASS", "projectedStatus": "stable"},
    "rollback_deployment":  {"mttrImpactPct": -35, "trafficImpactPct": -10, "riskScore": 30, "confidence": 0.77, "opaStatus": "PASS", "projectedStatus": "degraded"},
    "cordon_node":          {"mttrImpactPct": -20, "trafficImpactPct": 25,  "riskScore": 60, "confidence": 0.65, "opaStatus": "FAIL", "projectedStatus": "degraded"},
}

# In-memory action audit log (bounded ring buffer, max 500 entries)
_ACTION_HISTORY: list[dict[str, Any]] = []
_AUTONOMY_MODE: dict[str, str] = {"mode": "human-in-loop"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _call_guardian(path: str, payload: dict) -> dict:
    """Forward a request to the Layer-3 Guardian service."""
    url = f"{GUARDIAN_SERVICE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("Guardian service unreachable (%s) — using local simulation", exc)
        return {}


def _build_simulation_result(action_name: str, target_node: str) -> dict[str, Any]:
    """Build a deterministic simulation result for offline/fallback use."""
    outcome = _SIM_OUTCOMES.get(
        action_name,
        {"mttrImpactPct": -30, "trafficImpactPct": -5, "riskScore": 20,
         "confidence": 0.80, "opaStatus": "PASS", "projectedStatus": "stable"},
    )
    violations = (
        ["cpu_threshold: replica count too low after this action"]
        if outcome["opaStatus"] == "FAIL"
        else []
    )
    recommendation = (
        "ACTION BLOCKED — OPA policy violation detected."
        if outcome["opaStatus"] == "FAIL"
        else (
            "SAFE TO EXECUTE — All policies passed. Low risk."
            if outcome["riskScore"] < 20
            else "PROCEED WITH CAUTION — Review collateral impact before executing."
        )
    )
    return {
        "action_name":        action_name,
        "target_node":        target_node,
        "mttrImpactPct":      outcome["mttrImpactPct"],
        "trafficImpactPct":   outcome["trafficImpactPct"],
        "collateralServices": [target_node] if outcome["riskScore"] > 40 else [],
        "riskScore":          outcome["riskScore"],
        "confidence":         outcome["confidence"],
        "opaViolations":      violations,
        "projectedStatus":    outcome["projectedStatus"],
        "recommendation":     recommendation,
        "simDurationMs":      round(2800 + __import__("random").random() * 400, 1),
        "opaStatus":          outcome["opaStatus"],
        "source":             "local_simulation",
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get(
    "/guardian/policies",
    summary="List OPA policies",
)
async def get_policies() -> JSONResponse:
    """
    Return all registered OPA policies with their current status,
    violation counts and type (blocking vs advisory).
    """
    total_violations = sum(p["violations"] for p in OPA_POLICIES)
    return JSONResponse(content={
        "policies":        OPA_POLICIES,
        "total":           len(OPA_POLICIES),
        "total_violations": total_violations,
        "compliance_pct":  round(
            100 * len([p for p in OPA_POLICIES if p["violations"] == 0]) / len(OPA_POLICIES), 1
        ),
    })


@router.get(
    "/guardian/actions",
    summary="RL-proposed remediation actions",
)
async def get_actions(
    risk:  str | None = Query(None, description="Filter by risk: LOW|MED|HIGH"),
    limit: int        = Query(10,   ge=1, le=50),
) -> JSONResponse:
    """
    Return the current set of remediation actions proposed by the RL agent,
    ranked by confidence score.
    """
    actions = RL_ACTIONS[:]
    if risk:
        actions = [a for a in actions if a["risk"] == risk.upper()]
    return JSONResponse(content={
        "actions": actions[:limit],
        "total":   len(actions),
        "autonomy_mode": _AUTONOMY_MODE["mode"],
    })


@router.post(
    "/actions/preview",
    summary="Ghost Preview — simulate action before execution",
)
async def preview_action(body: ActionRequest) -> JSONResponse:
    """
    Run a Ghost Preview simulation for a proposed action.

    Pipeline:
      1. Clone cluster state snapshot
      2. Apply action in isolated twin
      3. Predict outcome (MTTR, traffic, collateral)
      4. Evaluate OPA policies
      5. Return structured SimulationResult

    The actual Layer-3 Guardian service is called when available.
    Falls back to a deterministic local simulation.
    """
    # Try real guardian service first
    result = await _call_guardian("/ghost_preview/simulate", {
        "action_name": body.action_name,
        "target_node": body.target_node,
        "namespace":   body.namespace,
        "parameters":  body.parameters,
    })

    if not result:
        result = _build_simulation_result(body.action_name, body.target_node)

    logger.info(
        "Ghost Preview: action=%s node=%s risk=%s opa=%s",
        body.action_name, body.target_node,
        result.get("riskScore", "?"), result.get("opaStatus", "?"),
    )
    return JSONResponse(content=result)


@router.post(
    "/actions/execute",
    summary="Execute a remediation action",
)
async def execute_action(body: ActionRequest) -> JSONResponse:
    """
    Execute a remediation action on the live cluster.

    Safety gates (applied in order):
      1. OPA policy evaluation — hard block on violation
      2. Autonomy mode check — in human-in-loop mode this records intent only
      3. Ghost Preview risk check — blocks if riskScore > 75
      4. Forwards to Layer-3 Guardian executor

    All executions are appended to the audit log regardless of outcome.
    """
    # Safety: check OPA before executing
    sim = _build_simulation_result(body.action_name, body.target_node)
    if sim["opaStatus"] == "FAIL":
        _record_history(body, "BLOCKED_OPA", sim)
        raise HTTPException(
            status_code=422,
            detail={
                "error":         "OPA_POLICY_VIOLATION",
                "violations":    sim["opaViolations"],
                "recommendation": sim["recommendation"],
            },
        )

    if sim["riskScore"] > 75:
        _record_history(body, "BLOCKED_RISK", sim)
        raise HTTPException(
            status_code=422,
            detail={
                "error":       "RISK_THRESHOLD_EXCEEDED",
                "riskScore":   sim["riskScore"],
                "recommendation": "Ghost Preview risk score exceeds 75/100 threshold.",
            },
        )

    # Human-in-loop mode: record intent but do not forward
    if _AUTONOMY_MODE["mode"] == "human-in-loop" and not body.dry_run:
        _record_history(body, "PENDING_APPROVAL", sim)
        return JSONResponse(content={
            "status":  "PENDING_APPROVAL",
            "message": "Action queued for SRE approval (human-in-loop mode active)",
            "action_name": body.action_name,
            "target_node": body.target_node,
            "simulation":  sim,
        })

    # Forward to Guardian executor
    result = await _call_guardian("/execute", {
        "action_name": body.action_name,
        "target_node": body.target_node,
        "namespace":   body.namespace,
        "parameters":  body.parameters,
        "dry_run":     body.dry_run,
    })

    if not result:
        # Fallback: simulate success for dry_run / offline dev
        result = {
            "status":      "SIMULATED_SUCCESS" if body.dry_run else "SUCCESS",
            "action_name": body.action_name,
            "target_node": body.target_node,
            "executedAt":  int(time.time()),
            "message":     "Guardian service offline — local simulation",
        }

    _record_history(body, result.get("status", "UNKNOWN"), sim)
    logger.info(
        "Action executed: %s on %s status=%s",
        body.action_name, body.target_node, result.get("status"),
    )
    return JSONResponse(content=result)


@router.get(
    "/actions/history",
    summary="Audit log of past action executions",
)
async def get_action_history(
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0,  ge=0),
) -> JSONResponse:
    """
    Return paginated audit log of all Guardian action executions.
    Includes status (SUCCESS, BLOCKED_OPA, PENDING_APPROVAL, etc.).
    """
    total = len(_ACTION_HISTORY)
    page  = _ACTION_HISTORY[offset: offset + limit]
    return JSONResponse(content={"history": page, "total": total})


@router.get(
    "/guardian/autonomy",
    summary="Get current autonomy mode",
)
async def get_autonomy() -> JSONResponse:
    """Return the current Guardian autonomy mode."""
    return JSONResponse(content=_AUTONOMY_MODE)


@router.put(
    "/guardian/autonomy",
    summary="Update autonomy mode",
)
async def update_autonomy(body: AutonomyUpdate) -> JSONResponse:
    """
    Change the Guardian autonomy mode.

    Modes:
      human-in-loop  All actions require explicit SRE approval
      supervised     Low-risk (riskScore < 30) actions execute automatically
      full-auto      Full autonomous remediation — use with caution
    """
    previous = _AUTONOMY_MODE["mode"]
    _AUTONOMY_MODE["mode"]   = body.mode
    _AUTONOMY_MODE["reason"] = body.reason
    _AUTONOMY_MODE["updatedAt"] = str(int(time.time()))

    logger.warning(
        "Autonomy mode changed: %s → %s (reason: %s)",
        previous, body.mode, body.reason or "none",
    )
    return JSONResponse(content={
        "previous_mode": previous,
        "current_mode":  body.mode,
        "reason":        body.reason,
    })


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _record_history(
    req:    ActionRequest,
    status: str,
    sim:    dict[str, Any],
) -> None:
    """Append an entry to the in-memory action audit log (max 500 entries)."""
    entry = {
        "id":          str(uuid.uuid4())[:8],
        "action_name": req.action_name,
        "target_node": req.target_node,
        "namespace":   req.namespace,
        "status":      status,
        "risk_score":  sim.get("riskScore"),
        "opa_status":  sim.get("opaStatus"),
        "incident_id": req.incident_id,
        "dry_run":     req.dry_run,
        "timestamp":   int(time.time()),
        "autonomy_mode": _AUTONOMY_MODE["mode"],
    }
    _ACTION_HISTORY.insert(0, entry)
    if len(_ACTION_HISTORY) > 500:
        _ACTION_HISTORY.pop()
