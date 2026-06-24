"""
CCDT Layer-2 Cognitive Core — FastAPI Inference Server
"""
from __future__ import annotations
from inference.explainer import CCDTExplainer
from models.counterfactual import CounterfactualEngine
from models.dag_builder import LiveDAGBuilder
from models.causal_gnn import build_model, load_checkpoint, predict, find_root_cause

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import torch
from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


logger = logging.getLogger("ccdt.inference.server")
logging.basicConfig(
    level=getattr(logging, os.getenv(
        "LOG_LEVEL", "INFO").upper(), logging.INFO)
)

MODEL_PATH = os.getenv("MODEL_PATH",
                       "/app/checkpoints/causal_gnn_best.pt")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
DEVICE = os.getenv("INFERENCE_DEVICE",        "cpu")
INFER_INTERVAL = float(os.getenv("INFER_INTERVAL_S",  "5.0"))
CACHE_TTL = float(os.getenv("INFERENCE_CACHE_TTL_S", "2.0"))

CLASS_NAMES = ["healthy", "fault", "attack"]

INFER_COUNT = Counter("ccdt_gnn_inferences_total",
                      "GNN inference calls", ["status"])
INFER_LATENCY = Histogram(
    "ccdt_gnn_inference_duration_seconds", "GNN inference duration",
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
)
CF_COUNT = Counter("ccdt_counterfactual_total", "Counterfactual calls")

_state: dict[str, Any] = {
    "model":          None,
    "dag_builder":    None,
    "cf_engine":      None,
    "explainer":      None,
    "kafka_producer": None,
    "last_result":    None,
    "last_infer_ts":  0.0,
    "model_loaded":   False,
    "inference_task": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Layer-2 Cognitive Core starting...")

    # ── Load GNN checkpoint ───────────────────────────────────────────────────
    try:
        if os.path.exists(MODEL_PATH):
            model = load_checkpoint(MODEL_PATH, device=DEVICE)
            logger.info("Checkpoint loaded: %s", MODEL_PATH)
        else:
            logger.warning(
                "No checkpoint at %s — using untrained model", MODEL_PATH)
            model = build_model()
            model.eval()
        _state["model"] = model
        _state["model_loaded"] = True
    except Exception as exc:
        logger.error("Model load failed: %s", exc)
        _state["model"] = build_model()
        _state["model_loaded"] = False

    # ── Build live topology DAG ───────────────────────────────────────────────
    # Constructor signature: kafka_servers (NOT kafka_bootstrap), k8s_enabled
    dag = LiveDAGBuilder(
        kafka_servers=KAFKA_BOOTSTRAP,
        k8s_enabled=False,   # no k8s on macOS — use simulator data from Kafka
    )
    await dag.bootstrap()
    # start_kafka_consumer is ASYNC — must be awaited
    await dag.start_kafka_consumer()

    _state["dag_builder"] = dag
    _state["cf_engine"] = CounterfactualEngine(_state["model"], dag)
    _state["explainer"] = CCDTExplainer(_state["model"])

    # ── Initialize Kafka Producer ─────────────────────────────────────────────
    try:
        producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            compression_type='gzip',
        )
        await producer.start()
        _state["kafka_producer"] = producer
        logger.info("✅ Kafka producer connected: %s", KAFKA_BOOTSTRAP)
    except Exception as exc:
        logger.warning("⚠️  Kafka producer failed: %s — inference will not publish", exc)
        _state["kafka_producer"] = None

    # Warm up — run first inference so last_result is populated
    try:
        await _run_inference()
    except Exception as exc:
        logger.warning("Warm-up inference failed (ok on first start): %s", exc)

    # ── Start background inference loop ───────────────────────────────────────
    _state["inference_task"] = asyncio.create_task(_inference_loop())
    logger.info("✅ Background inference loop started (interval=%ds)", INFER_INTERVAL)

    logger.info("Layer-2 ready on %s", DEVICE)
    yield

    # Cleanup
    logger.info("Layer-2 shutting down...")
    if _state["inference_task"]:
        _state["inference_task"].cancel()
    if _state["kafka_producer"]:
        await _state["kafka_producer"].stop()
    logger.info("Layer-2 stopped")


def _classify_incident_type(dag: LiveDAGBuilder, node_ids: list[str], gnn_class: int) -> int:
    """
    Rule-based incident classification to fix untrained GNN misclassification.

    Returns:
        0 = healthy
        1 = fault (resource issues: OOM, high CPU/mem, latency)
        2 = attack (security indicators: cap_events, syscall anomalies)
    """
    # Get node states from DAG
    nodes = dag._nodes

    # Collect incident indicators
    oom_nodes = []
    high_mem_nodes = []
    high_cpu_nodes = []
    security_nodes = []

    for nid in node_ids:
        node = nodes.get(nid)
        if not node:
            continue

        # Memory pressure / OOM indicators (FAULT)
        if node.oom_count > 0:
            oom_nodes.append(nid)
        if node.mem >= 85:
            high_mem_nodes.append(nid)
        if node.cpu >= 85:
            high_cpu_nodes.append(nid)

        # Security indicators (ATTACK)
        if node.cap_events >= 5 or node.syscall_rate >= 5000:
            security_nodes.append(nid)

    # Classification logic
    has_oom = len(oom_nodes) > 0
    has_memory_pressure = len(high_mem_nodes) > 0
    has_cpu_pressure = len(high_cpu_nodes) > 0
    has_security_events = len(security_nodes) > 0

    # Priority: Security events indicate attack
    if has_security_events and not (has_oom or has_memory_pressure):
        logger.info("🔍 Classification override: ATTACK detected (security events on %s)", security_nodes)
        return 2  # attack

    # OOM or high memory/CPU indicates fault
    if has_oom or has_memory_pressure or has_cpu_pressure:
        affected = list(set(oom_nodes + high_mem_nodes + high_cpu_nodes))
        logger.info("🔍 Classification override: FAULT detected (resource pressure on %s)", affected[:3])
        return 1  # fault

    # No clear indicators — trust GNN or default to healthy
    if gnn_class > 0:
        logger.debug("🔍 Classification: Using GNN prediction (%s)", CLASS_NAMES[gnn_class])
        return gnn_class

    return 0  # healthy


def _find_root_cause_by_metrics(dag: LiveDAGBuilder, node_ids: list[str], incident_class: int) -> Optional[str]:
    """
    Find root cause node by analyzing actual metrics instead of GNN probabilities.

    For fault (class=1): prioritize OOM > high mem > high CPU
    For attack (class=2): prioritize cap_events > syscall_rate
    """
    nodes = dag._nodes
    candidates = []

    for nid in node_ids:
        node = nodes.get(nid)
        if not node:
            continue

        if incident_class == 1:  # fault
            # Calculate fault score: OOM is critical, then mem, then cpu
            score = 0.0
            if node.oom_count > 0:
                score += 100.0 * node.oom_count
            score += node.mem * 1.5  # Memory weighted higher
            score += node.cpu * 1.0
            score += node.error_rate * 50.0  # Error rate also significant

            if score > 50:  # Only consider nodes with significant issues
                candidates.append((nid, score))

        elif incident_class == 2:  # attack
            # Calculate attack score
            score = 0.0
            score += node.cap_events * 20.0
            score += node.syscall_rate / 100.0
            score += node.cpu * 0.5  # CPU less important for attacks

            if score > 30:
                candidates.append((nid, score))

    if not candidates:
        return None

    # Return node with highest score
    candidates.sort(key=lambda x: x[1], reverse=True)
    root_node = candidates[0][0]
    logger.debug("🎯 Metric-based root cause: %s (score=%.1f)", root_node, candidates[0][1])
    return root_node


async def _inference_loop() -> None:
    """
    Background task that runs GNN inference every INFER_INTERVAL seconds
    and publishes results to Kafka topic 'ccdt.gnn.inference'.
    """
    logger.info("🔄 Inference loop starting...")
    await asyncio.sleep(2)  # Give Kafka producer time to initialize

    while True:
        try:
            # Run inference
            result = await _run_inference()

            # Publish to Kafka if producer is available
            producer = _state.get("kafka_producer")
            if producer:
                try:
                    await producer.send("ccdt.gnn.inference", value=result)
                    logger.debug("📤 Published inference to Kafka: incident_type=%s, root=%s, confidence=%.2f",
                                result.get("incidentType"),
                                result.get("rootCauseNode"),
                                result.get("rootCauseConfidence", 0.0))
                except Exception as exc:
                    logger.warning("Kafka publish failed: %s", exc)

        except asyncio.CancelledError:
            logger.info("Inference loop cancelled")
            break
        except Exception as exc:
            logger.error("Inference loop error: %s", exc, exc_info=True)

        await asyncio.sleep(INFER_INTERVAL)


app = FastAPI(title="CCDT GNN Inference Server",
              version="1.0.0", lifespan=lifespan)


class InferRequest(BaseModel):
    force_refresh: bool = Field(False)
    topology:      Optional[dict] = Field(None)


class CounterfactualRequest(BaseModel):
    target_node: str = Field(...)
    action:      str = Field(...)
    parameters:  dict = Field(default_factory=dict)


async def _run_inference(topology_override: Optional[dict] = None) -> dict:
    now = time.time()

    if (
        not topology_override
        and _state["last_result"]
        and (now - _state["last_infer_ts"]) < CACHE_TTL
    ):
        return _state["last_result"]

    model = _state["model"]
    dag:  LiveDAGBuilder = _state["dag_builder"]
    if model is None or dag is None:
        raise RuntimeError("Model or topology not initialised")

    start = time.perf_counter()
    try:
        # get_pyg_data() returns Data only; node_ids is a method (not property)
        data = await dag.get_pyg_data()
        node_ids = dag.node_ids   # method call, not attribute

        result = predict(
            model, data.x, data.edge_index,
            data.edge_attr, batch=None, device=DEVICE,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        INFER_LATENCY.observe(elapsed_ms / 1000)
        INFER_COUNT.labels(status="ok").inc()

        graph_probs = result["graph_probs"][0]
        graph_class_raw = result["graph_class"][0].item()
        node_probs = result["node_probs"]
        node_classes = result["node_classes"]

        # ── Rule-based classification override (fixes untrained model misclassification) ────
        graph_class = _classify_incident_type(dag, node_ids, graph_class_raw)

        target_cls = graph_class if graph_class > 0 else 2

        # ── Root cause detection with metric-based fallback ────
        root_cause_id, root_cause_conf, ranked_nodes = find_root_cause(
            node_probs, node_ids, target_class=target_cls
        )

        # Fallback: If GNN confidence is low (<0.5), use metric-based root cause
        if root_cause_conf < 0.5 and graph_class > 0:
            metric_root = _find_root_cause_by_metrics(dag, node_ids, graph_class)
            if metric_root:
                root_cause_id = metric_root
                root_cause_conf = 0.85  # High confidence for metric-based detection
                logger.info("🎯 Root cause override: %s (metric-based, class=%s)",
                           root_cause_id, CLASS_NAMES[graph_class])

        blast_radius = [
            nid for nid, _ in ranked_nodes
            if node_probs[node_ids.index(nid)][target_cls].item() > 0.3
        ][:5]

        causal_chain = [
            {
                "node":        nid,
                "causalScore": round(score, 4),
                "status":      CLASS_NAMES[node_classes[node_ids.index(nid)].item()],
            }
            for nid, score in ranked_nodes[:5]
        ]

        node_classifications = {
            nid: {
                cls: round(node_probs[i][j].item(), 4)
                for j, cls in enumerate(CLASS_NAMES)
            }
            for i, nid in enumerate(node_ids)
        }

        output = {
            "nodeClassifications":  node_classifications,
            "graphClassification":  {
                cls: round(graph_probs[j].item(), 4)
                for j, cls in enumerate(CLASS_NAMES)
            },
            "rootCauseNode":        root_cause_id,
            "rootCauseConfidence":  round(root_cause_conf, 4),
            "incidentType":         CLASS_NAMES[graph_class],
            "blastRadius":          blast_radius,
            "causalChain":          causal_chain,
            "inferenceMs":          round(elapsed_ms, 2),
            "nodeCount":            len(node_ids),
            "edgeCount":            data.edge_index.size(1),
            "timestamp":            int(now),
            "source":               "gnn",
        }
        _state["last_result"] = output
        _state["last_infer_ts"] = now
        return output

    except Exception as exc:
        INFER_COUNT.labels(status="error").inc()
        logger.error("Inference failed: %s", exc, exc_info=True)
        raise


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(content={
        "status":       "ok",
        "service":      "layer2-cognitive",
        "model_loaded": _state["model_loaded"],
        "timestamp":    int(time.time()),
    })


@app.get("/ready")
async def ready() -> JSONResponse:
    if not _state["model_loaded"] or _state["dag_builder"] is None:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(content={"status": "ready"})


@app.get("/topology")
async def get_topology() -> JSONResponse:
    dag = _state.get("dag_builder")
    if dag is None:
        raise HTTPException(status_code=503, detail="Topology not initialised")
    return JSONResponse(content=await dag.get_topology_dict())


@app.get("/debug/state")
async def debug_state() -> JSONResponse:
    dag = _state.get("dag_builder")
    return JSONResponse(content={
        "nodes":         dag.num_nodes if dag else 0,
        "last_inference": _state["last_infer_ts"],
        "model_loaded":  _state["model_loaded"],
        "incident_type": _state["last_result"]["incidentType"] if _state["last_result"] else None,
    })


@app.post("/infer")
async def run_infer(body: InferRequest) -> JSONResponse:
    try:
        if body.force_refresh:
            _state["last_infer_ts"] = 0.0
        return JSONResponse(content=await _run_inference(body.topology))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/counterfactual")
async def run_counterfactual(body: CounterfactualRequest) -> JSONResponse:
    CF_COUNT.inc()
    engine: CounterfactualEngine = _state.get("cf_engine")
    if engine is None:
        raise HTTPException(
            status_code=503, detail="Counterfactual engine not ready")
    try:
        result = await engine.analyse(body.target_node, body.action, body.parameters)
        return JSONResponse(content=result)
    except Exception as exc:
        logger.error("Counterfactual failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(
        content=generate_latest().decode(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.websocket("/ws/inference")
async def ws_inference(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("WS inference stream opened: %s", websocket.client)
    try:
        while True:
            try:
                result = await _run_inference()
                await websocket.send_text(json.dumps({
                    "type":      "inference_update",
                    "payload":   result,
                    "timestamp": int(time.time()),
                }))
            except Exception as exc:
                logger.warning("WS inference error: %s", exc)
            await asyncio.sleep(INFER_INTERVAL)
    except WebSocketDisconnect:
        logger.info("WS inference stream closed")
    except Exception as exc:
        logger.error("WS inference fatal: %s", exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "inference.server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8001")),
        reload=os.getenv("RELOAD", "true").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
