#!/usr/bin/env python3
"""
CCDT Business Facade — Human-Like Traffic Generator (v2)
=========================================================
Replaces the original demo-traffic-gen headless HTTP script.

Simulates 100+ concurrent human users interacting with the NexaOps Mock UI:
  • Login sessions (POST /api/login)
  • Dashboard views (GET /api/dashboard)
  • Order browsing  (GET /api/orders)
  • Inventory checks (GET /api/inventory)
  • Logout           (POST /api/logout)

Each virtual user has a random think-time (0.5–4 s) between actions, and
sessions expire & are renewed automatically — just like a real user.

PORT NOTE:
  MOCK_API_URL defaults to http://demo-nginx:80  ← container-internal port.
  Port 80 is what nginx listens on INSIDE the Docker network.
  (Port 8088 is the host-side mapping — only reachable from your laptop,
   not from another container.)
"""

import os
import sys
import time
import uuid
import random
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

# ── Config ────────────────────────────────────────────────────────────────────
# Use container-internal port 80, NOT host-mapped port 8088.
MOCK_API_BASE = os.getenv("MOCK_API_URL", "http://demo-nginx:80")
CONCURRENT_USERS = int(os.getenv("CONCURRENT_USERS", "100"))
THINK_TIME_MIN = float(os.getenv("THINK_TIME_MIN", "0.5"))
THINK_TIME_MAX = float(os.getenv("THINK_TIME_MAX", "4.0"))
SESSION_TTL = int(os.getenv("SESSION_TTL_S", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
STATS_INTERVAL = int(os.getenv("STATS_INTERVAL_S", "10"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_S", "8.0"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("traffic-gen")

# ── Synthetic user catalogue ──────────────────────────────────────────────────
USERS = [
    "alice.sharma",    "bob.chen",      "carol.rodriguez", "dave.kim",
    "erin.patel",      "frank.mueller", "grace.okonkwo",   "hiro.tanaka",
    "isabella.santos", "james.wright",  "kira.novak",      "liam.osei",
    "mia.bergstrom",   "noah.ibrahim",  "olivia.chukwu",   "paris.dumont",
    "quinn.fitzgerald", "ravi.kapoor",   "sara.johansson",  "teo.nakamura",
    "uma.williams",    "victor.leung",  "wren.oconnell",   "xia.zhang",
    "yasmin.ali",      "zach.brown",    "ana.pereira",     "ben.adeyemi",
    "chloe.martin",    "daniel.soto",   "ella.berg",       "finn.walsh",
]

# ── Page visit weights (realistic distribution) ───────────────────────────────
# (endpoint, method, weight)
PAGES = [
    ("/api/dashboard",        "GET", 35),
    ("/api/orders",           "GET", 30),
    ("/api/inventory",        "GET", 25),
    ("/api/health",           "GET",  5),
    ("/api/orders?limit=25",  "GET",  5),
]
PAGE_ENDPOINTS = [p[0] for p in PAGES]
PAGE_METHODS = [p[1] for p in PAGES]
PAGE_WEIGHTS = [p[2] for p in PAGES]


# ── Stats tracker ─────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.requests = 0
        self.errors = 0
        self.logins = 0
        self.logouts = 0
        self.active_sessions = 0
        self.latency_sum = 0.0

    def rps(self, elapsed: float) -> float:
        return self.requests / max(elapsed, 1)

    def error_rate(self) -> float:
        return (self.errors / max(self.requests, 1)) * 100

    def avg_latency_ms(self) -> float:
        return (self.latency_sum / max(self.requests, 1)) * 1000


stats = Stats()
start_time = time.time()


# ── Session state ─────────────────────────────────────────────────────────────
@dataclass
class UserSession:
    username:   str
    session_id: Optional[str] = None
    logged_in:  bool = False
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > SESSION_TTL


# ── HTTP helpers ──────────────────────────────────────────────────────────────
async def _get(session: aiohttp.ClientSession, path: str) -> tuple[int, dict | None]:
    url = f"{MOCK_API_BASE}{path}"
    t0 = time.monotonic()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as r:
            latency = time.monotonic() - t0
            stats.requests += 1
            stats.latency_sum += latency
            if r.status >= 400:
                stats.errors += 1
                log.debug("GET %s → %d (%.0fms)", path,
                          r.status, latency * 1000)
                return r.status, None
            try:
                data = await r.json()
            except Exception:
                data = {}
            log.debug("GET %s → %d (%.0fms)", path, r.status, latency * 1000)
            return r.status, data
    except asyncio.TimeoutError:
        stats.requests += 1
        stats.errors += 1
        log.warning("GET %s → TIMEOUT after %.1fs", path, REQUEST_TIMEOUT)
        return 504, None
    except aiohttp.ClientConnectionError as exc:
        stats.requests += 1
        stats.errors += 1
        log.warning("GET %s → CONNECTION ERROR: %s", path, exc)
        return 503, None


async def _post(session: aiohttp.ClientSession, path: str, body: dict) -> tuple[int, dict | None]:
    url = f"{MOCK_API_BASE}{path}"
    t0 = time.monotonic()
    try:
        async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as r:
            latency = time.monotonic() - t0
            stats.requests += 1
            stats.latency_sum += latency
            if r.status >= 400:
                stats.errors += 1
                return r.status, None
            try:
                data = await r.json()
            except Exception:
                data = {}
            return r.status, data
    except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as exc:
        stats.requests += 1
        stats.errors += 1
        log.warning("POST %s → %s", path, type(exc).__name__)
        return 503, None


# ── Single-user simulation ────────────────────────────────────────────────────
async def simulate_user(user: UserSession, http: aiohttp.ClientSession) -> None:
    """
    Runs indefinitely for one virtual user: login → browse → logout → repeat.
    """
    while True:
        try:
            # ── Login / re-login ──────────────────────────────────────────────
            if not user.logged_in or user.is_expired():
                if user.logged_in:
                    await _post(http, "/api/logout", {"session_id": user.session_id})
                    user.logged_in = False
                    stats.logouts += 1
                    stats.active_sessions = max(0, stats.active_sessions - 1)

                code, data = await _post(http, "/api/login", {"username": user.username})
                if code == 200 and data:
                    user.session_id = data.get("session_id")
                    user.logged_in = True
                    user.created_at = time.time()
                    stats.logins += 1
                    stats.active_sessions += 1
                    log.debug("[%s] ✓ login", user.username)
                else:
                    # Backend unavailable — back off and retry
                    await asyncio.sleep(random.uniform(2, 6))
                    continue

            # ── Browse (3–8 page views per session cycle) ─────────────────────
            page_views = random.randint(3, 8)
            for _ in range(page_views):
                path = random.choices(
                    PAGE_ENDPOINTS, weights=PAGE_WEIGHTS, k=1)[0]
                code, _ = await _get(http, path)

                if code in (502, 503, 504):
                    # Service degraded — back off
                    await asyncio.sleep(random.uniform(1.5, 4.0))
                    break

                # Human think-time between clicks
                await asyncio.sleep(random.uniform(THINK_TIME_MIN, THINK_TIME_MAX))

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[%s] Unexpected error: %s", user.username, exc)
            await asyncio.sleep(2)


# ── Stats reporter ────────────────────────────────────────────────────────────
async def stats_reporter() -> None:
    while True:
        await asyncio.sleep(STATS_INTERVAL)
        elapsed = time.time() - start_time
        log.info(
            "📊 STATS | Requests: %s | RPS: %.1f | Errors: %d (%.1f%%) | "
            "Avg latency: %.0fms | Sessions: %d/%d | "
            "Logins: %d | Logouts: %d",
            f"{stats.requests:,}",
            stats.rps(elapsed),
            stats.errors,
            stats.error_rate(),
            stats.avg_latency_ms(),
            stats.active_sessions,
            CONCURRENT_USERS,
            stats.logins,
            stats.logouts,
        )


# ── Wait for backend ──────────────────────────────────────────────────────────
async def wait_for_backend(http: aiohttp.ClientSession, max_wait: int = 90) -> None:
    log.info("Waiting for backend at %s …", MOCK_API_BASE)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            async with http.get(
                f"{MOCK_API_BASE}/api/health",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                if r.status == 200:
                    log.info("✓ Backend is ready")
                    return
        except Exception:
            pass
        await asyncio.sleep(2)
    log.warning(
        "Backend did not become ready in %ds — starting anyway", max_wait)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    log.info(
        "CCDT Traffic Generator v2 starting — %d virtual users → %s",
        CONCURRENT_USERS, MOCK_API_BASE,
    )

    connector = aiohttp.TCPConnector(
        limit=CONCURRENT_USERS + 20,
        limit_per_host=CONCURRENT_USERS + 20,
    )
    async with aiohttp.ClientSession(connector=connector) as http:
        await wait_for_backend(http)

        # Build virtual users (cycle through names if count > len(USERS))
        users = [
            UserSession(
                username=(
                    f"{USERS[i % len(USERS)]}.{i // len(USERS)}"
                    if i >= len(USERS) else USERS[i]
                )
            )
            for i in range(CONCURRENT_USERS)
        ]

        # Stagger start times — ramp 100 users over ~5 s (50 ms apart)
        async def _start(user: UserSession, delay: float) -> None:
            await asyncio.sleep(delay)
            await simulate_user(user, http)

        log.info("Ramping up %d users over ~5 s…", CONCURRENT_USERS)
        tasks = [
            asyncio.create_task(_start(u, i * 0.05))
            for i, u in enumerate(users)
        ]
        tasks.append(asyncio.create_task(stats_reporter()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Shutting down…")
        finally:
            for t in tasks:
                t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Traffic generator stopped.")
        sys.exit(0)
