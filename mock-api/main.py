"""
CCDT Business Facade — Mock API Backend (v2 Final)
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
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

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

# ── Globals ────────────────────────────────────────────────────────────────────
pg_pool:      asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None


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


app = FastAPI(title="CCDT Mock API", version="2.0.0", lifespan=lifespan)

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
        ts = datetime.utcnow() - timedelta(seconds=offset)
        rows.append((cust, prod, amount, status, ts))

    await conn.executemany(
        "INSERT INTO mock_orders (customer_name, product_name, amount, status, created_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        rows,
    )


# ── GET /api/health ────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health() -> dict:
    result = {"postgres": False, "redis": False,
              "ts": datetime.utcnow().isoformat()}

    try:
        if pg_pool:
            async with pg_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            result["postgres"] = True
    except Exception as exc:
        log.error("PG health: %s", exc)

    try:
        if redis_client:
            await redis_client.ping()
            result["redis"] = True
    except Exception as exc:
        log.error("Redis health: %s", exc)

    return result


# ── GET /api/dashboard ─────────────────────────────────────────────────────────
@app.get("/api/dashboard")
async def dashboard() -> dict:
    try:
        # ── Redis session / cache metrics ──────────────────────────────────────
        session_id = str(uuid.uuid4())
        live_sessions = random.randint(80, 120)   # fallback if Redis is down
        cache_hit_rate = 0.0

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
        except Exception:
            pass   # Redis down — UI will reflect this via /api/health

        # ── PostgreSQL queries ─────────────────────────────────────────────────
        async with pg_pool.acquire() as conn:
            revenue = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM mock_orders "
                "WHERE status = 'fulfilled' AND created_at > NOW() - INTERVAL '24 hours'"
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
                    # Critical — always restock a large batch
                    delta = random.randint(
                        int(max_qty * 0.25), int(max_qty * 0.45))
                elif pct < 0.40:
                    # Getting low — 70% chance restock, 30% sell a little
                    delta = (random.randint(10, 40)
                             if random.random() < 0.70
                             else random.randint(-2, -1))
                else:
                    # Normal — 35% small restock, 65% sell a few
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
        if sid:
            await redis_client.delete(f"session:{sid}")
        return {"ok": True}
    except Exception:
        return {"ok": False}


# ── POST /api/admin/reseed-inventory ──────────────────────────────────────────
@app.post("/api/admin/reseed-inventory")
async def reseed_inventory() -> dict:
    """
    Resets all inventory quantities to healthy seed levels.
    Use this if stock drains to 0 during a long demo session:

        curl -X POST http://localhost:8088/api/admin/reseed-inventory
    """
    try:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM mock_inventory")
            await _seed_inventory(conn)
        log.info("Inventory reseeded by admin request")
        return {"ok": True, "message": "Inventory reset to healthy levels"}
    except Exception as exc:
        log.error("Reseed error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /ready ─────────────────────────────────────────────────────────────────
@app.get("/ready")
async def ready() -> dict:
    """Docker / nginx readiness probe."""
    return {"status": "ok"}


# ── Dev entrypoint ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8089, reload=False)
