"""
CCDT API Gateway — Topology Router
────────────────────────────────────────────────────────────────────────────────
Endpoints:
  GET  /api/v1/topology              → full cluster topology snapshot
  GET  /api/v1/topology/nodes        → node list only
  GET  /api/v1/topology/nodes/{id}   → single node detail
  GET  /api/v1/topology/edges        → edge list only
  POST /api/v1/infer                 → trigger GNN inference
  POST /api/v1/counterfactual        → what-if causal analysis
  WS   /ws/topology/stream           → live topology push (2 s cadence)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("ccdt.routers.topology")

router = APIRouter(prefix="/api/v1", tags=["topology"])

# ─── Service URLs (resolved via Kubernetes service DNS in-cluster) ────────────
GNN_SERVICE_URL = os.getenv("GNN_SERVICE_URL", "http://layer2-cognitive:8001")
TOPOLOGY_PUSH_INTERVAL = float(os.getenv("TOPOLOGY_PUSH_INTERVAL_S", "2.0"))

# ─── Pydantic models ──────────────────────────────────────────────────────────

class CounterfactualRequest(BaseModel):
    target_node: str = Field(..., description="Node ID to apply do-calculus on")
    action:      str = Field(..., description="Action name, e.g. 'isolate_container'")
    parameters:  dict[str, Any] = Field(default_factory=dict)


class InferRequest(BaseModel):
    force_refresh: bool = Field(False, description="Bypass cached inference result")


# ─── Seed topology (returned when the GNN service is unreachable) ─────────────
FALLBACK_TOPOLOGY: dict[str, Any] = {
    "nodes": [
        {"id": "api-gw",        "label": "API Gateway",       "x": 400, "y": 60,  "status": "healthy",  "layer": "network",  "cpu": 42, "mem": 58,  "namespace": "default", "nodeName": "node-01", "restarts": 0},
        {"id": "auth-svc",      "label": "Auth Service",      "x": 200, "y": 160, "status": "healthy",  "layer": "service",  "cpu": 31, "mem": 44,  "namespace": "default", "nodeName": "node-02", "restarts": 0},
        {"id": "order-svc",     "label": "Order Service",     "x": 400, "y": 160, "status": "critical", "layer": "service",  "cpu": 94, "mem": 87,  "namespace": "default", "nodeName": "node-01", "restarts": 3},
        {"id": "payment-svc",   "label": "Payment Service",   "x": 600, "y": 160, "status": "warning",  "layer": "service",  "cpu": 67, "mem": 71,  "namespace": "default", "nodeName": "node-03", "restarts": 1},
        {"id": "inventory-svc", "label": "Inventory Service", "x": 150, "y": 280, "status": "healthy",  "layer": "service",  "cpu": 28, "mem": 39,  "namespace": "default", "nodeName": "node-02", "restarts": 0},
        {"id": "notify-svc",    "label": "Notify Service",    "x": 350, "y": 280, "status": "warning",  "layer": "service",  "cpu": 73, "mem": 62,  "namespace": "default", "nodeName": "node-01", "restarts": 1},
        {"id": "postgres",      "label": "PostgreSQL",        "x": 550, "y": 280, "status": "critical", "layer": "data",     "cpu": 91, "mem": 89,  "namespace": "default", "nodeName": "node-02", "restarts": 2},
        {"id": "redis",         "label": "Redis Cache",       "x": 200, "y": 380, "status": "healthy",  "layer": "data",     "cpu": 18, "mem": 45,  "namespace": "default", "nodeName": "node-03", "restarts": 0},
        {"id": "kafka",         "label": "Kafka Broker",      "x": 420, "y": 380, "status": "healthy",  "layer": "system",   "cpu": 35, "mem": 52,  "namespace": "default", "nodeName": "node-02", "restarts": 0},
        {"id": "monitoring",    "label": "VictoriaMetrics",   "x": 620, "y": 380, "status": "healthy",  "layer": "system",   "cpu": 22, "mem": 41,  "namespace": "monitoring", "nodeName": "node-03", "restarts": 0},
    ],
    "edges": [
        {"from": "api-gw",        "to": "auth-svc",      "type": "grpc",  "causal": False, "latencyMs": 2.1,  "errorRate": 0.001, "requestRate": 320},
        {"from": "api-gw",        "to": "order-svc",     "type": "http",  "causal": True,  "latencyMs": 142.0,"errorRate": 0.084, "requestRate": 280},
        {"from": "api-gw",        "to": "payment-svc",   "type": "http",  "causal": False, "latencyMs": 18.4, "errorRate": 0.003, "requestRate": 95},
        {"from": "order-svc",     "to": "postgres",      "type": "tcp",   "causal": True,  "latencyMs": 88.0, "errorRate": 0.124, "requestRate": 540},
        {"from": "order-svc",     "to": "notify-svc",    "type": "kafka", "causal": True,  "latencyMs": 5.2,  "errorRate": 0.002, "requestRate": 120},
        {"from": "payment-svc",   "to": "postgres",      "type": "tcp",   "causal": False, "latencyMs": 12.1, "errorRate": 0.004, "requestRate": 90},
        {"from": "inventory-svc", "to": "postgres",      "type": "tcp",   "causal": False, "latencyMs": 8.4,  "errorRate": 0.001, "requestRate": 60},
        {"from": "notify-svc",    "to": "kafka",         "type": "kafka", "causal": False, "latencyMs": 3.1,  "errorRate": 0.000, "requestRate": 180},
        {"from": "order-svc",     "to": "redis",         "type": "tcp",   "causal": False, "latencyMs": 1.2,  "errorRate": 0.000, "requestRate": 820},
        {"from": "monitoring",    "to": "kafka",         "type": "probe", "causal": False, "latencyMs": 0.8,  "errorRate": 0.000, "requestRate": 40},
    ],
    "metadata": {
        "source":    "fallback",
        "timestamp": 0,
    },
}

FALLBACK_INFERENCE: dict[str, Any] = {
    "nodeClassifications": {
        "api-gw":        {"healthy": 0.91, "fault": 0.06, "attack": 0.03},
        "auth-svc":      {"healthy": 0.88, "fault": 0.09, "attack": 0.03},
        "order-svc":     {"healthy": 0.03, "fault": 0.11, "attack": 0.86},
        "payment-svc":   {"healthy": 0.62, "fault": 0.28, "attack": 0.10},
        "inventory-svc": {"healthy": 0.93, "fault": 0.05, "attack": 0.02},
        "notify-svc":    {"healthy": 0.31, "fault": 0.64, "attack": 0.05},
        "postgres":      {"healthy": 0.04, "fault": 0.94, "attack": 0.02},
        "redis":         {"healthy": 0.81, "fault": 0.17, "attack": 0.02},
        "kafka":         {"healthy": 0.94, "fault": 0.05, "attack": 0.01},
        "monitoring":    {"healthy": 0.97, "fault": 0.02, "attack": 0.01},
    },
    "graphClassification":  {"healthy": 0.04, "fault": 0.14, "attack": 0.82},
    "rootCauseNode":        "order-svc",
    "rootCauseConfidence":  0.942,
    "incidentType":         "attack",
    "blastRadius":          ["order-svc", "postgres", "notify-svc"],
    "causalChain": [
        {"node": "order-svc",   "causalScore": 0.942, "status": "critical"},
        {"node": "postgres",    "causalScore": 0.871, "status": "critical"},
        {"node": "notify-svc",  "causalScore": 0.634, "status": "warning"},
        {"node": "payment-svc", "causalScore": 0.281, "status": "warning"},
        {"node": "api-gw",      "causalScore": 0.063, "status": "healthy"},
    ],
    "inferenceMs": 8.4,
    "source": "fallback",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _fetch_from_gnn(path: str, payload: dict | None = None) -> dict:
    """
    Forward a request to the Layer-2 GNN service.
    Returns fallback data on any connection error.
    """
    url = f"{GNN_SERVICE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if payload is not None:
                resp = await client.post(url, json=payload)
            else:
                resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("GNN service unreachable (%s) — returning fallback data", exc)
        return {}


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get(
    "/topology",
    summary="Full cluster topology",
    response_description="All nodes and directed edges representing the live service graph",
)
async def get_topology(
    namespace: str | None = Query(None, description="Filter by Kubernetes namespace"),
    layer:     str | None = Query(None, description="Filter by layer: network|service|data|system"),
) -> JSONResponse:
    """
    Return the current cluster topology as a node-edge graph.

    Fetches from the Layer-2 GNN service `/topology` endpoint.
    Falls back to embedded seed data when the GNN service is unreachable
    (e.g. during local development without the full stack running).
    """
    data = await _fetch_from_gnn("/topology")
    if not data:
        data = FALLBACK_TOPOLOGY.copy()

    # Apply optional filters
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    if namespace:
        node_ids = {n["id"] for n in nodes if n.get("namespace") == namespace}
        nodes    = [n for n in nodes if n["id"] in node_ids]
        edges    = [e for e in edges if e["from"] in node_ids and e["to"] in node_ids]

    if layer:
        node_ids = {n["id"] for n in nodes if n.get("layer") == layer}
        nodes    = [n for n in nodes if n["id"] in node_ids]
        edges    = [e for e in edges if e["from"] in node_ids and e["to"] in node_ids]

    return JSONResponse(content={
        "nodes":    nodes,
        "edges":    edges,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "timestamp": int(time.time()),
    })


@router.get(
    "/topology/nodes",
    summary="All cluster nodes",
)
async def get_nodes(
    status: str | None = Query(None, description="Filter: healthy|warning|critical"),
) -> JSONResponse:
    """Return all service nodes, optionally filtered by health status."""
    data  = await _fetch_from_gnn("/topology")
    nodes = (data or FALLBACK_TOPOLOGY).get("nodes", [])

    if status:
        nodes = [n for n in nodes if n.get("status") == status]

    return JSONResponse(content={"nodes": nodes, "total": len(nodes)})


@router.get(
    "/topology/nodes/{node_id}",
    summary="Single node detail",
)
async def get_node(node_id: str) -> JSONResponse:
    """Return detailed information for a single node by ID."""
    data  = await _fetch_from_gnn("/topology")
    nodes = (data or FALLBACK_TOPOLOGY).get("nodes", [])
    node  = next((n for n in nodes if n["id"] == node_id), None)

    if node is None:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")

    return JSONResponse(content=node)


@router.get(
    "/topology/edges",
    summary="All service edges",
)
async def get_edges(
    causal: bool | None = Query(None, description="Filter causal=true|false"),
    type_:  str  | None = Query(None, alias="type", description="Filter by edge type"),
) -> JSONResponse:
    """Return all directed service edges."""
    data  = await _fetch_from_gnn("/topology")
    edges = (data or FALLBACK_TOPOLOGY).get("edges", [])

    if causal is not None:
        edges = [e for e in edges if e.get("causal") == causal]
    if type_:
        edges = [e for e in edges if e.get("type") == type_]

    return JSONResponse(content={"edges": edges, "total": len(edges)})


@router.post(
    "/infer",
    summary="Trigger GNN inference",
    response_description="Full GNN inference result with root-cause analysis",
)
async def run_inference(body: InferRequest = InferRequest()) -> JSONResponse:
    """
    Trigger a full GNN inference pass on the current cluster graph.

    The GNN service runs a GATv2 4-layer model with causal regularisation.
    Returns node/graph classifications, root cause node, blast radius,
    causal chain and counterfactual recommendations.
    """
    data = await _fetch_from_gnn("/infer", payload={"force_refresh": body.force_refresh})
    if not data:
        data = FALLBACK_INFERENCE.copy()
        data["inferenceMs"] = round(7.0 + __import__("random").random() * 4, 1)

    return JSONResponse(content=data)


@router.post(
    "/counterfactual",
    summary="What-if causal analysis",
    response_description="Pearl do-calculus counterfactual result",
)
async def run_counterfactual(body: CounterfactualRequest) -> JSONResponse:
    """
    Ask: *'What would happen to the cluster if we applied <action> to <node>?'*

    Uses Pearl's do-calculus implemented in the GNN counterfactual engine.
    Returns MTTR impact, blast radius delta, and risk score.
    """
    payload = {
        "target_node": body.target_node,
        "action":      body.action,
        "parameters":  body.parameters,
    }
    data = await _fetch_from_gnn("/counterfactual", payload=payload)
    if not data:
        # Fallback synthetic counterfactual
        import random
        data = {
            "targetNode":    body.target_node,
            "action":        body.action,
            "mttrImpactPct": round(-40 - random.random() * 30, 1),
            "riskScore":     round(10 + random.random() * 20, 1),
            "confidence":    round(0.75 + random.random() * 0.2, 3),
            "blastRadiusDelta": [],
            "recommendation": "Simulated result — GNN service unavailable",
            "source": "fallback",
        }

    return JSONResponse(content=data)


# ─── WebSocket: live topology stream ─────────────────────────────────────────

@router.websocket("/ws/topology/stream")
async def topology_stream(websocket: WebSocket):
    """
    WebSocket endpoint that pushes topology + inference updates every 2 s.

    Message format: JSON object with fields:
      type        "topology_update" | "inference_update" | "ping"
      payload     the data object
      timestamp   Unix epoch (seconds)
    """
    await websocket.accept()
    logger.info("WS topology stream opened: %s", websocket.client)

    try:
        while True:
            # Fetch latest topology
            topo_data = await _fetch_from_gnn("/topology")
            if not topo_data:
                topo_data = FALLBACK_TOPOLOGY.copy()

            topo_data["timestamp"] = int(time.time())

            await websocket.send_text(json.dumps({
                "type":      "topology_update",
                "payload":   topo_data,
                "timestamp": int(time.time()),
            }))

            await asyncio.sleep(TOPOLOGY_PUSH_INTERVAL)

    except WebSocketDisconnect:
        logger.info("WS topology stream closed: %s", websocket.client)
    except Exception as exc:
        logger.error("WS topology stream error: %s", exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
