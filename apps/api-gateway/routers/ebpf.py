"""
CCDT API Gateway — eBPF Router
────────────────────────────────────────────────────────────────────────────────
Endpoints:
  GET  /api/v1/ebpf/events           → list recent eBPF events (filterable)
  GET  /api/v1/ebpf/events/{id}      → single event detail
  GET  /api/v1/ebpf/metrics          → per-node aggregated metrics snapshot
  GET  /api/v1/ebpf/probes           → probe health and overhead
  GET  /api/v1/ebpf/probes/{name}    → single probe detail
  POST /api/v1/ebpf/probes/{name}/enable  → enable a probe
  POST /api/v1/ebpf/probes/{name}/disable → disable a probe
  WS   /ws/ebpf/stream               → live event stream push
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger("ccdt.routers.ebpf")

router = APIRouter(prefix="/api/v1", tags=["ebpf"])

# ─── Service URLs ─────────────────────────────────────────────────────────────
NERVOUS_SERVICE_URL = os.getenv("NERVOUS_SERVICE_URL", "http://layer1-nervous:8000")
EBPF_STREAM_INTERVAL = float(os.getenv("EBPF_STREAM_INTERVAL_S", "1.0"))

# ─── Type aliases ─────────────────────────────────────────────────────────────
EBPFEventType = Literal["syscall", "oom", "tcp", "sched", "file", "capability", "probe"]
SeverityLevel = Literal["critical", "warning", "info"]

# ─── Static probe definitions ─────────────────────────────────────────────────
_PROBE_REGISTRY: dict[str, dict[str, Any]] = {
    "scheduler": {
        "name":         "scheduler",
        "bpf_program":  "sched_wakeup / sched_switch",
        "hook":         "tp/sched/sched_switch",
        "status":       "active",
        "overhead_pct": "0.12",
        "events_total": 84320,
        "events_rate":  420,
        "ring_buffer":  "16MB",
        "threshold_ms": 1,
        "description":  "Run-queue latency histogram; emits events when p99 > 1ms",
    },
    "oom_kill": {
        "name":         "oom_kill",
        "bpf_program":  "oom_kill_process",
        "hook":         "kprobe/oom_kill_process",
        "status":       "active",
        "overhead_pct": "0.01",
        "events_total": 3,
        "events_rate":  0,
        "ring_buffer":  "1MB",
        "threshold_ms": None,
        "description":  "OOM kill probe with cgroup fingerprinting and per-cgroup counter map",
    },
    "tcp_retransmit": {
        "name":         "tcp_retransmit",
        "bpf_program":  "tcp_retransmit_skb",
        "hook":         "kprobe/tcp_retransmit_skb",
        "status":       "active",
        "overhead_pct": "0.08",
        "events_total": 12440,
        "events_rate":  62,
        "ring_buffer":  "16MB",
        "threshold_ms": None,
        "description":  "TCP retransmit tracking, RTT measurement, connection lifecycle",
    },
    "syscall": {
        "name":         "syscall",
        "bpf_program":  "sys_enter_*",
        "hook":         "tp/syscalls/sys_enter_execve + setuid + ptrace + mount",
        "status":       "active",
        "overhead_pct": "0.34",
        "events_total": 421800,
        "events_rate":  2109,
        "ring_buffer":  "16MB",
        "threshold_ms": None,
        "description":  "Security syscall filter: execve/setuid/ptrace/mount/pivot_root/unshare",
    },
    "file_access": {
        "name":         "file_access",
        "bpf_program":  "lsm_file_open",
        "hook":         "lsm/file_open",
        "status":       "active",
        "overhead_pct": "0.15",
        "events_total": 18200,
        "events_rate":  91,
        "ring_buffer":  "4MB",
        "threshold_ms": None,
        "description":  "LSM hook on file_open; monitors /etc/shadow and /proc paths",
    },
    "capability": {
        "name":         "capability",
        "bpf_program":  "cap_capable",
        "hook":         "kprobe/cap_capable",
        "status":       "active",
        "overhead_pct": "0.01",
        "events_total": 14,
        "events_rate":  0,
        "ring_buffer":  "1MB",
        "threshold_ms": None,
        "description":  "CAP_SYS_ADMIN / CAP_NET_ADMIN / CAP_SYS_PTRACE detection",
    },
}

# Possible probe states for toggle operations
_PROBE_STATUS: dict[str, str] = {name: "active" for name in _PROBE_REGISTRY}

# ─── Seed events (in-memory ring buffer — max 500) ────────────────────────────
_PODS  = ["order-svc", "postgres", "payment-svc", "notify-svc", "redis", "api-gw", "auth-svc"]
_NODES = ["node-01", "node-02", "node-03"]
_EVENT_TEMPLATES: list[dict[str, Any]] = [
    {"type": "capability", "severity": "critical", "detail": "CAP_SYS_ADMIN granted pid=7841 uid=1000 comm=python3"},
    {"type": "oom",        "severity": "critical", "detail": "OOM kill rss=3.8GB limit=4GB cgroup=order-svc"},
    {"type": "tcp",        "severity": "warning",  "detail": "retransmit 187/s rtt=88000µs sport=42180 dport=5432"},
    {"type": "sched",      "severity": "warning",  "detail": "latency_p99=142ms cpu=2 comm=python3 pid=7841"},
    {"type": "file",       "severity": "critical", "detail": "/etc/shadow read-attempt uid=1000 pid=7841"},
    {"type": "syscall",    "severity": "warning",  "detail": "execve('/usr/bin/curl') uid=1000 pid=8204"},
    {"type": "syscall",    "severity": "warning",  "detail": "mount('/proc') uid=0 pid=7841 flags=MS_BIND"},
    {"type": "tcp",        "severity": "info",     "detail": "retransmit 4/s rtt=1200µs sport=54321 dport=6379"},
    {"type": "probe",      "severity": "info",     "detail": "probe_attach sched_switch cpu_overhead=0.12%"},
    {"type": "sched",      "severity": "info",     "detail": "latency_p99=8ms cpu=0 comm=postgres pid=1234"},
    {"type": "capability", "severity": "critical", "detail": "CAP_NET_ADMIN granted pid=9012 uid=0 comm=ip"},
    {"type": "syscall",    "severity": "critical", "detail": "ptrace(PTRACE_ATTACH) pid=7841 target=1234"},
]

_event_id_counter = 1


def _make_event(template: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate a synthetic eBPF event."""
    global _event_id_counter
    tpl = template or random.choice(_EVENT_TEMPLATES)
    now = time.localtime()
    ts  = f"{now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d}.{random.randint(0,999):03d}"
    evt = {
        "id":       _event_id_counter,
        "ts":       ts,
        "type":     tpl["type"],
        "pod":      random.choice(_PODS),
        "node":     random.choice(_NODES),
        "detail":   tpl["detail"],
        "severity": tpl["severity"],
        "epoch":    int(time.time()),
    }
    _event_id_counter += 1
    return evt


# Seed initial 50 events
_EVENT_RING: list[dict[str, Any]] = [_make_event() for _ in range(50)]


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _fetch_from_nervous(path: str) -> dict:
    """Forward a request to the Layer-1 Nervous System service."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{NERVOUS_SERVICE_URL}{path}")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.debug("Nervous service unreachable (%s) — returning local data", exc)
        return {}


def _add_event(evt: dict[str, Any]) -> None:
    """Add event to ring buffer, keeping max 500 entries."""
    _EVENT_RING.insert(0, evt)
    if len(_EVENT_RING) > 500:
        _EVENT_RING.pop()


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get(
    "/ebpf/events",
    summary="List recent eBPF events",
)
async def list_events(
    type_:    str | None = Query(None, alias="type",  description="Filter by event type"),
    severity: str | None = Query(None, description="Filter by severity"),
    pod:      str | None = Query(None, description="Filter by pod name"),
    node:     str | None = Query(None, description="Filter by node name"),
    limit:    int        = Query(100,  ge=1, le=500),
    offset:   int        = Query(0,    ge=0),
) -> JSONResponse:
    """
    Return recent eBPF events from the 500-event ring buffer.
    Events are sorted newest-first.

    When the Layer-1 Nervous System service is available, data is fetched live.
    Otherwise falls back to the in-process simulated ring buffer.
    """
    # Try live service first
    live = await _fetch_from_nervous(f"/events?limit={limit + offset}")
    events: list[dict[str, Any]] = live.get("events", _EVENT_RING)

    # Simulate a new event on each request for realistic demo behaviour
    if not live:
        _add_event(_make_event())
        events = _EVENT_RING[:]

    # Apply filters
    if type_:
        events = [e for e in events if e.get("type") == type_]
    if severity:
        events = [e for e in events if e.get("severity") == severity]
    if pod:
        events = [e for e in events if e.get("pod") == pod]
    if node:
        events = [e for e in events if e.get("node") == node]

    total  = len(events)
    paged  = events[offset: offset + limit]

    return JSONResponse(content={
        "events":    paged,
        "total":     total,
        "limit":     limit,
        "offset":    offset,
        "source":    "live" if live else "simulated",
    })


@router.get(
    "/ebpf/events/{event_id}",
    summary="Get a single eBPF event by ID",
)
async def get_event(event_id: int) -> JSONResponse:
    """Return full detail for a single eBPF event."""
    event = next((e for e in _EVENT_RING if e.get("id") == event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found in ring buffer")
    return JSONResponse(content=event)


@router.get(
    "/ebpf/metrics",
    summary="Per-node aggregated eBPF metrics",
)
async def get_metrics() -> JSONResponse:
    """
    Return aggregated per-node metrics derived from eBPF measurements:
    CPU utilisation, scheduler latency, TCP retransmit rates, OOM counts.
    """
    live = await _fetch_from_nervous("/metrics")
    if live:
        return JSONResponse(content=live)

    # Fallback: synthesise per-node snapshot
    nodes_metrics = []
    for node in _NODES:
        nodes_metrics.append({
            "node":              node,
            "cpu_pct":           round(20 + random.random() * 70, 1),
            "mem_pct":           round(30 + random.random() * 60, 1),
            "sched_latency_p99": round(5  + random.random() * 140, 1),
            "tcp_retransmits":   random.randint(0, 200),
            "oom_kills":         random.randint(0, 3),
            "syscall_rate":      random.randint(100, 8000),
            "cap_events":        random.randint(0, 5),
            "timestamp":         int(time.time()),
        })

    total_events = sum(p["events_total"] for p in _PROBE_REGISTRY.values())
    total_overhead = round(sum(float(p["overhead_pct"]) for p in _PROBE_REGISTRY.values()), 2)

    return JSONResponse(content={
        "nodes":           nodes_metrics,
        "total_events":    total_events,
        "total_overhead":  f"{total_overhead}%",
        "ring_buffer_usage": f"{len(_EVENT_RING)}/500",
        "timestamp":       int(time.time()),
        "source":          "simulated",
    })


@router.get(
    "/ebpf/probes",
    summary="Probe registry and health",
)
async def list_probes() -> JSONResponse:
    """
    Return all registered eBPF probes with their current status,
    event counts and CPU overhead percentage.
    """
    live = await _fetch_from_nervous("/probes")
    if live:
        return JSONResponse(content=live)

    probes = []
    for name, probe in _PROBE_REGISTRY.items():
        probes.append({
            **probe,
            "status": _PROBE_STATUS.get(name, "active"),
            # Simulate slowly growing event counts
            "events_total": probe["events_total"] + random.randint(0, 100),
        })

    total_overhead = round(
        sum(float(p["overhead_pct"]) for p in _PROBE_REGISTRY.values()), 2
    )

    return JSONResponse(content={
        "probes":          probes,
        "total":           len(probes),
        "active":          sum(1 for s in _PROBE_STATUS.values() if s == "active"),
        "total_overhead":  f"{total_overhead}%",
        "overhead_budget": "1.00%",
        "source":          "simulated",
    })


@router.get(
    "/ebpf/probes/{probe_name}",
    summary="Single probe detail",
)
async def get_probe(probe_name: str) -> JSONResponse:
    """Return detailed information for a single eBPF probe."""
    probe = _PROBE_REGISTRY.get(probe_name)
    if probe is None:
        raise HTTPException(
            status_code=404,
            detail=f"Probe '{probe_name}' not found. "
                   f"Available: {list(_PROBE_REGISTRY.keys())}",
        )
    return JSONResponse(content={
        **probe,
        "status": _PROBE_STATUS.get(probe_name, "active"),
    })


@router.post(
    "/ebpf/probes/{probe_name}/enable",
    summary="Enable an eBPF probe",
)
async def enable_probe(probe_name: str) -> JSONResponse:
    """Enable a disabled eBPF probe."""
    if probe_name not in _PROBE_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Probe '{probe_name}' not found")

    previous = _PROBE_STATUS.get(probe_name, "active")
    _PROBE_STATUS[probe_name] = "active"
    logger.info("Probe enabled: %s (was: %s)", probe_name, previous)

    return JSONResponse(content={
        "probe":    probe_name,
        "status":   "active",
        "previous": previous,
    })


@router.post(
    "/ebpf/probes/{probe_name}/disable",
    summary="Disable an eBPF probe",
)
async def disable_probe(probe_name: str) -> JSONResponse:
    """
    Disable an eBPF probe.
    Note: disabling the syscall or capability probe reduces security coverage.
    """
    if probe_name not in _PROBE_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Probe '{probe_name}' not found")

    previous = _PROBE_STATUS.get(probe_name, "active")
    _PROBE_STATUS[probe_name] = "inactive"

    warning = (
        "WARNING: Disabling this probe reduces security visibility"
        if probe_name in ("syscall", "capability", "file_access")
        else None
    )
    logger.warning("Probe disabled: %s", probe_name)

    return JSONResponse(content={
        "probe":    probe_name,
        "status":   "inactive",
        "previous": previous,
        "warning":  warning,
    })


# ─── WebSocket: live eBPF event stream ────────────────────────────────────────

@router.websocket("/ws/ebpf/stream")
async def ebpf_stream(
    websocket: WebSocket,
    type_:     str | None = Query(None, alias="type"),
    severity:  str | None = Query(None),
):
    """
    WebSocket endpoint that pushes new eBPF events as they arrive (~1 s cadence).

    Optional query params (same as GET /ebpf/events):
      type=capability|oom|tcp|sched|file|syscall|probe
      severity=critical|warning|info

    Message format:
      {"type": "ebpf_event", "payload": <event>, "timestamp": <epoch>}
    """
    await websocket.accept()
    logger.info("WS eBPF stream opened: %s type=%s severity=%s",
                websocket.client, type_, severity)

    try:
        while True:
            # Pick a template weighted towards interesting events
            template = random.choices(
                _EVENT_TEMPLATES,
                weights=[3, 3, 2, 2, 3, 2, 2, 1, 1, 1, 2, 3],
                k=1,
            )[0]

            evt = _make_event(template)
            _add_event(evt)

            # Apply stream filter before sending
            if type_ and evt["type"] != type_:
                await asyncio.sleep(EBPF_STREAM_INTERVAL)
                continue
            if severity and evt["severity"] != severity:
                await asyncio.sleep(EBPF_STREAM_INTERVAL)
                continue

            await websocket.send_text(json.dumps({
                "type":      "ebpf_event",
                "payload":   evt,
                "timestamp": int(time.time()),
            }))

            await asyncio.sleep(EBPF_STREAM_INTERVAL)

    except WebSocketDisconnect:
        logger.info("WS eBPF stream closed: %s", websocket.client)
    except Exception as exc:
        logger.error("WS eBPF stream error: %s", exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
