"""
CCDT Business Facade — Mock API Backend (v2 Final — Fixed)
================================================================================
Connects the NexaOps SaaS Portal (Screen 1) to:
  • demo-postgres  — internal Docker port 5432
  • demo-redis     — internal Docker port 6379

PORT NOTE:
  Inside Docker, services communicate on CONTAINER-internal ports.
    demo-postgres → port 5432  (host sees 5433 — irrelevant inside Docker)
    demo-redis    → port 6379  (host sees 6380 — irrelevant inside Docker)

Endpoints:
  GET  /api/health                   — postgres + redis liveness
  GET  /api/dashboard                — KPI stats (revenue, sessions, latency…)
  GET  /api/orders?limit=N           — recent orders from postgres
  GET  /api/inventory                — stock levels from postgres
  POST /api/login                    — create Redis session
  POST /api/logout                   — destroy Redis session
  POST /api/admin/reseed-inventory   — reset all stock to healthy levels
  GET  /ready                        — readiness probe for Docker healthcheck

Run inside Docker as `mock-api` service on port 8089.
nginx proxies /api/* → http://mock-api:8089
"""

from __future__ import annotations
from fastapi import Request

import os
import time
import uuid
import random
import asyncio
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mock-api")

# ── Config ─────────────────────────────────────────────────────────────────────
# ALWAYS use container-internal ports (5432 / 6379), never host-mapped ports.
PG_DSN = os.getenv(
    "PG_DSN",    "postgresql://ccdt:ccdt@demo-postgres:5432/ccdt")
REDIS_URL = os.getenv("REDIS_URL", "redis://demo-redis:6379/0")

# CCDT Integration — Phase 2.3
CCDT_API_GATEWAY = os.getenv("CCDT_API_GATEWAY", "http://api-gateway:8000")
CCDT_INCIDENT_POLL_ENABLED = os.getenv("CCDT_INCIDENT_POLL_ENABLED", "true").lower() == "true"

# ── Globals ────────────────────────────────────────────────────────────────────
pg_pool:      asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None
ccdt_active_incident: dict | None = None  # Cache for active incident
ccdt_last_check: float = 0.0  # Timestamp of last CCDT check


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global pg_pool, redis_client

    # PostgreSQL — retry up to 10 × with 3 s back-off
    for attempt in range(10):
        try:
            pg_pool = await asyncpg.create_pool(
                PG_DSN, min_size=2, max_size=10, command_timeout=5
            )
            await _init_schema()
            log.info("✓ PostgreSQL connected and schema ready")
            break
        except Exception as exc:
            log.warning("PG connect attempt %d/10: %s", attempt + 1, exc)
            await asyncio.sleep(3)
    else:
        log.error("PostgreSQL unavailable after 10 attempts — degraded mode")

    # Redis — retry up to 10 × with 3 s back-off
    for attempt in range(10):
        try:
            redis_client = aioredis.from_url(
                REDIS_URL, decode_responses=True, socket_connect_timeout=3
            )
            await redis_client.ping()
            log.info("✓ Redis connected")
            break
        except Exception as exc:
            log.warning("Redis connect attempt %d/10: %s", attempt + 1, exc)
            await asyncio.sleep(3)
    else:
        log.error("Redis unavailable after 10 attempts — degraded mode")

    yield   # ← app is running

    if pg_pool:
        await pg_pool.close()
    if redis_client:
        await redis_client.aclose()


app = FastAPI(title="CCDT Mock API", version="2.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Schema ─────────────────────────────────────────────────────────────────────
async def _init_schema() -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mock_orders (
                order_id      SERIAL          PRIMARY KEY,
                customer_name VARCHAR(60)     NOT NULL,
                product_name  VARCHAR(80)     NOT NULL,
                amount        NUMERIC(10, 2)  NOT NULL,
                status        VARCHAR(20)     NOT NULL DEFAULT 'pending',
                created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS mock_inventory (
                sku           VARCHAR(20)  PRIMARY KEY,
                quantity      INTEGER      NOT NULL CHECK (quantity >= 0),
                max_quantity  INTEGER      NOT NULL DEFAULT 1000
            );

            CREATE TABLE IF NOT EXISTS mock_metrics (
                id          SERIAL          PRIMARY KEY,
                ts          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
                metric_name VARCHAR(40)     NOT NULL,
                value       NUMERIC(12, 4)  NOT NULL
            );

            CREATE INDEX IF NOT EXISTS mock_metrics_ts_idx
                ON mock_metrics (metric_name, ts DESC);
        """)

        # Seed inventory if empty
        if await conn.fetchval("SELECT COUNT(*) FROM mock_inventory") == 0:
            await _seed_inventory(conn)

        # Seed orders if empty
        if await conn.fetchval("SELECT COUNT(*) FROM mock_orders") == 0:
            await _seed_orders(conn)

    log.info("Schema ready")


async def _seed_inventory(conn: asyncpg.Connection) -> None:
    await conn.executemany(
        "INSERT INTO mock_inventory (sku, quantity, max_quantity) "
        "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        [
            ("SKU-001", random.randint(450, 900), 1000),
            ("SKU-002", random.randint(250, 600),  800),
            ("SKU-003", random.randint(150, 400),  500),
            ("SKU-004", random.randint(80,  250),  300),
            ("SKU-005", random.randint(350, 800), 1000),
            ("SKU-006", random.randint(200, 500),  600),
            ("SKU-007", random.randint(100, 300),  400),
            ("SKU-008", random.randint(250, 700),  900),
            ("SKU-009", random.randint(40,  150),  200),
            ("SKU-010", random.randint(120, 400),  500),
            ("SKU-011", random.randint(30,  120),  150),
            ("SKU-012", random.randint(60,  250),  350),
        ],
    )


async def _seed_orders(conn: asyncpg.Connection) -> None:
    customers = [
        "Alice Sharma", "Bob Chen", "Carol Rodriguez", "Dave Kim",
        "Erin Patel", "Frank Mueller", "Grace Okonkwo", "Hiro Tanaka",
    ]
    products = [
        ("Compute Node v3",  4899.00),
        ("SSD Array 4TB",    1299.00),
        ("GPU Module H100",  9999.00),
        ("Memory DDR5 64GB",  349.00),
        ("Network Switch",    799.00),
        ("Power Supply 1kW",  199.00),
    ]
    statuses = ["fulfilled", "fulfilled",
                "fulfilled", "pending", "pending", "failed"]

    rows = []
    for _ in range(50):
        cust = random.choice(customers)
        prod, base = random.choice(products)
        amount = round(base + random.uniform(-50, 200), 2)
        status = random.choice(statuses)
        offset = random.randint(0, 86400)
        ts = datetime.utcnow() - timedelta(seconds=random.randint(0, 3600))
        rows.append((cust, prod, amount, status, ts))

    await conn.executemany(
        "INSERT INTO mock_orders (customer_name, product_name, amount, status, created_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        rows,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────
def _pg_ok() -> bool:
    """Return True if pg_pool is initialised and presumably healthy."""
    return pg_pool is not None


def _redis_ok() -> bool:
    """Return True if redis_client is initialised."""
    return redis_client is not None


# ── CCDT Integration (Phase 2.3) ───────────────────────────────────────────────
async def fetch_ccdt_active_incident() -> dict | None:
    """
    Fetch the currently active CRITICAL incident from CCDT API Gateway.
    Returns incident dict if found, else None.
    Caches result for 2 seconds to avoid hammering CCDT backend.
    """
    global ccdt_active_incident, ccdt_last_check

    if not CCDT_INCIDENT_POLL_ENABLED:
        return None

    now = time.time()
    # Cache for 2 seconds
    if now - ccdt_last_check < 2.0:
        return ccdt_active_incident

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{CCDT_API_GATEWAY}/api/incidents",
                params={"status": "active", "limit": 1}
            )

            if response.status_code == 200:
                data = response.json()
                incidents = data.get("incidents", data.get("rows", []))

                if incidents:
                    incident = incidents[0]
                    # Only simulate failure for CRITICAL incidents
                    if incident.get("severity") == "critical":
                        ccdt_active_incident = incident
                        ccdt_last_check = now
                        log.warning(
                            "🔴 CCDT CRITICAL incident detected: %s (ID: %s)",
                            incident.get("title", "Unknown"),
                            incident.get("id", "N/A")
                        )
                        return incident

                # No critical incident - clear cache
                if ccdt_active_incident:
                    log.info("✅ CCDT incident resolved - resuming normal operations")
                ccdt_active_incident = None
                ccdt_last_check = now
                return None

    except Exception as exc:
        # CCDT API unreachable - fail open (don't break mock-api)
        log.debug("CCDT incident check failed (fail-open): %s", exc)
        ccdt_active_incident = None
        ccdt_last_check = now

    return None


# ── GET /api/health ────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health() -> dict:
    """
    Health check endpoint.

    Phase 2.3 Enhancement: If CCDT has detected a CRITICAL incident affecting
    postgres or redis, simulate service degradation by returning unhealthy status.
    This makes the NexaOps UI visibly break when CCDT Guardian is working.
    """
    result = {"postgres": False, "redis": False,
              "ts": datetime.utcnow().isoformat()}

    # Phase 2.3: Check if CCDT has active critical incident
    incident = await fetch_ccdt_active_incident()

    if incident:
        # Simulate service failure based on incident root cause
        root_cause = incident.get("root_cause", "").lower()
        affected = incident.get("affected", "").lower()

        # If postgres is the root cause or in blast radius, mark it unhealthy
        if "postgres" in root_cause or "postgres" in affected or "demo-postgres" in root_cause:
            result["postgres"] = False
            result["ccdt_incident"] = incident.get("id")
            result["ccdt_message"] = incident.get("title", "Service degraded")
            log.warning("❌ Simulating postgres failure due to CCDT incident %s", incident.get("id"))

        # If redis is the root cause or in blast radius, mark it unhealthy
        if "redis" in root_cause or "redis" in affected or "demo-redis" in root_cause:
            result["redis"] = False
            result["ccdt_incident"] = incident.get("id")
            result["ccdt_message"] = incident.get("title", "Service degraded")
            log.warning("❌ Simulating redis failure due to CCDT incident %s", incident.get("id"))

        # If it's a broad failure (OOM cascade, network partition), fail both
        if any(keyword in incident.get("description", "").lower()
               for keyword in ["cascade", "network", "partition", "critical"]):
            result["postgres"] = False
            result["redis"] = False
            result["ccdt_incident"] = incident.get("id")
            result["ccdt_message"] = incident.get("title", "Service degraded")

        # Return 503 Service Unavailable during active incident
        return JSONResponse(status_code=503, content=result)

    # Normal health check when no CCDT incident
    try:
        if _pg_ok():
            async with pg_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            result["postgres"] = True
    except Exception as exc:
        log.error("PG health: %s", exc)

    try:
        if _redis_ok():
            await redis_client.ping()
            result["redis"] = True
    except Exception as exc:
        log.error("Redis health: %s", exc)

    return result


# ── GET /api/dashboard ─────────────────────────────────────────────────────────
@app.get("/api/dashboard")
async def dashboard() -> dict:
    # Phase 2.3: Simulate failure during CCDT incident
    incident = await fetch_ccdt_active_incident()
    if incident:
        log.warning("🔴 Dashboard unavailable during CCDT incident %s", incident.get("id"))
        raise HTTPException(
            status_code=503,
            detail=f"Service degraded: {incident.get('title', 'CCDT incident active')}"
        )

    if not _pg_ok():
        raise HTTPException(
            status_code=503, detail="Database pool not initialised")

    try:
        # ── Redis session / cache metrics (graceful degradation) ───────────────
        session_id = str(uuid.uuid4())
        live_sessions = random.randint(80, 120)   # fallback if Redis is down
        cache_hit_rate = 0.0

        if _redis_ok():
            try:
                await redis_client.setex(f"session:{session_id}", 300, "active")
                _cur, keys = await redis_client.scan(0, match="session:*", count=500)
                live_sessions = max(len(keys), 1)
                await redis_client.incr("cache:total")
                if random.random() < 0.87:
                    await redis_client.incr("cache:hits")
                hits = int(await redis_client.get("cache:hits") or 0)
                total = int(await redis_client.get("cache:total") or 1)
                cache_hit_rate = round((hits / max(total, 1)) * 100, 1)
            except Exception as redis_exc:
                log.warning("Redis metrics skipped: %s", redis_exc)
                # Redis is down — continue with defaults; /api/health will show it

        # ── PostgreSQL queries ─────────────────────────────────────────────────
        async with pg_pool.acquire() as conn:
            revenue = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM mock_orders "
                "WHERE status = 'fulfilled'"
            )
            active_orders = await conn.fetchval(
                "SELECT COUNT(*) FROM mock_orders WHERE status IN ('pending', 'fulfilled')"
            )
            pending_orders = await conn.fetchval(
                "SELECT COUNT(*) FROM mock_orders WHERE status = 'pending'"
            )

            # Insert latency sample then immediately prune old rows
            # (prevents unbounded growth at ~33 rows/sec with 100 users)
            await conn.execute(
                "INSERT INTO mock_metrics (metric_name, value) VALUES ('latency_ms', $1)",
                random.uniform(8, 45),
            )
            await conn.execute(
                "DELETE FROM mock_metrics "
                "WHERE metric_name = 'latency_ms' AND ts < NOW() - INTERVAL '10 minutes'"
            )
            avg_latency = await conn.fetchval(
                "SELECT ROUND(AVG(value)) FROM mock_metrics "
                "WHERE metric_name = 'latency_ms' AND ts > NOW() - INTERVAL '5 minutes'"
            )

        latency_ms = int(float(avg_latency or random.randint(12, 35)))

        return {
            "revenue_24h":    float(revenue or 0),
            "revenue_delta":  round(random.uniform(2.1, 8.4), 1),
            "active_orders":  int(active_orders or 0),
            "pending_orders": int(pending_orders or 0),
            "live_sessions":  live_sessions,
            "avg_latency_ms": latency_ms,
            "p95_latency_ms": int(latency_ms * 2.3),
            "db_qps":         random.randint(120, 380),
            "cache_hit_rate": cache_hit_rate,
            "error_rate_5m":  random.randint(0, 3),
            "session_id":     session_id,
        }

    except Exception as exc:
        log.error("Dashboard error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Database error: {exc}")


# ── GET /api/orders ────────────────────────────────────────────────────────────
@app.get("/api/orders")
async def orders(limit: int = Query(default=10, le=50)) -> dict:
    # Phase 2.3: Simulate failure during CCDT incident
    incident = await fetch_ccdt_active_incident()
    if incident:
        log.warning("🔴 Orders endpoint unavailable during CCDT incident %s", incident.get("id"))
        raise HTTPException(
            status_code=502,
            detail=f"Bad Gateway: {incident.get('title', 'Database unreachable')}"
        )

    if not _pg_ok():
        raise HTTPException(
            status_code=503, detail="Database pool not initialised")

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_id, customer_name, product_name, "
                "       amount::float AS amount, status, created_at "
                "FROM mock_orders ORDER BY created_at DESC LIMIT $1",
                limit,
            )

        # ~30% chance of a new live order arriving on each poll
        new_order = None
        if random.random() < 0.30:
            customers = ["Alice Sharma", "Bob Chen", "Carol Rodriguez",
                         "Dave Kim", "Erin Patel", "Frank Mueller"]
            products = [("Compute Node v3", 4899), ("SSD Array 4TB", 1299),
                        ("GPU Module H100", 9999), ("Memory DDR5 64GB", 349),
                        ("Network Switch", 799)]
            cust = random.choice(customers)
            prod, base = random.choice(products)
            amt = round(base + random.uniform(-30, 150), 2)
            async with pg_pool.acquire() as conn:
                oid = await conn.fetchval(
                    "INSERT INTO mock_orders (customer_name, product_name, amount, status) "
                    "VALUES ($1, $2, $3, 'pending') RETURNING order_id",
                    cust, prod, amt,
                )
            new_order = {
                "order_id":      oid,
                "customer_name": cust,
                "product_name":  prod,
                "amount":        str(amt),
                "status":        "pending",
            }

        return {
            "rows":      [dict(r) for r in rows],
            "new_order": new_order,
            "total":     len(rows),
        }

    except Exception as exc:
        log.error("Orders error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Database error: {exc}")


# ── GET /api/inventory ─────────────────────────────────────────────────────────
@app.get("/api/inventory")
async def inventory() -> dict:
    # Phase 2.3: Simulate failure during CCDT incident
    incident = await fetch_ccdt_active_incident()
    if incident:
        log.warning("🔴 Inventory endpoint unavailable during CCDT incident %s", incident.get("id"))
        raise HTTPException(
            status_code=503,
            detail=f"Service Unavailable: {incident.get('title', 'PostgreSQL connection lost')}"
        )

    if not _pg_ok():
        raise HTTPException(
            status_code=503, detail="Database pool not initialised")

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT sku, quantity, max_quantity FROM mock_inventory ORDER BY sku"
            )
            if rows:
                row = random.choice(rows)
                sku = row["sku"]
                qty = row["quantity"]
                max_qty = row["max_quantity"]
                pct = qty / max_qty if max_qty > 0 else 1.0

                # Realistic stock movement: low stock triggers restocking
                if pct < 0.15:
                    delta = random.randint(
                        int(max_qty * 0.25), int(max_qty * 0.45))
                elif pct < 0.40:
                    delta = (random.randint(10, 40)
                             if random.random() < 0.70
                             else random.randint(-2, -1))
                else:
                    delta = (random.randint(1, 8)
                             if random.random() < 0.35
                             else random.randint(-4, -1))

                await conn.execute(
                    "UPDATE mock_inventory "
                    "SET quantity = LEAST(max_quantity, GREATEST(0, quantity + $1)) "
                    "WHERE sku = $2",
                    delta, sku,
                )

            # Re-fetch after update
            rows = await conn.fetch(
                "SELECT sku, quantity, max_quantity FROM mock_inventory ORDER BY sku"
            )

        return {"items": [dict(r) for r in rows]}

    except Exception as exc:
        log.error("Inventory error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Database error: {exc}")


# ── POST /api/login ────────────────────────────────────────────────────────────
@app.post("/api/login")
async def login(body: dict) -> dict:
    username = body.get("username", "guest")
    if not _redis_ok():
        raise HTTPException(
            status_code=503, detail="Session store unavailable")
    try:
        sid = str(uuid.uuid4())
        await redis_client.setex(f"session:{sid}", 1800, username)
        await redis_client.incr("metric:login_count")
        return {"session_id": sid, "username": username, "expires_in": 1800}
    except Exception as exc:
        log.error("Login error: %s", exc)
        raise HTTPException(
            status_code=503, detail="Session store unavailable")


# ── POST /api/logout ───────────────────────────────────────────────────────────
@app.post("/api/logout")
async def logout(body: dict) -> dict:
    sid = body.get("session_id", "")
    try:
        if sid and _redis_ok():
            await redis_client.delete(f"session:{sid}")
        return {"ok": True}
    except Exception:
        return {"ok": False}


@app.post("/login")
async def login_compat(request: Request):
    body = await request.json()
    return await login(body)

# ── POST /api/admin/reseed-inventory ──────────────────────────────────────────

@app.post("/api/admin/reseed-inventory")
async def reseed_inventory() -> dict:
    """
    Resets all inventory quantities to healthy seed levels.
    Use this if stock drains to 0 during a long demo session.
    """
    if not _pg_ok():
        raise HTTPException(
            status_code=503, detail="Database pool not initialised")
    try:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM mock_inventory")
            await _seed_inventory(conn)
        log.info("Inventory reseeded by admin request")
        return {"ok": True, "message": "Inventory reset to healthy levels"}
    except Exception as exc:
        log.error("Reseed error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /api/ccdt/incident ────────────────────────────────────────────────────
@app.get("/api/ccdt/incident")
async def get_ccdt_incident() -> dict:
    """
    Phase 2.3: Expose CCDT active incident status.
    This endpoint allows the frontend to check if CCDT is currently
    detecting/healing an incident.
    """
    incident = await fetch_ccdt_active_incident()
    if incident:
        return {
            "has_incident": True,
            "incident": {
                "id": incident.get("id"),
                "title": incident.get("title"),
                "severity": incident.get("severity"),
                "incident_type": incident.get("incident_type"),
                "root_cause": incident.get("root_cause"),
                "affected": incident.get("affected"),
                "status": incident.get("status"),
                "created_at": incident.get("created_at"),
                "gnn_confidence": incident.get("gnn_confidence"),
                "action_taken": incident.get("action_taken"),
                "description": incident.get("description"),
            }
        }
    return {"has_incident": False, "incident": None}


# ── GET /ready ─────────────────────────────────────────────────────────────────
@app.get("/ready")
async def ready() -> dict:
    """Docker / nginx readiness probe."""
    return {"status": "ok"}


# ── Compatibility routes (NO /api prefix) ─────────────────────────────


@app.get("/dashboard")
async def dashboard_compat():
    return await dashboard()


@app.get("/orders")
async def orders_compat(limit: int = 10):
    return await orders(limit)


@app.get("/inventory")
async def inventory_compat():
    return await inventory()


@app.get("/health")
async def health_compat():
    return await health()

# ── CCDT Proxy Routes ──────────────────────────────────────────────────────────
# Note: nginx strips /api/ prefix, so /api/topology reaches here as /topology
@app.get("/topology")
async def topology_proxy():
    """Proxy topology requests to Layer-2 GNN service."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("http://layer2-gnn:8001/topology")
            return JSONResponse(content=response.json(), status_code=response.status_code)
    except Exception as exc:
        log.error("Topology proxy error: %s", exc)
        raise HTTPException(status_code=502, detail=f"GNN service unavailable: {exc}")

@app.get("/incidents")
async def incidents_proxy(
    status: str | None = Query(None),
    limit: int = Query(50, le=200)
):
    """Proxy incidents requests to API Gateway."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            params = {"limit": limit}
            if status:
                params["status"] = status
            response = await client.get(
                f"{CCDT_API_GATEWAY}/api/v1/incidents",
                params=params
            )
            return JSONResponse(content=response.json(), status_code=response.status_code)
    except Exception as exc:
        log.error("Incidents proxy error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Incidents API unavailable: {exc}")

@app.get("/metrics/docker")
async def cadvisor_proxy():
    """Proxy cAdvisor metrics requests."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("http://cadvisor:8080/api/v1.3/docker")
            return JSONResponse(content=response.json(), status_code=response.status_code)
    except Exception as exc:
        log.debug("cAdvisor proxy error (fail-open): %s", exc)
        # Return empty metrics on failure - integration layer will use mock data
        return JSONResponse(content={})

# ── Dev entrypoint ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8089, reload=False)
