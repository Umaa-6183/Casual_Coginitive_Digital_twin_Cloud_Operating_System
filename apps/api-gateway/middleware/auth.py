"""
CCDT API Gateway — JWT Authentication + RBAC Middleware

Every request passes through this middleware:
  1. Public paths → pass through immediately
  2. Extract Bearer token from Authorization header
  3. Decode + verify JWT signature / expiry
  4. RBAC check: role must be permitted for (method, path)
  5. Inject request.state.user and request.state.role for downstream use

Role hierarchy:  viewer < operator < admin
"""
from __future__ import annotations

import os
import time
import logging
from typing import Optional

import jwt
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger("ccdt.auth")

# ─── Configuration (loaded from environment / .env) ───────────────────────────
JWT_SECRET    = os.getenv("JWT_SECRET",    "ccdt-dev-secret-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").lower() == "true"

# ─── Paths that never require a token ─────────────────────────────────────────
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/ready",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
})

PUBLIC_PREFIXES: tuple[str, ...] = (
    "/ws/",       # WebSocket upgrades (auth handled inside the WS handler)
    "/static/",
)

# ─── RBAC: per-role permission sets ───────────────────────────────────────────
# Each entry is (HTTP_METHOD, path_prefix).
# "admin" gets a wildcard inside the check function; no need to enumerate.
ROLE_PERMISSIONS: dict[str, list[tuple[str, str]]] = {
    "viewer": [
        ("GET",  "/api/v1/topology"),
        ("GET",  "/api/v1/incidents"),
        ("GET",  "/api/v1/guardian/policies"),
        ("GET",  "/api/v1/guardian/actions"),
        ("GET",  "/api/v1/ebpf"),
        ("POST", "/api/v1/infer"),
        ("POST", "/api/v1/copilot/chat"),
    ],
    "operator": [
        ("GET",  "/api/v1"),
        ("POST", "/api/v1/infer"),
        ("POST", "/api/v1/counterfactual"),
        ("POST", "/api/v1/actions/preview"),
        ("POST", "/api/v1/copilot"),
        ("POST", "/api/v1/incidents"),
        ("PUT",  "/api/v1/incidents"),
    ],
    "admin": [],   # wildcard — handled in _role_allowed()
}

# Lower-privilege roles are automatically included in higher-privilege roles.
ROLE_HIERARCHY: dict[str, list[str]] = {
    "viewer":   ["viewer"],
    "operator": ["viewer", "operator"],
    "admin":    ["viewer", "operator", "admin"],
}


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _is_public(path: str) -> bool:
    """Return True if the path needs no authentication."""
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _role_allowed(role: str, method: str, path: str) -> bool:
    """Return True if the role is permitted to call method on path."""
    if role == "admin":
        return True
    for r in ROLE_HIERARCHY.get(role, [role]):
        for perm_method, perm_prefix in ROLE_PERMISSIONS.get(r, []):
            if method.upper() == perm_method and path.startswith(perm_prefix):
                return True
    return False


def _decode_token(token: str) -> dict:
    """
    Decode and verify a JWT.
    Raises HTTPException(401) on any validation failure.
    """
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": True},
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired — please re-authenticate",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ─── Middleware ────────────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette / FastAPI middleware that enforces JWT authentication and
    role-based access control on every inbound HTTP request.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path

        # 1. Public paths — no token needed
        if _is_public(path):
            return await call_next(request)

        # 2. Dev mode — auth disabled via AUTH_DISABLED=true env var
        if AUTH_DISABLED:
            request.state.user = "dev-user"
            request.state.role = "admin"
            return await call_next(request)

        # 3. Extract Bearer token
        auth_header: Optional[str] = request.headers.get("Authorization")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": "Missing Authorization header. "
                              "Use: Authorization: Bearer <token>"
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[7:].strip()   # strip "Bearer "

        # 4. Decode and validate JWT
        try:
            payload = _decode_token(token)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers or {},
            )

        user: str = payload.get("sub", "unknown")
        role: str = payload.get("role", "viewer")

        # 5. RBAC check
        if not _role_allowed(role, request.method, path):
            logger.warning(
                "RBAC denied user=%s role=%s %s %s",
                user, role, request.method, path,
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "detail": (
                        f"Role '{role}' is not authorised to "
                        f"{request.method} {path}"
                    )
                },
            )

        # 6. Inject into request.state for downstream handlers
        request.state.user = user
        request.state.role = role

        logger.debug("Auth OK user=%s role=%s %s %s", user, role, request.method, path)
        return await call_next(request)


# ─── FastAPI dependencies ─────────────────────────────────────────────────────

async def require_auth(request: Request) -> dict:
    """
    FastAPI dependency — returns the authenticated user dict.
    Raises 401 if the request has not been authenticated.

    Usage:
        @router.get("/protected")
        async def endpoint(auth: dict = Depends(require_auth)):
            return {"user": auth["user"], "role": auth["role"]}
    """
    user = getattr(request.state, "user", None)
    role = getattr(request.state, "role", None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return {"user": user, "role": role}


async def require_admin(request: Request) -> dict:
    """FastAPI dependency — same as require_auth but enforces admin role."""
    auth = await require_auth(request)
    if auth["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required for this endpoint",
        )
    return auth


async def require_operator(request: Request) -> dict:
    """FastAPI dependency — requires operator or admin role."""
    auth = await require_auth(request)
    if auth["role"] not in ("operator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator or Admin role required for this endpoint",
        )
    return auth


# ─── Token generation helper (dev / testing) ──────────────────────────────────

def create_access_token(
    subject: str,
    role: str = "operator",
    expires_in: int = 3600,
) -> str:
    """
    Generate a signed JWT for testing or CLI tooling.

    Args:
        subject:        User identifier (e.g. "alice@company.com")
        role:           "viewer" | "operator" | "admin"
        expires_in:     Token lifetime in seconds (default 1 hour)

    Returns:
        Signed JWT string.

    Example:
        token = create_access_token("sre-alice", role="operator")
        # Use as: Authorization: Bearer <token>
    """
    now = int(time.time())
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + expires_in,
        "iss": "ccdt-api-gateway",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
