"""
CCDT API Gateway — Rate Limiting Middleware

Two-tier rate limiting:
  • Global:  300 requests / 60 s per client IP (in-process sliding window)
  • Redis:   Optional distributed sliding window (used in multi-replica deploy)

If Redis is unavailable the middleware falls back silently to in-process limits.
Rate-limited responses return HTTP 429 with a Retry-After header.
"""
from __future__ import annotations

import os
import time
import asyncio
import logging
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger("ccdt.rate_limit")

# ─── Configuration ─────────────────────────────────────────────────────────────
RATE_LIMIT_ENABLED  = os.getenv("RATE_LIMIT_ENABLED",  "true").lower() == "true"
REQUESTS_PER_MINUTE = int(os.getenv("REQUESTS_PER_MINUTE", "300"))
WINDOW_SECONDS      = int(os.getenv("RATE_LIMIT_WINDOW",   "60"))
REDIS_URL           = os.getenv("REDIS_URL", "redis://redis:6379/0")
USE_REDIS           = os.getenv("RATE_LIMIT_USE_REDIS", "false").lower() == "true"

# Paths that are exempt from rate limiting
EXEMPT_PATHS: frozenset[str] = frozenset({
    "/health",
    "/ready",
    "/metrics",
})

# Higher limits for specific path prefixes
PATH_LIMITS: dict[str, int] = {
    "/api/v1/copilot": 20,   # LLM calls are expensive — 20 rpm
    "/api/v1/actions/execute": 30,  # Destructive actions — conservative
    "/ws/":            1000,        # WebSocket connections — high limit
}


def _get_limit_for_path(path: str) -> int:
    """Return the requests-per-minute limit for a given path."""
    for prefix, limit in PATH_LIMITS.items():
        if path.startswith(prefix):
            return limit
    return REQUESTS_PER_MINUTE


# ─── In-process sliding window ────────────────────────────────────────────────

class SlidingWindowCounter:
    """
    Per-client IP sliding-window request counter.
    Thread-safe for asyncio (single-threaded event loop).
    Memory-bounded: idle clients are pruned periodically.
    """

    def __init__(self, window: int = WINDOW_SECONDS) -> None:
        self.window:   int = window
        # client_key → deque of timestamps
        self._windows: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock     = asyncio.Lock()
        self._last_prune: float = time.monotonic()

    async def is_allowed(self, key: str, limit: int) -> tuple[bool, int, float]:
        """
        Check whether the client identified by `key` is within the rate limit.

        Returns:
            (allowed, remaining_requests, retry_after_seconds)
        """
        async with self._lock:
            now = time.monotonic()
            window_start = now - self.window
            q = self._windows[key]

            # Evict timestamps outside the window
            while q and q[0] < window_start:
                q.popleft()

            count = len(q)
            if count >= limit:
                retry_after = self.window - (now - q[0]) if q else float(self.window)
                return False, 0, max(0.0, retry_after)

            q.append(now)
            self._windows[key] = q
            remaining = limit - count - 1

            # Prune idle clients every 5 minutes to prevent unbounded growth
            if now - self._last_prune > 300:
                self._prune(window_start)
                self._last_prune = now

            return True, remaining, 0.0

    def _prune(self, window_start: float) -> None:
        dead_keys = [k for k, q in self._windows.items() if not q or q[-1] < window_start]
        for k in dead_keys:
            del self._windows[k]


# ─── Optional Redis distributed counter ───────────────────────────────────────

class RedisRateLimiter:
    """
    Distributed sliding-window rate limiter backed by Redis.
    Uses a sorted set per client; each member is a request timestamp.
    Falls back silently if Redis is unavailable.
    """

    def __init__(self, redis_url: str, window: int = WINDOW_SECONDS) -> None:
        self.redis_url = redis_url
        self.window    = window
        self._redis: Optional[object] = None
        self._available = False

    async def connect(self) -> None:
        try:
            import redis.asyncio as aioredis
            self._redis = await aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=1,
            )
            await self._redis.ping()  # type: ignore[union-attr]
            self._available = True
            logger.info("Redis rate limiter connected: %s", self.redis_url)
        except Exception as exc:
            logger.warning(
                "Redis unavailable (%s) — falling back to in-process rate limiting", exc
            )
            self._available = False

    async def is_allowed(self, key: str, limit: int) -> tuple[bool, int, float]:
        """
        Check rate limit using Redis sorted set.
        Returns (allowed, remaining, retry_after).
        Falls back to (True, limit, 0) if Redis is unavailable.
        """
        if not self._available or self._redis is None:
            return True, limit, 0.0

        try:
            redis_key  = f"ccdt:rl:{key}"
            now        = time.time()
            window_start = now - self.window

            pipe = self._redis.pipeline()  # type: ignore[union-attr]
            pipe.zremrangebyscore(redis_key, "-inf", window_start)
            pipe.zadd(redis_key, {str(now): now})
            pipe.zcard(redis_key)
            pipe.expire(redis_key, self.window + 5)
            results = await pipe.execute()

            count = results[2]  # zcard result
            if count > limit:
                # Oldest timestamp in the window
                oldest = await self._redis.zrange(redis_key, 0, 0, withscores=True)  # type: ignore[union-attr]
                retry_after = (oldest[0][1] + self.window - now) if oldest else float(self.window)
                return False, 0, max(0.0, retry_after)

            return True, max(0, limit - count), 0.0

        except Exception as exc:
            logger.warning("Redis rate-limit check failed: %s — allowing request", exc)
            return True, limit, 0.0

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()  # type: ignore[union-attr]
            except Exception:
                pass


# ─── Singleton instances ───────────────────────────────────────────────────────
_in_process_limiter = SlidingWindowCounter()
_redis_limiter      = RedisRateLimiter(REDIS_URL) if USE_REDIS else None


async def startup_rate_limiter() -> None:
    """Call this from the FastAPI lifespan startup to connect Redis."""
    if _redis_limiter is not None:
        await _redis_limiter.connect()


async def shutdown_rate_limiter() -> None:
    """Call this from the FastAPI lifespan shutdown to close Redis."""
    if _redis_limiter is not None:
        await _redis_limiter.close()


# ─── Middleware ────────────────────────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    HTTP rate-limiting middleware.

    - Identifies clients by X-Forwarded-For header, then X-Real-IP, then remote addr.
    - Uses Redis when available (multi-replica), falls back to in-process counter.
    - Returns HTTP 429 with Retry-After and X-RateLimit-* headers on breach.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path

        # Skip exempt paths
        if not RATE_LIMIT_ENABLED or path in EXEMPT_PATHS:
            return await call_next(request)

        # Identify client
        client_key = self._client_key(request)
        limit      = _get_limit_for_path(path)

        # Choose limiter
        if USE_REDIS and _redis_limiter is not None:
            allowed, remaining, retry_after = await _redis_limiter.is_allowed(
                client_key, limit
            )
        else:
            allowed, remaining, retry_after = await _in_process_limiter.is_allowed(
                client_key, limit
            )

        if not allowed:
            logger.info(
                "Rate limit exceeded: client=%s path=%s limit=%d retry_after=%.1fs",
                client_key, path, limit, retry_after,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "limit":  limit,
                    "window": f"{WINDOW_SECONDS}s",
                    "retry_after_seconds": round(retry_after, 1),
                },
                headers={
                    "Retry-After":               str(int(retry_after) + 1),
                    "X-RateLimit-Limit":         str(limit),
                    "X-RateLimit-Remaining":     "0",
                    "X-RateLimit-Reset":         str(int(time.time() + retry_after)),
                },
            )

        response = await call_next(request)

        # Attach informational headers on success
        response.headers["X-RateLimit-Limit"]     = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"]    = f"{WINDOW_SECONDS}s"

        return response

    @staticmethod
    def _client_key(request: Request) -> str:
        """
        Extract a stable client identifier from request headers.
        Prefers X-Forwarded-For (set by load balancers / ingress).
        """
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Take the first (leftmost) IP — the original client
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        if request.client:
            return request.client.host
        return "unknown"
