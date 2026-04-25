# ══════════════════════════════════════════════════════════════════════════════
# CCDT Layer-2 Cognitive Core — Dockerfile (Apple Silicon / ARM64 compatible)
# ══════════════════════════════════════════════════════════════════════════════
# Removed from build: h5py, torch-scatter, torch-sparse, torch-cluster
# These packages have no pre-built ARM64 wheels and fail to compile from source.
# torch-geometric works without them for GNN inference on aarch64.
# ══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /install

COPY requirements.txt .

# Step 1: install PyTorch CPU (pre-built ARM64 wheel — fast)
RUN pip install --upgrade pip && \
    pip install --no-cache-dir \
        torch==2.3.0 \
        torchvision==0.18.0 \
        --index-url https://download.pytorch.org/whl/cpu

# Step 2: install torch-geometric only (no scatter/sparse/cluster — no ARM64 wheels)
RUN pip install --no-cache-dir \
        torch-geometric==2.5.3

# Step 3: install everything else from requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ARG BUILD_DATE="unknown"
ARG GIT_SHA="unknown"
ARG VERSION="dev"

LABEL org.opencontainers.image.title="CCDT Layer-2 Cognitive Core" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -s /bin/false -m -d /app ccdt

COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app
USER root

COPY --chown=ccdt:ccdt . .

RUN mkdir -p /app/checkpoints

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8001

EXPOSE 8001

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsSo /dev/null http://localhost:8001/health || exit 1

CMD ["uvicorn", "inference.server:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--log-level", "info"]
