# ══════════════════════════════════════════════════════════════════════════════
# CCDT Layer-3 Guardian — Dockerfile
# ══════════════════════════════════════════════════════════════════════════════
# Stage layout:
#   builder  — installs Python deps (CPU-only torch)
#   runtime  — slim production image with RL agent + OPA client
# ══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /install

# CPU-only torch first (avoids 2 GB CUDA download)
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install/deps \
    torch==2.3.0 \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install/deps \
    -r requirements.txt \
    --ignore-installed torch

# ── Runtime image ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ARG BUILD_DATE="unknown"
ARG GIT_SHA="unknown"
ARG VERSION="dev"

LABEL org.opencontainers.image.title="CCDT Layer-3 Guardian" \
    org.opencontainers.image.description="RL Agent + OPA Guardian Layer" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.revision="${GIT_SHA}" \
    org.opencontainers.image.version="${VERSION}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -s /bin/false -m -d /app ccdt

COPY --from=builder /install/deps /usr/local

WORKDIR /app
USER ccdt

COPY --chown=ccdt:ccdt . .


# OPA policy directory (also mounted at runtime for hot-reload)
RUN mkdir -p /app/checkpoints /app/opa/policies

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8002 \
    AUTONOMY_MODE=human-in-loop \
    OPA_URL=http://opa:8181 \
    GNN_SERVICE_URL=http://layer2-cognitive:8001

EXPOSE 8002

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8002/health || exit 1

CMD ["uvicorn", "server:app", \
    "--host", "0.0.0.0", \
    "--port", "8002", \
    "--log-level", "info"]
