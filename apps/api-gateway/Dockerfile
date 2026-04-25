# ══════════════════════════════════════════════════════════════════════════════
# CCDT API Gateway — Dockerfile
# ══════════════════════════════════════════════════════════════════════════════
# Multi-stage build:
#   builder  — installs Python dependencies
#   runtime  — slim production image (~120 MB)
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: dependency installer ─────────────────────────────────────────────
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /install
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install/deps -r requirements.txt

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ARG BUILD_DATE="unknown"
ARG GIT_SHA="unknown"
ARG VERSION="dev"

LABEL org.opencontainers.image.title="CCDT API Gateway" \
    org.opencontainers.image.description="CCDT REST + WebSocket API Gateway" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.revision="${GIT_SHA}" \
    org.opencontainers.image.version="${VERSION}"

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -s /bin/false -m -d /app ccdt

# Copy installed packages from builder
COPY --from=builder /install/deps /usr/local

WORKDIR /app
USER ccdt

# Copy application source
COPY --chown=ccdt:ccdt . .


ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "main:app", \
    "--host", "0.0.0.0", \
    "--port", "8000", \
    "--workers", "2", \
    "--log-level", "info", \
    "--access-log"]
