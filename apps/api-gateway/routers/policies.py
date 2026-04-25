"""
CCDT API Gateway — OPA Policies Router
════════════════════════════════════════════════════════════════════════════════
Manages OPA Rego policies including LLM-authored ones (Enhancement 1).

Endpoints:
  GET  /api/v1/policies              → list all policies
  GET  /api/v1/policies/{id}         → single policy
  POST /api/v1/policies              → create new policy (called by Co-Pilot)
  POST /api/v1/policies/{id}/approve → human approves LLM policy → loads to OPA
  POST /api/v1/policies/{id}/reject  → reject a pending policy
  GET  /api/v1/policies/stats        → policy effectiveness stats
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("ccdt.routers.policies")
router = APIRouter(prefix="/api/v1", tags=["policies"])

OPA_URL = os.getenv("OPA_URL", "http://opa:8181")


class PolicyCreate(BaseModel):
    name:         str  = Field(..., min_length=3, max_length=80)
    rego_code:    str  = Field(..., min_length=10)
    description:  str  = Field(default="")
    source:       str  = Field(default="llm")
    triggered_by: str  = Field(default="")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/policies")
async def list_policies(status: str | None = None) -> JSONResponse:
    from database import db
    policies = db.list_policies(status=status)
    return JSONResponse({"policies": policies, "count": len(policies)})


@router.get("/policies/stats")
async def policy_stats() -> JSONResponse:
    from database import db
    policies = db.list_policies()
    active  = [p for p in policies if p["status"] == "active"]
    pending = [p for p in policies if p["status"] == "pending"]
    llm     = [p for p in policies if p["source"] == "llm"]
    return JSONResponse({
        "total":   len(policies),
        "active":  len(active),
        "pending": len(pending),
        "llm_authored": len(llm),
        "builtin": len([p for p in policies if p["source"] == "builtin"]),
    })


@router.get("/policies/{policy_id}")
async def get_policy(policy_id: int) -> JSONResponse:
    from database import db
    policy = db.get_policy(policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    return JSONResponse(policy)


@router.post("/policies")
async def create_policy(body: PolicyCreate) -> JSONResponse:
    """
    Called by the Co-Pilot when it authors a new Rego policy.
    Policy starts as 'pending' — requires human approval before loading to OPA.
    """
    from database import db
    policy_id = db.save_policy({
        "name":         body.name,
        "rego_code":    body.rego_code,
        "description":  body.description,
        "source":       body.source,
        "status":       "pending",
        "triggered_by": body.triggered_by,
    })
    logger.info("New %s policy created: %s (id=%d)", body.source, body.name, policy_id)
    return JSONResponse({
        "id":      policy_id,
        "status":  "pending",
        "message": f"Policy '{body.name}' saved. Awaiting human approval before activation.",
    }, status_code=201)


@router.post("/policies/{policy_id}/approve")
async def approve_policy(policy_id: int, approved_by: str = "operator") -> JSONResponse:
    """
    Human approves an LLM-authored policy.
    Loads the Rego code into the running OPA server via its REST API.
    """
    from database import db

    policy = db.get_policy(policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    if policy["status"] == "active":
        return JSONResponse({"message": "Policy already active"})

    # Push Rego code to OPA via REST API
    opa_result = await _load_policy_to_opa(policy["name"], policy["rego_code"])

    if opa_result["success"]:
        db.approve_policy(policy_id, approved_by=approved_by)
        logger.info("Policy '%s' approved and loaded to OPA by %s", policy["name"], approved_by)
        return JSONResponse({
            "status":  "active",
            "message": f"Policy '{policy['name']}' is now active in OPA.",
            "opa":     opa_result,
        })
    else:
        raise HTTPException(
            status_code=422,
            detail=f"OPA rejected the policy: {opa_result.get('error', 'unknown error')}. "
                   f"Check the Rego syntax."
        )


@router.post("/policies/{policy_id}/reject")
async def reject_policy(policy_id: int) -> JSONResponse:
    from database import db
    policy = db.get_policy(policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    with __import__("database").get_db() as conn:
        conn.execute(
            "UPDATE opa_policies SET status='rejected' WHERE id=?", (policy_id,)
        )
    logger.info("Policy %d rejected", policy_id)
    return JSONResponse({"status": "rejected"})


# ── OPA integration ───────────────────────────────────────────────────────────

async def _load_policy_to_opa(name: str, rego_code: str) -> dict:
    """
    Load a Rego policy into OPA via PUT /v1/policies/{name}.
    OPA accepts raw Rego text — it compiles it and makes it immediately active.
    """
    safe_name = name.replace(" ", "_").replace("-", "_").lower()
    url = f"{OPA_URL}/v1/policies/{safe_name}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.put(
                url,
                content=rego_code.encode(),
                headers={"Content-Type": "text/plain"},
            )
            if resp.status_code in (200, 201):
                return {"success": True, "opa_id": safe_name}
            else:
                return {
                    "success": False,
                    "error":   resp.text[:300],
                    "status":  resp.status_code,
                }
    except Exception as exc:
        logger.warning("OPA policy load failed: %s", exc)
        return {"success": False, "error": str(exc)}
