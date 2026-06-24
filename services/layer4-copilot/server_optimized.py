"""
CCDT Layer-4 Co-Pilot — Optimized FastAPI Server
═══════════════════════════════════════════════════

Performance Enhancements:
✓ Intelligent response caching (sub-100ms for cached)
✓ Parallel AI provider execution
✓ WebSocket real-time updates
✓ Autonomous recovery triggers
✓ Mock UI synchronization
✓ Prometheus metrics

Usage:
  uvicorn server_optimized:app --host 0.0.0.0 --port 8003 --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse

# Import existing components
from context_builder import ClusterContextBuilder
from copilot import ToolExecutor

# Import new optimizations
from optimized_copilot import OptimizedCoPilot
from realtime_sync import (
    RealTimeSyncManager,
    IncidentUpdate,
    NodeHealthUpdate,
    get_sync_manager,
)

logger = logging.getLogger("ccdt.copilot.server")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

# ─── Configuration ─────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
KAFKA_TOPIC_INFER = os.getenv("KAFKA_TOPIC_INFER", "ccdt.gnn.inference")
KAFKA_TOPIC_INCIDENTS = os.getenv("KAFKA_TOPIC_INCIDENTS", "ccdt.incidents")
AUTO_RECOVERY_ENABLED = os.getenv("AUTO_RECOVERY_ENABLED", "true").lower() == "true"

# ─── Prometheus Metrics ────────────────────────────────────────────────────────
CHAT_REQUESTS = Counter("ccdt_copilot_chat_requests_total", "Chat requests", ["type"])
CHAT_LATENCY = Histogram(
    "ccdt_copilot_chat_latency_seconds",
    "Chat latency",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)
CACHE_HITS = Counter("ccdt_copilot_cache_hits_total", "Cache hits")
CACHE_MISSES = Counter("ccdt_copilot_cache_misses_total", "Cache misses")
AUTO_RECOVERIES = Counter("ccdt_copilot_auto_recoveries_total", "Autonomous recoveries executed")
WS_CONNECTIONS = Counter("ccdt_copilot_ws_connections_total", "WebSocket connections", ["status"])

# ─── Pydantic Models ───────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    context: Optional[dict] = Field(default=None)
    stream: bool = Field(default=False)


class StatsResponse(BaseModel):
    cache: dict
    sync: dict
    providers: list[str]
    auto_recovery_enabled: bool


# ─── Kafka Consumers ───────────────────────────────────────────────────────────


async def _kafka_inference_consumer(copilot: OptimizedCoPilot, sync_mgr: RealTimeSyncManager) -> None:
    """
    Monitor GNN inference results and trigger autonomous recovery.

    Flow:
    1. GNN publishes inference → ccdt.gnn.inference
    2. We detect critical incidents (confidence > 75%)
    3. Trigger autonomous recovery if enabled
    4. Broadcast update to all UIs in real-time
    """
    backoff = 5.0
    max_backoff = 120.0

    while True:
        try:
            from aiokafka import AIOKafkaConsumer

            consumer = AIOKafkaConsumer(
                KAFKA_TOPIC_INFER,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="ccdt-copilot-autonomous",
                auto_offset_reset="latest",
                value_deserializer=lambda m: json.loads(m.decode()),
                metadata_max_age_ms=30_000,
            )
            await consumer.start()
            logger.info("Kafka inference consumer started — autonomous recovery: %s", AUTO_RECOVERY_ENABLED)
            backoff = 5.0

            try:
                async for msg in consumer:
                    incident_data = msg.value
                    if not isinstance(incident_data, dict):
                        continue

                    incident_type = incident_data.get("incidentType", "healthy")
                    if incident_type == "healthy":
                        continue

                    confidence = incident_data.get("rootCauseConfidence", 0)
                    root_cause = incident_data.get("rootCauseNode", "unknown")

                    logger.info(
                        "Incident detected: %s on %s (%.0f%% confidence)",
                        incident_type, root_cause, confidence * 100
                    )

                    # Build incident update
                    incident = IncidentUpdate(
                        incident_id=incident_data.get("id", f"inc-{int(time.time())}"),
                        status="active",
                        severity="critical" if confidence > 0.8 else "warning",
                        root_cause=root_cause,
                        confidence=confidence,
                        blast_radius=incident_data.get("blastRadius", []),
                        timestamp=incident_data.get("timestamp", ""),
                        layer_source=2,  # From GNN
                    )

                    # Broadcast to UIs immediately
                    await sync_mgr.broadcast_incident_update(incident)

                    # Check autonomous recovery
                    if AUTO_RECOVERY_ENABLED and confidence >= 0.75:
                        recovery_result = await copilot.trigger_autonomous_recovery(incident_data)

                        if recovery_result:
                            AUTO_RECOVERIES.inc()

                            # Update incident status
                            incident.status = "resolving"
                            incident.auto_recovered = True
                            incident.recovery_action = recovery_result.get("action_type")
                            await sync_mgr.broadcast_incident_update(incident)

            finally:
                try:
                    await consumer.stop()
                except:
                    pass

        except asyncio.CancelledError:
            logger.info("Kafka consumer cancelled")
            return
        except Exception as exc:
            logger.warning("Kafka consumer error: %s — reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


async def _kafka_incidents_consumer(sync_mgr: RealTimeSyncManager) -> None:
    """
    Monitor incident lifecycle events and broadcast to UIs.

    Syncs: ccdt.incidents → WebSocket → Dashboard + Mock UI
    """
    backoff = 5.0
    max_backoff = 120.0

    while True:
        try:
            from aiokafka import AIOKafkaConsumer

            consumer = AIOKafkaConsumer(
                KAFKA_TOPIC_INCIDENTS,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="ccdt-copilot-incidents",
                auto_offset_reset="latest",
                value_deserializer=lambda m: json.loads(m.decode()),
            )
            await consumer.start()
            logger.info("Kafka incidents consumer started")
            backoff = 5.0

            try:
                async for msg in consumer:
                    incident_data = msg.value
                    if not isinstance(incident_data, dict):
                        continue

                    # Build incident update
                    incident = IncidentUpdate(
                        incident_id=incident_data.get("id", "unknown"),
                        status=incident_data.get("status", "active"),
                        severity=incident_data.get("severity", "warning"),
                        root_cause=incident_data.get("root_cause", "unknown"),
                        confidence=incident_data.get("confidence", 0),
                        blast_radius=incident_data.get("affected", []),
                        timestamp=incident_data.get("created_at", ""),
                        layer_source=incident_data.get("layer_source", 1),
                        auto_recovered=incident_data.get("auto_resolved", False),
                        mttr_seconds=incident_data.get("mttr_seconds"),
                    )

                    await sync_mgr.broadcast_incident_update(incident)

            finally:
                try:
                    await consumer.stop()
                except:
                    pass

        except asyncio.CancelledError:
            logger.info("Incidents consumer cancelled")
            return
        except Exception as exc:
            logger.warning("Incidents consumer error: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


# ─── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize services and background tasks."""
    logger.info("CCDT Co-Pilot (Optimized) starting...")

    # Initialize components
    ctx_builder = ClusterContextBuilder()
    tool_executor = ToolExecutor()
    copilot = OptimizedCoPilot(ctx_builder, tool_executor)
    sync_mgr = get_sync_manager()

    # Store in app state
    app.state.copilot = copilot
    app.state.sync_mgr = sync_mgr
    app.state.ctx_builder = ctx_builder

    # Set WebSocket broadcast callback
    copilot.set_websocket_broadcast(sync_mgr.broadcast_recovery_action)

    # Start Kafka consumers
    tasks = [
        asyncio.create_task(_kafka_inference_consumer(copilot, sync_mgr)),
        asyncio.create_task(_kafka_incidents_consumer(sync_mgr)),
    ]
    app.state.kafka_tasks = tasks

    logger.info("✅ CCDT Co-Pilot ready — autonomous recovery: %s", AUTO_RECOVERY_ENABLED)

    yield

    # Cleanup
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("CCDT Co-Pilot stopped")


# ─── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="CCDT Co-Pilot (Optimized)",
    description="Layer-4 AI Operator with intelligent caching and autonomous recovery",
    version="8.0.0-optimized",
    lifespan=lifespan,
)


# ─── Health & Stats ────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint."""
    copilot_stats = app.state.copilot.get_stats()
    sync_stats = app.state.sync_mgr.get_stats()

    return JSONResponse(content={
        "status": "ok",
        "service": "layer4-copilot-optimized",
        "cache_hit_rate": copilot_stats.get("hit_rate_pct", 0),
        "ws_connections": sync_stats.get("ws_connections", 0),
        "auto_recovery_enabled": AUTO_RECOVERY_ENABLED,
        "providers": copilot_stats.get("provider_names", []),
        "timestamp": int(time.time()),
    })


@app.get("/stats")
async def stats() -> JSONResponse:
    """Detailed statistics."""
    copilot_stats = app.state.copilot.get_stats()
    sync_stats = app.state.sync_mgr.get_stats()

    return JSONResponse(content={
        "cache": {
            "hits": copilot_stats.get("hits", 0),
            "misses": copilot_stats.get("misses", 0),
            "hit_rate_pct": copilot_stats.get("hit_rate_pct", 0),
            "size": copilot_stats.get("cache_size", 0),
        },
        "sync": sync_stats,
        "providers": copilot_stats.get("provider_names", []),
        "auto_recovery": {
            "enabled": AUTO_RECOVERY_ENABLED,
            "total_executed": copilot_stats.get("auto_fixes_total", 0),
        },
    })


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus metrics."""
    return PlainTextResponse(
        content=generate_latest().decode(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ─── Chat Endpoints ────────────────────────────────────────────────────────────


@app.post("/chat")
async def chat(body: ChatRequest) -> JSONResponse:
    """
    Fast chat with intelligent caching.

    Performance targets:
    - Cached responses: <100ms
    - Template responses: <200ms
    - AI responses: <2000ms
    """
    t0 = time.perf_counter()
    CHAT_REQUESTS.labels(type="non_stream").inc()

    try:
        result = await app.state.copilot.fast_chat(
            session_id=body.session_id,
            user_message=body.message,
            context=body.context,
        )

        elapsed = time.perf_counter() - t0
        CHAT_LATENCY.observe(elapsed)

        # Track cache metrics
        if result.get("cached"):
            CACHE_HITS.inc()
        else:
            CACHE_MISSES.inc()

        return JSONResponse(content=result)

    except Exception as exc:
        logger.error("Chat error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest) -> StreamingResponse:
    """Streaming chat endpoint."""
    CHAT_REQUESTS.labels(type="stream").inc()

    async def generator():
        try:
            result = await app.state.copilot.fast_chat(
                session_id=body.session_id,
                user_message=body.message,
                context=body.context,
            )

            # Stream response word-by-word
            reply = result.get("reply", "")
            words = reply.split(" ")

            for i, word in enumerate(words):
                text = word + (" " if i < len(words) - 1 else "")
                yield f'data: {json.dumps({"type": "text_delta", "text": text})}\n\n'
                await asyncio.sleep(0.01)  # Smooth streaming

            yield f'data: {json.dumps({"type": "done", "model": result.get("model_used"), "cached": result.get("cached")})}\n\n'

        except Exception as exc:
            yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ─── WebSocket Endpoint ────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket for real-time updates.

    Messages:
    - incident_update: New/updated incident
    - node_health: Node health change
    - recovery_action: Guardian action executed
    - topology_update: Full topology refresh
    - ui_refresh: Trigger UI auto-refresh
    """
    await websocket.accept()
    sync_mgr = app.state.sync_mgr
    sync_mgr.add_websocket(websocket)
    WS_CONNECTIONS.labels(status="connected").inc()

    try:
        # Send initial state
        await websocket.send_json({
            "type": "connected",
            "message": "CCDT Co-Pilot WebSocket connected",
            "auto_recovery_enabled": AUTO_RECOVERY_ENABLED,
            "timestamp": int(time.time()),
        })

        # Keep connection alive
        while True:
            try:
                # Receive ping/keepalive messages
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send keepalive
                await websocket.send_json({"type": "keepalive"})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        sync_mgr.remove_websocket(websocket)
        WS_CONNECTIONS.labels(status="disconnected").inc()


# ─── Cache Management ──────────────────────────────────────────────────────────


@app.post("/cache/clear")
async def clear_cache() -> JSONResponse:
    """Clear response cache."""
    result = app.state.copilot.clear_cache()
    return JSONResponse(content=result)


@app.get("/cache/stats")
async def cache_stats() -> JSONResponse:
    """Get cache statistics."""
    stats = app.state.copilot.get_stats()
    return JSONResponse(content={
        "cache": {
            "hits": stats.get("hits", 0),
            "misses": stats.get("misses", 0),
            "hit_rate_pct": stats.get("hit_rate_pct", 0),
            "size": stats.get("cache_size", 0),
        }
    })


# ─── Sync Status ───────────────────────────────────────────────────────────────


@app.get("/sync/health")
async def sync_health() -> JSONResponse:
    """Check connectivity to external services."""
    health_status = await app.state.sync_mgr.health_check()
    return JSONResponse(content=health_status)


# ─── Main ──────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server_optimized:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8003")),
        reload=os.getenv("RELOAD", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
