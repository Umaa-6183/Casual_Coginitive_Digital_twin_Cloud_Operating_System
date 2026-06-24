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
import random
import math
import copy

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
        {"id": "order-svc",     "label": "Order Service",     "x": 400, "y": 160, "status": "healthy",  "layer": "service",  "cpu": 35, "mem": 42,  "namespace": "default", "nodeName": "node-01", "restarts": 0},
        {"id": "payment-svc",   "label": "Payment Service",   "x": 600, "y": 160, "status": "healthy",  "layer": "service",  "cpu": 38, "mem": 45,  "namespace": "default", "nodeName": "node-03", "restarts": 0},
        {"id": "inventory-svc", "label": "Inventory Service", "x": 150, "y": 280, "status": "healthy",  "layer": "service",  "cpu": 28, "mem": 39,  "namespace": "default", "nodeName": "node-02", "restarts": 0},
        {"id": "notify-svc",    "label": "Notify Service",    "x": 350, "y": 280, "status": "healthy",  "layer": "service",  "cpu": 32, "mem": 48,  "namespace": "default", "nodeName": "node-01", "restarts": 0},
        {"id": "postgres",      "label": "PostgreSQL",        "x": 550, "y": 280, "status": "healthy",  "layer": "data",     "cpu": 40, "mem": 52,  "namespace": "default", "nodeName": "node-02", "restarts": 0},
        {"id": "redis",         "label": "Redis Cache",       "x": 200, "y": 380, "status": "healthy",  "layer": "data",     "cpu": 18, "mem": 45,  "namespace": "default", "nodeName": "node-03", "restarts": 0},
        {"id": "kafka",         "label": "Kafka Broker",      "x": 420, "y": 380, "status": "healthy",  "layer": "system",   "cpu": 35, "mem": 52,  "namespace": "default", "nodeName": "node-02", "restarts": 0},
        {"id": "monitoring",    "label": "VictoriaMetrics",   "x": 620, "y": 380, "status": "healthy",  "layer": "system",   "cpu": 22, "mem": 41,  "namespace": "monitoring", "nodeName": "node-03", "restarts": 0},
        {"id": "user-svc",      "label": "User Service",      "x": 50,  "y": 160, "status": "healthy",  "layer": "service",  "cpu": 29, "mem": 38,  "namespace": "default", "nodeName": "node-01", "restarts": 0},
        {"id": "search-svc",    "label": "Search Service",    "x": 700, "y": 280, "status": "healthy",  "layer": "service",  "cpu": 33, "mem": 46,  "namespace": "default", "nodeName": "node-03", "restarts": 0},
        {"id": "elasticsearch", "label": "Elasticsearch",     "x": 650, "y": 380, "status": "healthy",  "layer": "data",     "cpu": 44, "mem": 58,  "namespace": "default", "nodeName": "node-02", "restarts": 0},
    ],
    "edges": [
        {"from": "api-gw",        "to": "auth-svc",      "type": "grpc",  "causal": False, "latencyMs": 2.1,  "errorRate": 0.001, "requestRate": 320},
        {"from": "api-gw",        "to": "order-svc",     "type": "http",  "causal": False, "latencyMs": 5.4,  "errorRate": 0.002, "requestRate": 280},
        {"from": "api-gw",        "to": "payment-svc",   "type": "http",  "causal": False, "latencyMs": 4.2,  "errorRate": 0.001, "requestRate": 95},
        {"from": "api-gw",        "to": "user-svc",      "type": "http",  "causal": False, "latencyMs": 3.8,  "errorRate": 0.001, "requestRate": 450},
        {"from": "order-svc",     "to": "postgres",      "type": "tcp",   "causal": False, "latencyMs": 6.2,  "errorRate": 0.003, "requestRate": 540},
        {"from": "order-svc",     "to": "notify-svc",    "type": "kafka", "causal": False, "latencyMs": 2.8,  "errorRate": 0.001, "requestRate": 120},
        {"from": "order-svc",     "to": "inventory-svc", "type": "http",  "causal": False, "latencyMs": 7.1,  "errorRate": 0.002, "requestRate": 200},
        {"from": "payment-svc",   "to": "postgres",      "type": "tcp",   "causal": False, "latencyMs": 5.8,  "errorRate": 0.002, "requestRate": 90},
        {"from": "inventory-svc", "to": "postgres",      "type": "tcp",   "causal": False, "latencyMs": 4.9,  "errorRate": 0.001, "requestRate": 60},
        {"from": "notify-svc",    "to": "kafka",         "type": "kafka", "causal": False, "latencyMs": 3.1,  "errorRate": 0.001, "requestRate": 180},
        {"from": "order-svc",     "to": "redis",         "type": "tcp",   "causal": False, "latencyMs": 1.2,  "errorRate": 0.000, "requestRate": 820},
        {"from": "monitoring",    "to": "kafka",         "type": "probe", "causal": False, "latencyMs": 0.8,  "errorRate": 0.000, "requestRate": 40},
        {"from": "user-svc",      "to": "postgres",      "type": "tcp",   "causal": False, "latencyMs": 5.4,  "errorRate": 0.002, "requestRate": 380},
        {"from": "user-svc",      "to": "redis",         "type": "tcp",   "causal": False, "latencyMs": 1.5,  "errorRate": 0.000, "requestRate": 920},
        {"from": "search-svc",    "to": "elasticsearch", "type": "http",  "causal": False, "latencyMs": 8.2,  "errorRate": 0.003, "requestRate": 150},
        {"from": "api-gw",        "to": "search-svc",    "type": "http",  "causal": False, "latencyMs": 6.4,  "errorRate": 0.002, "requestRate": 180},
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

def _get_dynamic_fallback() -> dict[str, Any]:
    """
    Generate dynamic fallback with realistic state cycling.

    Cycle timeline (2 minute cycle):
    0.0 - 0.25 (0-30s): All healthy baseline
    0.25 - 0.40 (30-48s): Incident building up (warning phase)
    0.40 - 0.65 (48-78s): Full incident (critical phase)
    0.65 - 0.85 (78-102s): Recovery phase (critical -> warning -> healthy)
    0.85 - 1.0 (102-120s): Stable healthy
    """
    data = copy.deepcopy(FALLBACK_TOPOLOGY)
    tick = time.time()
    cycle = (tick / 120.0) % 1.0  # 2-minute cycle for better demo visibility

    # Define incident scenarios that rotate
    scenario_cycle = int(tick / 240.0) % 3  # Switch scenarios every 4 minutes

    for node in data["nodes"]:
        base_cpu = 30.0 + random.uniform(-5, 5)
        base_mem = 40.0 + random.uniform(-5, 5)
        restarts = 0

        # Scenario 1: Database overload cascade (order-svc -> postgres -> notify-svc)
        if scenario_cycle == 0:
            if node["id"] == "order-svc":
                if 0.25 <= cycle < 0.40:  # Building up
                    progress = (cycle - 0.25) / 0.15
                    base_cpu = 30.0 + (45.0 * progress)  # 30 -> 75
                    base_mem = 40.0 + (30.0 * progress)  # 40 -> 70
                elif 0.40 <= cycle < 0.65:  # Critical
                    base_cpu = 88.0 + random.uniform(-3, 7)
                    base_mem = 88.0 + random.uniform(-3, 7)
                    restarts = random.randint(2, 4)
                elif 0.65 <= cycle < 0.85:  # Recovery
                    progress = (cycle - 0.65) / 0.20
                    base_cpu = 88.0 - (58.0 * progress)  # 88 -> 30
                    base_mem = 88.0 - (48.0 * progress)  # 88 -> 40

            elif node["id"] == "postgres":
                if 0.30 <= cycle < 0.45:  # Slightly delayed
                    progress = (cycle - 0.30) / 0.15
                    base_cpu = 40.0 + (48.0 * progress)
                    base_mem = 50.0 + (42.0 * progress)
                elif 0.45 <= cycle < 0.65:
                    base_cpu = 92.0 + random.uniform(-2, 3)
                    base_mem = 93.0 + random.uniform(-3, 2)
                    restarts = random.randint(1, 3)
                elif 0.65 <= cycle < 0.85:
                    progress = (cycle - 0.65) / 0.20
                    base_cpu = 92.0 - (52.0 * progress)
                    base_mem = 93.0 - (43.0 * progress)

            elif node["id"] == "notify-svc":
                if 0.35 <= cycle < 0.50:
                    progress = (cycle - 0.35) / 0.15
                    base_cpu = 32.0 + (38.0 * progress)
                    base_mem = 48.0 + (20.0 * progress)
                elif 0.50 <= cycle < 0.65:
                    base_cpu = 70.0 + random.uniform(-3, 5)
                    base_mem = 78.0 + random.uniform(-3, 5)
                    restarts = random.randint(1, 2)
                elif 0.65 <= cycle < 0.80:
                    progress = (cycle - 0.65) / 0.15
                    base_cpu = 70.0 - (38.0 * progress)
                    base_mem = 78.0 - (30.0 * progress)

        # Scenario 2: Payment service memory leak (payment-svc -> postgres)
        elif scenario_cycle == 1:
            if node["id"] == "payment-svc":
                if 0.25 <= cycle < 0.40:
                    progress = (cycle - 0.25) / 0.15
                    base_mem = 45.0 + (48.0 * progress)  # Memory leak
                    base_cpu = 38.0 + (30.0 * progress)
                elif 0.40 <= cycle < 0.65:
                    base_mem = 93.0 + random.uniform(-2, 2)
                    base_cpu = 68.0 + random.uniform(-3, 7)
                    restarts = random.randint(2, 5)
                elif 0.65 <= cycle < 0.85:
                    progress = (cycle - 0.65) / 0.20
                    base_mem = 93.0 - (48.0 * progress)
                    base_cpu = 68.0 - (30.0 * progress)

            elif node["id"] == "postgres":
                if 0.30 <= cycle < 0.45:
                    progress = (cycle - 0.30) / 0.15
                    base_cpu = 40.0 + (30.0 * progress)
                elif 0.45 <= cycle < 0.65:
                    base_cpu = 70.0 + random.uniform(-3, 5)
                    base_mem = 77.0 + random.uniform(-2, 5)
                elif 0.65 <= cycle < 0.80:
                    progress = (cycle - 0.65) / 0.15
                    base_cpu = 70.0 - (30.0 * progress)
                    base_mem = 77.0 - (25.0 * progress)

        # Scenario 3: Search service overload (search-svc -> elasticsearch -> api-gw)
        elif scenario_cycle == 2:
            if node["id"] == "search-svc":
                if 0.25 <= cycle < 0.40:
                    progress = (cycle - 0.25) / 0.15
                    base_cpu = 33.0 + (55.0 * progress)
                    base_mem = 46.0 + (40.0 * progress)
                elif 0.40 <= cycle < 0.65:
                    base_cpu = 90.0 + random.uniform(-2, 5)
                    base_mem = 86.0 + random.uniform(-3, 9)
                    restarts = random.randint(1, 3)
                elif 0.65 <= cycle < 0.85:
                    progress = (cycle - 0.65) / 0.20
                    base_cpu = 90.0 - (57.0 * progress)
                    base_mem = 86.0 - (40.0 * progress)

            elif node["id"] == "elasticsearch":
                if 0.30 <= cycle < 0.45:
                    progress = (cycle - 0.30) / 0.15
                    base_cpu = 44.0 + (45.0 * progress)
                    base_mem = 58.0 + (35.0 * progress)
                elif 0.45 <= cycle < 0.65:
                    base_cpu = 89.0 + random.uniform(-3, 6)
                    base_mem = 93.0 + random.uniform(-3, 2)
                    restarts = random.randint(1, 2)
                elif 0.65 <= cycle < 0.85:
                    progress = (cycle - 0.65) / 0.20
                    base_cpu = 89.0 - (45.0 * progress)
                    base_mem = 93.0 - (35.0 * progress)

            elif node["id"] == "api-gw":
                if 0.35 <= cycle < 0.50:
                    progress = (cycle - 0.35) / 0.15
                    base_cpu = 42.0 + (28.0 * progress)
                elif 0.50 <= cycle < 0.65:
                    base_cpu = 70.0 + random.uniform(-3, 5)
                elif 0.65 <= cycle < 0.80:
                    progress = (cycle - 0.65) / 0.15
                    base_cpu = 70.0 - (28.0 * progress)

        node["cpu"] = max(5, min(99, base_cpu))
        node["mem"] = max(10, min(99, base_mem))
        node["restarts"] = restarts

        # Recalculate status from metrics
        if node["cpu"] > 85 or node["mem"] > 90:
            node["status"] = "critical"
        elif node["cpu"] > 65 or node["mem"] > 75:
            node["status"] = "warning"
        else:
            node["status"] = "healthy"

    # Derive edge causal from connected node statuses
    node_status = {n["id"]: n["status"] for n in data["nodes"]}
    for edge in data["edges"]:
        src_node = next((n for n in data["nodes"] if n["id"] == edge["from"]), None)
        dst_node = next((n for n in data["nodes"] if n["id"] == edge["to"]), None)
        src_s = node_status.get(edge["from"], "healthy")
        dst_s = node_status.get(edge["to"], "healthy")

        # Edge is causal if either end is critical or both ends are warning+
        edge["causal"] = src_s == "critical" or dst_s == "critical"

        if edge["causal"]:
            edge["latencyMs"] = round(random.uniform(80.0, 250.0), 1)
            edge["errorRate"] = round(random.uniform(0.08, 0.18), 3)
            edge["requestRate"] = int(edge.get("requestRate", 100) * random.uniform(0.3, 0.6))
        elif src_s == "warning" or dst_s == "warning":
            edge["latencyMs"] = round(random.uniform(20.0, 60.0), 1)
            edge["errorRate"] = round(random.uniform(0.01, 0.04), 3)
            edge["requestRate"] = int(edge.get("requestRate", 100) * random.uniform(0.7, 0.9))
        else:
            edge["latencyMs"] = round(random.uniform(1.0, 15.0), 1)
            edge["errorRate"] = round(random.uniform(0.001, 0.005), 3)
            edge["requestRate"] = edge.get("requestRate", 100)

    data["metadata"]["timestamp"] = int(time.time())
    data["metadata"]["source"] = "synthetic-dynamic"
    data["metadata"]["scenario"] = f"scenario-{scenario_cycle + 1}"
    data["metadata"]["cycle_phase"] = (
        "healthy" if cycle < 0.25 or cycle >= 0.85
        else "building" if cycle < 0.40
        else "critical" if cycle < 0.65
        else "recovery"
    )
    return data

def _get_dynamic_inference() -> dict[str, Any]:
    """Generate dynamic inference aligned with topology scenarios."""
    data = copy.deepcopy(FALLBACK_INFERENCE)
    tick = time.time()
    cycle = (tick / 120.0) % 1.0  # Match 2-minute topology cycle
    scenario_cycle = int(tick / 240.0) % 3

    # Phase determination
    is_building = 0.25 <= cycle < 0.40
    is_critical = 0.40 <= cycle < 0.65
    is_recovery = 0.65 <= cycle < 0.85
    is_incident = is_building or is_critical or is_recovery

    # Determine affected nodes based on scenario
    if scenario_cycle == 0:
        critical_nodes = ["order-svc", "postgres"]
        warning_nodes = ["notify-svc"]
        root_cause = "order-svc"
        incident_desc = "Database cascade from order service overload"
    elif scenario_cycle == 1:
        critical_nodes = ["payment-svc"]
        warning_nodes = ["postgres"]
        root_cause = "payment-svc"
        incident_desc = "Payment service memory leak"
    else:
        critical_nodes = ["search-svc", "elasticsearch"]
        warning_nodes = ["api-gw"]
        root_cause = "search-svc"
        incident_desc = "Search service CPU saturation"

    # Update node classifications
    for node_id, probs in data["nodeClassifications"].items():
        if is_critical and node_id in critical_nodes:
            probs["fault"] = random.uniform(0.85, 0.97)
            probs["healthy"] = round(1.0 - probs["fault"] - 0.02, 3)
            probs["attack"] = 0.02
        elif (is_building or is_recovery) and node_id in critical_nodes:
            fault_base = 0.65 if is_building else 0.50
            probs["fault"] = random.uniform(fault_base, fault_base + 0.15)
            probs["healthy"] = round(1.0 - probs["fault"] - 0.02, 3)
            probs["attack"] = 0.02
        elif is_incident and node_id in warning_nodes:
            probs["fault"] = random.uniform(0.30, 0.50)
            probs["healthy"] = round(1.0 - probs["fault"] - 0.01, 3)
            probs["attack"] = 0.01
        else:
            probs["healthy"] = random.uniform(0.92, 0.99)
            probs["fault"] = round((1.0 - probs["healthy"]) * 0.8, 3)
            probs["attack"] = round(1.0 - probs["healthy"] - probs["fault"], 3)

    # Update root cause and incident metadata
    if is_critical:
        data["rootCauseNode"] = root_cause
        data["rootCauseConfidence"] = random.uniform(0.88, 0.96)
        data["incidentType"] = "fault"
        data["graphClassification"]["fault"] = random.uniform(0.75, 0.90)
        data["graphClassification"]["healthy"] = round(0.08 - random.random() * 0.04, 3)
        data["graphClassification"]["attack"] = round(1.0 - data["graphClassification"]["fault"] - data["graphClassification"]["healthy"], 3)
    elif is_building or is_recovery:
        data["rootCauseNode"] = root_cause
        data["rootCauseConfidence"] = random.uniform(0.55, 0.75) if is_building else random.uniform(0.35, 0.55)
        data["incidentType"] = "fault"
        fault_level = random.uniform(0.45, 0.65) if is_building else random.uniform(0.20, 0.40)
        data["graphClassification"]["fault"] = fault_level
        data["graphClassification"]["healthy"] = round(0.90 - fault_level, 3)
        data["graphClassification"]["attack"] = round(1.0 - data["graphClassification"]["fault"] - data["graphClassification"]["healthy"], 3)
    else:
        data["rootCauseNode"] = root_cause
        data["rootCauseConfidence"] = random.uniform(0.08, 0.18)
        data["incidentType"] = "none"
        data["graphClassification"]["healthy"] = random.uniform(0.92, 0.98)
        data["graphClassification"]["fault"] = round((1.0 - data["graphClassification"]["healthy"]) * 0.7, 3)
        data["graphClassification"]["attack"] = round(1.0 - data["graphClassification"]["healthy"] - data["graphClassification"]["fault"], 3)

    # Update blast radius and causal chain
    if is_incident:
        data["blastRadius"] = critical_nodes + warning_nodes
        causal_chain = []
        for idx, node in enumerate([root_cause] + critical_nodes + warning_nodes):
            if node == root_cause:
                score = data["rootCauseConfidence"]
                status = "critical"
            elif node in critical_nodes:
                score = random.uniform(0.70, 0.88)
                status = "critical"
            elif node in warning_nodes:
                score = random.uniform(0.30, 0.60)
                status = "warning"
            else:
                continue

            if node not in [n["node"] for n in causal_chain]:
                causal_chain.append({"node": node, "causalScore": round(score, 3), "status": status})

        data["causalChain"] = sorted(causal_chain, key=lambda x: x["causalScore"], reverse=True)
    else:
        data["blastRadius"] = []
        data["causalChain"] = [
            {"node": node, "causalScore": round(random.uniform(0.05, 0.15), 3), "status": "healthy"}
            for node in [root_cause]
        ]

    data["inferenceMs"] = round(6 + random.random() * 5, 1)
    data["metadata"] = {
        "scenario": scenario_cycle + 1,
        "phase": "critical" if is_critical else "building" if is_building else "recovery" if is_recovery else "healthy",
        "description": incident_desc if is_incident else "All systems nominal"
    }
    return data


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
        data = _get_dynamic_fallback()

    # ── Merge GNN classifications into node statuses ─────────────────────────────
    # Fetch latest GNN inference to get real node classifications
    inference = await _fetch_from_gnn("/infer")
    if inference and "nodeClassifications" in inference:
        node_classifications = inference["nodeClassifications"]

        for node in data.get("nodes", []):
            node_id = node["id"]
            if node_id in node_classifications:
                probs = node_classifications[node_id]

                # Determine status from GNN classification probabilities
                # Attack is always critical
                if probs.get("attack", 0) > 0.30:
                    node["status"] = "critical"
                # High fault probability is critical
                elif probs.get("fault", 0) > 0.80:
                    node["status"] = "critical"
                # Medium fault probability is warning
                elif probs.get("fault", 0) > 0.40:
                    node["status"] = "warning"
                # Otherwise healthy
                else:
                    node["status"] = "healthy"

                # Store GNN probabilities for debugging
                node["gnn_probs"] = probs

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
    nodes = (data or _get_dynamic_fallback()).get("nodes", [])

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
    nodes = (data or _get_dynamic_fallback()).get("nodes", [])
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
    edges = (data or _get_dynamic_fallback()).get("edges", [])

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
        data = _get_dynamic_inference()

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
                topo_data = _get_dynamic_fallback()

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
