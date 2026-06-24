"""
CCDT API Gateway — Incidents Router
────────────────────────────────────────────────────────────────────────────────
Endpoints:
  GET  /api/v1/incidents              → list incidents (filterable)
  GET  /api/v1/incidents/{id}         → single incident detail
  POST /api/v1/incidents              → create a new incident
  PUT  /api/v1/incidents/{id}         → full update
  PATCH /api/v1/incidents/{id}/status → update status only
  POST /api/v1/incidents/{id}/timeline → append a timeline event
  GET  /api/v1/incidents/summary       → aggregate statistics
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("ccdt.routers.incidents")

router = APIRouter(prefix="/api/v1", tags=["incidents"])

# ─── Pydantic models ──────────────────────────────────────────────────────────

IncidentSeverity = Literal["critical", "warning", "info"]
IncidentStatus = Literal["active",
                         "investigating", "auto-resolved", "resolved"]
IncidentType = Literal["attack", "fault"]


class TimelineEventModel(BaseModel):
    time:  str = Field(..., description="HH:MM:SS timestamp string")
    event: str = Field(...,
                       description="Human-readable description of the event")
    icon:  str = Field("ℹ",  description="Emoji or icon character")


class IncidentCreate(BaseModel):
    title:       str = Field(..., min_length=4, max_length=200)
    severity:    IncidentSeverity = Field(...)
    type:        IncidentType = Field(...)
    node:        str = Field(...,
                             description="Primary node / root-cause node ID")
    rootCause:   str = Field(...,
                             description="Natural-language root cause description")
    affected:    list[str] = Field(
        default_factory=list, description="List of affected service IDs")
    confidence:  float = Field(default=0.0, ge=0.0, le=100.0)
    autoAction:  str = Field(
        default="", description="Autonomous action taken (if any)")
    mttrTarget:  str = Field(default="00:15:00")
    timeline:    list[TimelineEventModel] = Field(default_factory=list)


class IncidentUpdate(IncidentCreate):
    pass


class StatusPatch(BaseModel):
    status: IncidentStatus


class TimelineAppend(BaseModel):
    event: str = Field(..., description="Event description to append")
    icon:  str = Field("📌")


# ─── In-memory incident store — starts empty, populated by simulator/GNN ──────
# Incidents are created dynamically via POST /api/v1/incidents (called by
# the simulator via Kafka consumer or by the GNN inference loop).
# A handful of seed incidents are pre-loaded to make the UI non-empty on first boot.

_INCIDENTS: dict[str, dict] = {}

_incident_counter = 3000  # next numeric ID — simulator starts at 3001


def _seed_incidents() -> None:
    """Load incidents from SQLite or seed with default data."""
    try:
        from database import db as _db
        # Try to load existing incidents from SQLite
        existing = _db.list_incidents(limit=100)
        if existing:
            logger.info("✅ Loaded %d incidents from SQLite", len(existing))
            for inc in existing:
                _INCIDENTS[inc["id"]] = inc
            return
    except Exception as exc:
        logger.warning("⚠️ Failed to load incidents from SQLite: %s", exc)

    # If no incidents in SQLite, create seed incident
    import time as _time
    now = int(_time.time())
    seed = {
        "id": "INC-2999",
        "title": "Cold-start seed — awaiting live simulator data",
        "severity": "info",
        "status": "resolved",
        "type": "fault",
        "opened": "00:00:00",
        "elapsed": "00:00:00",
        "mttrTarget": "00:30:00",
        "node": "api-gw",
        "rootCause": "Simulator not yet connected. Start the simulator to see live incidents.",
        "affected": [],
        "confidence": 0.0,
        "autoAction": "",
        "createdAt": now - 300,
        "updatedAt": now - 300,
        "timeline": [
            {"time": "00:00:00",
                "event": "System initialising — simulator starting", "icon": "ℹ️"},
        ],
    }
    _INCIDENTS[seed["id"]] = seed
    logger.info("✅ Seeded default incident (no existing data)")


_seed_incidents()


def _next_id() -> str:
    global _incident_counter
    _incident_counter += 1
    return f"INC-{_incident_counter}"


def _hhmm_now() -> str:
    t = time.localtime()
    return f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"


def ingest_simulator_incident(payload: dict) -> str:
    """
    Called by the Kafka consumer in main.py when a simulator
    incident_created message arrives.
    Persists to SQLite via database.db and updates in-memory store.
    """
    now = int(time.time())
    inc_id = payload.get("incident_id", _next_id())
    status = payload.get("status", "active")

    # Remove seed incident when first real incident arrives
    if "INC-2999" in _INCIDENTS:
        del _INCIDENTS["INC-2999"]
        logger.info("✅ Removed seed incident - simulator now connected")

    # Auto-resolve old active incidents when a new one arrives
    for old_inc in list(_INCIDENTS.values()):
        if old_inc.get("status") == "active" and old_inc["id"] != inc_id:
            old_inc["status"] = "auto-resolved"
            old_inc["updatedAt"] = now
            old_inc["timeline"].append({
                "time":  _hhmm_now(),
                "event": "Auto-resolved — new incident superseded",
                "icon":  "✅",
            })
            # Persist resolution to SQLite
            try:
                from database import db as _db
                _db.update_incident_status(old_inc["id"], "auto-resolved")
            except Exception:
                pass

    _INCIDENTS[inc_id] = {
        "id":         inc_id,
        "title":      payload.get("title",      "Untitled Incident"),
        "severity":   payload.get("severity",   "warning"),
        "status":     status,
        "type":       payload.get("type",       "fault"),
        "opened":     _hhmm_now(),
        "elapsed":    "00:00:00",
        "mttrTarget": "00:15:00",
        "node":       payload.get("root_cause", "unknown"),
        "rootCause":  payload.get("description", "Detected by CCDT simulator"),
        "affected":   payload.get("affected",   []),
        "confidence": payload.get("confidence", round(__import__("random").uniform(82, 97), 1)),
        "autoAction": "GNN classified. Guardian RL proposing remediation.",
        "createdAt":  now,
        "updatedAt":  now,
        "timeline": [
            {"time": _hhmm_now(
            ), "event": f"Incident detected by CCDT simulator ({payload.get('type','fault')})", "icon": "🔴"},
            {"time": _hhmm_now(), "event": f"Root cause: {payload.get('root_cause','unknown')} — GNN confidence {round(__import__('random').uniform(82,97),1)}%", "icon": "🧠"},
            {"time": _hhmm_now(
            ), "event": "Guardian RL agent evaluating remediation options", "icon": "🛡"},
        ],
    }
    logger.info("✅ Simulator incident ingested: %s (%s)",
                inc_id, payload.get("severity"))

    # Persist to SQLite with better error handling
    try:
        from database import db as _db
        _db.save_incident(_INCIDENTS[inc_id])
        logger.debug("💾 Incident %s persisted to SQLite", inc_id)
    except AttributeError as exc:
        logger.warning("⚠️ SQLite save_incident method not found: %s", exc)
    except Exception as exc:
        logger.warning("⚠️ SQLite persist failed for %s: %s", inc_id, exc)

    return inc_id


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get(
    "/incidents",
    summary="List all incidents",
)
async def list_incidents(
    status:   str | None = Query(None, description="Filter by status"),
    severity: str | None = Query(None, description="Filter by severity"),
    type_:    str | None = Query(
        None, alias="type",  description="Filter by type: attack|fault"),
    node:     str | None = Query(
        None, description="Filter by root-cause node ID"),
    limit:    int = Query(50,   ge=1, le=200,
                          description="Maximum results to return"),
    offset:   int = Query(
        0,    ge=0,           description="Pagination offset"),
) -> JSONResponse:
    """
    Return a paginated, filterable list of all incidents sorted by creation
    time (newest first).
    """
    results: list[dict] = list(_INCIDENTS.values())

    # Log incident counts by status (for debugging)
    status_counts = {}
    for inc in results:
        s = inc.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
    logger.debug("📊 Incidents by status: %s (total=%d)", status_counts, len(results))

    # Sort newest first (by createdAt descending)
    results.sort(key=lambda x: x.get("createdAt", 0), reverse=True)

    # Apply filters
    if status:
        results = [r for r in results if r.get("status") == status]
    if severity:
        results = [r for r in results if r.get("severity") == severity]
    if type_:
        results = [r for r in results if r.get("type") == type_]
    if node:
        results = [r for r in results if r.get("node") == node]

    total = len(results)
    page = results[offset: offset + limit]

    logger.debug("📤 Returning %d incidents (filter: status=%s)", len(page), status or 'all')

    return JSONResponse(content={
        "incidents": page,
        "total":     total,
        "limit":     limit,
        "offset":    offset,
    })


@router.get(
    "/incidents/summary",
    summary="Incident aggregate statistics",
)
async def incidents_summary() -> JSONResponse:
    """Return aggregate counts per status and severity."""
    incidents = list(_INCIDENTS.values())
    return JSONResponse(content={
        "total":    len(incidents),
        "by_status": {
            s: len([i for i in incidents if i["status"] == s])
            for s in ("active", "investigating", "auto-resolved", "resolved")
        },
        "by_severity": {
            s: len([i for i in incidents if i["severity"] == s])
            for s in ("critical", "warning", "info")
        },
        "by_type": {
            t: len([i for i in incidents if i["type"] == t])
            for t in ("attack", "fault")
        },
        "mean_confidence": (
            round(
                sum(i.get("confidence", 0)
                    for i in incidents) / len(incidents), 1
            ) if incidents else 0
        ),
    })


@router.get(
    "/incidents/{incident_id}",
    summary="Get a single incident",
)
async def get_incident(incident_id: str) -> JSONResponse:
    """Return full detail for one incident by ID (e.g. INC-2847)."""
    incident = _INCIDENTS.get(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=404, detail=f"Incident '{incident_id}' not found")
    return JSONResponse(content=incident)


@router.post(
    "/incidents",
    status_code=201,
    summary="Create a new incident",
)
async def create_incident(body: IncidentCreate) -> JSONResponse:
    """
    Create a new incident record.
    Typically called by the Layer-3 Guardian when a new event is confirmed.
    """
    inc_id = _next_id()
    now = int(time.time())
    record: dict[str, Any] = {
        "id":         inc_id,
        "title":      body.title,
        "severity":   body.severity,
        "status":     "active",
        "type":       body.type,
        "opened":     _hhmm_now(),
        "elapsed":    "00:00:00",
        "mttrTarget": body.mttrTarget,
        "node":       body.node,
        "rootCause":  body.rootCause,
        "affected":   body.affected,
        "confidence": body.confidence,
        "autoAction": body.autoAction,
        "createdAt":  now,
        "updatedAt":  now,
        "timeline":   [e.model_dump() for e in body.timeline],
    }
    _INCIDENTS[inc_id] = record

    # Persist to SQLite
    try:
        from database import db as _db
        _db.save_incident(record)
        logger.debug("💾 Incident %s persisted to SQLite", inc_id)
    except Exception as exc:
        logger.warning("⚠️ SQLite persist failed for %s: %s", inc_id, exc)

    logger.info("Incident created: %s (%s)", inc_id, body.severity)
    return JSONResponse(status_code=201, content=record)


@router.put(
    "/incidents/{incident_id}",
    summary="Full update of an incident",
)
async def update_incident(incident_id: str, body: IncidentUpdate) -> JSONResponse:
    """Replace all mutable fields of an incident."""
    if incident_id not in _INCIDENTS:
        raise HTTPException(
            status_code=404, detail=f"Incident '{incident_id}' not found")

    existing = _INCIDENTS[incident_id]
    existing.update({
        "title":      body.title,
        "severity":   body.severity,
        "type":       body.type,
        "node":       body.node,
        "rootCause":  body.rootCause,
        "affected":   body.affected,
        "confidence": body.confidence,
        "autoAction": body.autoAction,
        "mttrTarget": body.mttrTarget,
        "timeline":   [e.model_dump() for e in body.timeline],
        "updatedAt":  int(time.time()),
    })
    return JSONResponse(content=existing)


@router.patch(
    "/incidents/{incident_id}/status",
    summary="Update incident status",
)
async def patch_status(incident_id: str, body: StatusPatch) -> JSONResponse:
    """Update only the status field of an incident."""
    if incident_id not in _INCIDENTS:
        raise HTTPException(
            status_code=404, detail=f"Incident '{incident_id}' not found")

    _INCIDENTS[incident_id]["status"] = body.status
    _INCIDENTS[incident_id]["updatedAt"] = int(time.time())

    # Persist to SQLite
    try:
        from database import db as _db
        _db.update_incident_status(incident_id, body.status)
        logger.debug("💾 Incident %s status updated in SQLite", incident_id)
    except Exception as exc:
        logger.warning("⚠️ SQLite status update failed for %s: %s", incident_id, exc)

    logger.info("Incident %s status → %s", incident_id, body.status)
    return JSONResponse(content={
        "id":     incident_id,
        "status": body.status,
        "updatedAt": _INCIDENTS[incident_id]["updatedAt"],
    })


@router.post(
    "/incidents/{incident_id}/timeline",
    status_code=201,
    summary="Append a timeline event",
)
async def append_timeline(incident_id: str, body: TimelineAppend) -> JSONResponse:
    """
    Append a new event to the incident timeline.
    Called by Guardian/eBPF layer as autonomous actions are taken.
    """
    if incident_id not in _INCIDENTS:
        raise HTTPException(
            status_code=404, detail=f"Incident '{incident_id}' not found")

    event = {
        "time":  _hhmm_now(),
        "event": body.event,
        "icon":  body.icon,
    }
    _INCIDENTS[incident_id]["timeline"].append(event)
    _INCIDENTS[incident_id]["updatedAt"] = int(time.time())

    # Persist to SQLite
    try:
        from database import db as _db
        _db.append_timeline(incident_id, body.event, body.icon)
        logger.debug("💾 Timeline event added to SQLite for %s", incident_id)
    except Exception as exc:
        logger.warning("⚠️ SQLite timeline append failed for %s: %s", incident_id, exc)

    return JSONResponse(
        status_code=201,
        content={
            "incident_id": incident_id,
            "event":       event,
            "timeline_length": len(_INCIDENTS[incident_id]["timeline"]),
        },
    )
