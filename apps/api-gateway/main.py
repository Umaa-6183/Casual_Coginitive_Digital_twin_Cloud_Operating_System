"""
CCDT API Gateway — main.py
────────────────────────────────────────────────────────────────────────────────
The single entry point for the CCDT API Gateway service.

Startup order:
  1. Load configuration from environment / .env
  2. Configure structured JSON logging
  3. Mount Prometheus metrics endpoint
  4. Register CORS middleware
  5. Register rate-limiting middleware
  6. Register JWT auth middleware
  7. Include all routers (topology, incidents, guardian, copilot, ebpf)
  8. Start background Kafka consumer (events → alert fanout)

Run locally:
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Environment variables:
  AUTH_DISABLED=true          Disable JWT auth (dev only)
  RATE_LIMIT_ENABLED=false    Disable rate limiting (dev only)
  GNN_SERVICE_URL             URL of Layer-2 GNN service  (default: http://layer2-cognitive:8001)
  GUARDIAN_SERVICE_URL        URL of Layer-3 Guardian     (default: http://layer3-guardian:8002)
  COPILOT_SERVICE_URL         URL of Layer-4 Co-Pilot     (default: http://layer4-copilot:8003)
  NERVOUS_SERVICE_URL         URL of Layer-1 Nervous Sys  (default: http://layer1-nervous:8000)
  KAFKA_BOOTSTRAP_SERVERS     Kafka bootstrap servers     (default: kafka:9092)
  JWT_SECRET                  JWT signing secret
  LOG_LEVEL                   Python logging level        (default: INFO)
  CORS_ORIGINS                Comma-separated allowed origins
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST,
)

from middleware.auth import AuthMiddleware
from middleware.rate_limit import RateLimitMiddleware, startup_rate_limiter, shutdown_rate_limiter
from routers import topology, incidents, guardian, copilot, ebpf, policies

# ─── Logging setup ────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, LOG_LEVEL, logging.INFO)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    cache_logger_on_first_use=True,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(message)s",
)

logger = logging.getLogger("ccdt.gateway")

# ─── Prometheus metrics ───────────────────────────────────────────────────────
REQUEST_COUNT = Counter(
    "ccdt_gateway_requests_total",
    "Total HTTP requests processed by the API Gateway",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "ccdt_gateway_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5],
)
ACTIVE_WS = Gauge(
    "ccdt_gateway_active_websockets",
    "Number of active WebSocket connections",
)
UPSTREAM_ERRORS = Counter(
    "ccdt_gateway_upstream_errors_total",
    "Errors from upstream services",
    ["service"],
)

# ─── CORS configuration ───────────────────────────────────────────────────────
_raw_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:5173,http://dashboard:3000",
)
CORS_ORIGINS: list[str] = [o.strip()
                           for o in _raw_origins.split(",") if o.strip()]

# ─── Lifespan: startup + shutdown ────────────────────────────────────────────


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan handler for startup and graceful shutdown."""
    logger.info("CCDT API Gateway starting up…")

    # Initialise SQLite database (creates tables if they don't exist)
    try:
        from database import db, init_db
        init_db()
        logger.info("SQLite database ready")
    except Exception as exc:
        logger.warning(
            "SQLite init failed: %s — continuing without persistence", exc)

    # Start Redis rate-limiter connection (if USE_REDIS=true)
    await startup_rate_limiter()

    # Start Kafka consumer to receive simulator/eBPF incidents
    kafka_task = None
    try:
        kafka_task = asyncio.create_task(_kafka_consumer())
        logger.info("Kafka consumer started (simulator + eBPF incidents)")
    except Exception as exc:
        logger.warning("Kafka consumer failed to start: %s", exc)

    logger.info(
        "CCDT API Gateway ready — auth=%s rate_limit=%s cors_origins=%d",
        "disabled" if os.getenv(
            "AUTH_DISABLED", "false") == "true" else "enabled",
        os.getenv("RATE_LIMIT_ENABLED", "true"),
        len(CORS_ORIGINS),
    )

    yield  # ← application is running

    # Graceful shutdown
    logger.info("CCDT API Gateway shutting down…")
    if kafka_task is not None:
        kafka_task.cancel()
    await shutdown_rate_limiter()
    logger.info("CCDT API Gateway shutdown complete")


# ─── FastAPI application ─────────────────────────────────────────────────────

app = FastAPI(
    title="CCDT API Gateway",
    description=(
        "Cognitive Digital Twin — Cloud Operating System API Gateway.\n\n"
        "Aggregates Layer 1 (eBPF), Layer 2 (Causal GNN), "
        "Layer 3 (RL Guardian) and Layer 4 (LLM Co-Pilot) into a single API surface."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ─── Middleware stack (outermost first) ───────────────────────────────────────

# 1. CORS — must be outermost so pre-flight OPTIONS requests are handled
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-RateLimit-Limit",
                    "X-RateLimit-Remaining", "X-RateLimit-Reset"],
)

# 2. Rate limiting
app.add_middleware(RateLimitMiddleware)

# 3. JWT authentication
app.add_middleware(AuthMiddleware)

# ─── Request telemetry middleware ─────────────────────────────────────────────


@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    """Record per-request Prometheus metrics and inject X-Request-ID."""
    import uuid

    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)

    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start

    # Normalise path for metric labels (avoid high cardinality from IDs)
    path = _normalise_path(request.url.path)

    REQUEST_COUNT.labels(
        method=request.method,
        path=path,
        status=response.status_code,
    ).inc()
    REQUEST_LATENCY.labels(method=request.method, path=path).observe(duration)

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{duration*1000:.1f}ms"
    return response


def _normalise_path(path: str) -> str:
    """
    Replace path-parameter segments with placeholders to avoid metric
    cardinality explosion.
    e.g. /api/v1/incidents/INC-2847 → /api/v1/incidents/{id}
    """
    import re
    # Replace INC-\d+ style IDs
    path = re.sub(r"/INC-\d+", "/{incident_id}", path)
    # Replace UUIDs
    path = re.sub(
        r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "/{uuid}",
        path,
    )
    # Replace pure numeric segments
    path = re.sub(r"/\d+", "/{id}", path)
    return path


# ─── Routers ──────────────────────────────────────────────────────────────────

app.include_router(topology.router)
app.include_router(incidents.router)
app.include_router(guardian.router)
app.include_router(copilot.router)
app.include_router(ebpf.router)
app.include_router(policies.router)

# ─── Health / readiness ───────────────────────────────────────────────────────


@app.get("/health", tags=["health"], summary="Liveness probe")
async def health() -> JSONResponse:
    """Kubernetes liveness probe — returns 200 when the process is running."""
    return JSONResponse(content={
        "status":    "ok",
        "service":   "ccdt-api-gateway",
        "version":   "1.0.0",
        "timestamp": int(time.time()),
    })


@app.get("/ready", tags=["health"], summary="Readiness probe")
async def ready() -> JSONResponse:
    """
    Kubernetes readiness probe — returns 200 when all dependencies are
    reachable, 503 when the service should not receive traffic.
    """
    import httpx

    checks: dict[str, str] = {}
    all_ready = True

    upstream_urls = {
        "layer1_nervous":  os.getenv("NERVOUS_SERVICE_URL",  "http://layer1-nervous:8000") + "/health",
        "layer2_cognitive": os.getenv("GNN_SERVICE_URL",      "http://layer2-cognitive:8001") + "/health",
        "layer3_guardian": os.getenv("GUARDIAN_SERVICE_URL", "http://layer3-guardian:8002") + "/health",
        "layer4_copilot":  os.getenv("COPILOT_SERVICE_URL",  "http://layer4-copilot:8003") + "/health",
    }

    for name, url in upstream_urls.items():
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(url)
                checks[name] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
        except Exception:
            checks[name] = "unreachable"
            # Don't mark unready just because upstreams are down in dev
            if os.getenv("STRICT_READINESS", "false").lower() == "true":
                all_ready = False

    status_code = 200 if all_ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status":     "ready" if all_ready else "degraded",
            "checks":     checks,
            "timestamp":  int(time.time()),
        },
    )


# ─── Prometheus metrics endpoint ──────────────────────────────────────────────

@app.get("/metrics", tags=["observability"], summary="Prometheus metrics")
async def metrics() -> PlainTextResponse:
    """Expose Prometheus metrics for scraping."""
    return PlainTextResponse(
        content=generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )


# ─── Global exception handlers ────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "detail": f"Path '{request.url.path}' not found",
            "docs":   "/docs",
        },
    )


@app.exception_handler(405)
async def method_not_allowed_handler(request: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=405,
        content={
            "detail": f"Method '{request.method}' not allowed on '{request.url.path}'"},
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s: %s",
                 request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error — see gateway logs"},
    )


# ─── Root ─────────────────────────────────────────────────────────────────────

@app.get("/", tags=["root"], include_in_schema=False)
async def root() -> JSONResponse:
    return JSONResponse(content={
        "service":    "CCDT API Gateway",
        "version":    "1.0.0",
        "docs":       "/docs",
        "health":     "/health",
        "metrics":    "/metrics",
        "api_prefix": "/api/v1",
        "layers": {
            "L1_nervous":   os.getenv("NERVOUS_SERVICE_URL",  "http://layer1-nervous:8000"),
            "L2_cognitive": os.getenv("GNN_SERVICE_URL",      "http://layer2-cognitive:8001"),
            "L3_guardian":  os.getenv("GUARDIAN_SERVICE_URL", "http://layer3-guardian:8002"),
            "L4_copilot":   os.getenv("COPILOT_SERVICE_URL",  "http://layer4-copilot:8003"),
        },
    })


# ─── Background: Kafka consumer (optional) ────────────────────────────────────

async def _kafka_consumer() -> None:
    """
    Background task: consume events from Kafka topics.
    - ccdt.ebpf.events    → topology_update, ebpf_event (from Layer-1 or simulator)
    - ccdt.incidents      → incident_created (from simulator)
    Ingests incident_created messages into the incidents store.
    """
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    topic_ebpf = os.getenv("KAFKA_TOPIC_EBPF",      "ccdt.ebpf.events")
    topic_incidents = os.getenv("KAFKA_TOPIC_INCIDENTS",  "ccdt.incidents")

    # Import here to allow gateway to start even if kafka is temporarily down
    from routers.incidents import ingest_simulator_incident

    while True:
        try:
            from aiokafka import AIOKafkaConsumer
            consumer = AIOKafkaConsumer(
                topic_ebpf,
                topic_incidents,
                bootstrap_servers=bootstrap,
                group_id="ccdt-api-gateway-v2",
                auto_offset_reset="latest",
                value_deserializer=lambda m: m.decode("utf-8"),
            )
            await consumer.start()
            logger.info("Kafka consumer ready — topics: %s, %s",
                        topic_ebpf, topic_incidents)

            async for msg in consumer:
                try:
                    event = json.loads(msg.value)
                    msg_type = event.get("msg_type") or event.get("type", "")

                    if msg_type == "incident_created":
                        ingest_simulator_incident(event)
                    elif msg_type in ("chaos_run_start", "chaos_run_end"):
                        # Enhancement 2: log chaos runs to SQLite
                        try:
                            from database import db as _db
                            if msg_type == "chaos_run_start":
                                _db.save_chaos_run({
                                    "scenario_id":    event.get("scenario_id", ""),
                                    "scenario_title": event.get("scenario_title", event.get("scenario_id", "")),
                                    "type":           event.get("type", "fault"),
                                    "started_at":     event.get("started_at", int(time.time())),
                                    "incident_id":    event.get("incident_id", ""),
                                })
                            else:
                                # Update existing chaos run with resolution data
                                with _db.__class__.__module__.__class__ as _:
                                    pass  # chaos_run_end handled by scenario resolution
                        except Exception as exc:
                            logger.debug("Chaos run record failed: %s", exc)
                    else:
                        logger.debug("Kafka [%s]: %s", msg.topic, msg_type)

                except Exception as exc:
                    logger.warning("Kafka message parse error: %s", exc)

        except Exception as exc:
            logger.warning("Kafka consumer error (%s) — retrying in 10s", exc)
            await asyncio.sleep(10)


# ─── Dev entrypoint ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "true").lower() == "true",
        log_level=LOG_LEVEL.lower(),
        access_log=True,
    )
