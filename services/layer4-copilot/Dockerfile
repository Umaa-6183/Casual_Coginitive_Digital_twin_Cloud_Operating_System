# ══════════════════════════════════════════════════════════════════════════════
# CCDT Layer-4 Co-Pilot — Dockerfile
# ══════════════════════════════════════════════════════════════════════════════
# Stage layout:
#   builder  — installs Python deps (CPU torch + Anthropic SDK)
#   runtime  — slim production image
# ══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /install

# CPU-only torch (LoRA base model — no GPU needed in runtime)
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

LABEL org.opencontainers.image.title="CCDT Layer-4 Co-Pilot" \
    org.opencontainers.image.description="LLM Co-Pilot (Claude API) — SSE streaming" \
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


ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8003 \
    CLAUDE_MODEL=claude-sonnet-4-20250514 \
    MAX_TOKENS=2048 \
    MAX_HISTORY_TURNS=20

EXPOSE 8003

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8003/health || exit 1

CMD ["uvicorn", "server:app", \
    "--host", "0.0.0.0", \
    "--port", "8003", \
    "--log-level", "info"]
