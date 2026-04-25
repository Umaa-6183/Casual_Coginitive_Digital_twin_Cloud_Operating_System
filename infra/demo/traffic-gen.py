#!/usr/bin/env python3
"""
CCDT — Real Traffic Generator
════════════════════════════════════════════════════════════════════════════════
Makes genuine HTTP, SQL, Redis, and Kafka calls every 2 seconds to keep the
demo cluster under realistic load, generating REAL cgroup metrics that
Prometheus can scrape and the GNN can reason about.

Runs as a Docker container (demo-traffic-gen) inside the CCDT network.

What it does every tick:
  • HTTP GET  → demo-nginx (real HTTP connection, real Nginx access log)
  • SQL INSERT/SELECT → demo-postgres via psycopg2 (real WAL writes)
  • Redis GET/SET → demo-redis (real keyspace activity)
  • Kafka publish → ccdt.ebpf.events (real broker throughput)

Fault intensification:
  During an active scenario, the generator ramps up traffic to the affected
  services — this is what causes genuine CPU/memory pressure in the containers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ccdt.traffic-gen")

# ── Config ────────────────────────────────────────────────────────────────────
NGINX_URL    = os.getenv("NGINX_URL",    "http://demo-nginx:80")
PG_DSN       = os.getenv("PG_DSN",       "postgresql://ccdt:ccdt@demo-postgres:5432/ccdt")
REDIS_URL    = os.getenv("REDIS_URL_DEMO","redis://demo-redis:6379/1")
KAFKA_BOOTS  = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
INTERVAL     = float(os.getenv("TRAFFIC_INTERVAL", "2.0"))
BURST_FACTOR = float(os.getenv("BURST_FACTOR", "1.0"))  # set to 5.0 for fault injection


async def http_requests(session, burst: int = 1) -> None:
    """Make real HTTP calls to Nginx."""
    paths = ["/", "/health", "/api/orders", "/api/users", "/api/products"]
    for _ in range(burst):
        path = random.choice(paths)
        try:
            resp = await session.get(f"{NGINX_URL}{path}", timeout=2.0)
            logger.debug("HTTP %s %s → %d", "GET", path, resp.status_code)
        except Exception as exc:
            logger.debug("HTTP error: %s", exc)


async def sql_queries(pool, burst: int = 1) -> None:
    """Make real SQL queries against PostgreSQL."""
    queries = [
        "INSERT INTO orders (user_id, amount, status) VALUES ($1, $2, $3)",
        "SELECT COUNT(*) FROM orders WHERE status = $1",
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT 10",
        "UPDATE orders SET status = $1 WHERE id = (SELECT id FROM orders ORDER BY RANDOM() LIMIT 1)",
    ]
    for i in range(burst):
        try:
            async with pool.acquire() as conn:
                q = queries[i % len(queries)]
                if "INSERT" in q:
                    await conn.execute(q,
                        random.randint(1, 1000),
                        round(random.uniform(10, 500), 2),
                        random.choice(["pending", "confirmed", "shipped"]),
                    )
                elif "UPDATE" in q:
                    await conn.execute(q, random.choice(["completed", "cancelled"]))
                else:
                    await conn.fetchval(q, "pending") if "$1" in q else await conn.fetch(q)
        except Exception as exc:
            logger.debug("SQL error: %s", exc)


async def redis_ops(redis_client, burst: int = 1) -> None:
    """Make real Redis get/set operations."""
    keys = [f"session:{i}" for i in range(100)]
    for _ in range(burst):
        try:
            key = random.choice(keys)
            if random.random() < 0.6:
                await redis_client.get(key)
            else:
                await redis_client.setex(
                    key, 300,
                    json.dumps({"user_id": random.randint(1, 1000), "ts": time.time()})
                )
        except Exception as exc:
            logger.debug("Redis error: %s", exc)


async def kafka_publish(producer, burst: int = 1) -> None:
    """Publish real messages to Kafka."""
    for _ in range(burst):
        try:
            msg = {
                "msg_type":  "traffic_metric",
                "timestamp": int(time.time()),
                "service":   random.choice(["nginx", "postgres", "redis"]),
                "latency_ms": round(random.gauss(15, 5), 2),
                "requests_per_sec": round(random.gauss(280, 40), 1),
            }
            await producer.send(
                "ccdt.ebpf.events",
                json.dumps(msg).encode()
            )
        except Exception as exc:
            logger.debug("Kafka error: %s", exc)


async def main() -> None:
    logger.info("Traffic generator starting — interval=%.1fs burst=%.1fx",
                INTERVAL, BURST_FACTOR)

    # ── Connect clients ───────────────────────────────────────────────────────
    pg_pool      = None
    redis_client = None
    kafka_prod   = None
    http_session = None

    try:
        import httpx
        http_session = httpx.AsyncClient(timeout=3.0)
    except ImportError:
        logger.warning("httpx not available — skipping HTTP traffic")

    try:
        import asyncpg
        pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=5,
                                             command_timeout=3)
        # Create demo table
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id         SERIAL PRIMARY KEY,
                    user_id    INTEGER,
                    amount     DECIMAL(10,2),
                    status     VARCHAR(20),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        logger.info("PostgreSQL connected")
    except Exception as exc:
        logger.warning("PostgreSQL unavailable: %s", exc)

    try:
        import redis.asyncio as aioredis
        redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as exc:
        logger.warning("Redis unavailable: %s", exc)

    try:
        from aiokafka import AIOKafkaProducer
        kafka_prod = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTS)
        await kafka_prod.start()
        logger.info("Kafka connected")
    except Exception as exc:
        logger.warning("Kafka unavailable: %s", exc)

    # ── Main traffic loop ─────────────────────────────────────────────────────
    tick = 0
    while True:
        tick += 1
        burst = max(1, int(BURST_FACTOR))

        tasks = []
        if http_session:
            tasks.append(http_requests(http_session, burst=burst * 3))
        if pg_pool:
            tasks.append(sql_queries(pg_pool, burst=burst * 2))
        if redis_client:
            tasks.append(redis_ops(redis_client, burst=burst * 5))
        if kafka_prod:
            tasks.append(kafka_publish(kafka_prod, burst=burst))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if tick % 30 == 0:
            logger.info("Tick %d — burst=%.1fx — %d active connections",
                        tick, BURST_FACTOR, len(tasks))

        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
