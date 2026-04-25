# ─── Stage 1: eBPF compiler ──────────────────────────────────────────────────
# Compiles all .bpf.c probes to CO-RE .bpf.o objects using Clang + libbpf headers.
FROM ubuntu:24.04 AS ebpf-builder

ARG TARGETARCH=amd64

RUN apt-get update && apt-get install -y --no-install-recommends \
        clang-18 \
        llvm-18 \
        libbpf-dev \
        linux-headers-generic \
        make \
        curl \
        ca-certificates \
    && ln -sf /usr/bin/clang-18 /usr/bin/clang \
    && ln -sf /usr/bin/llvm-strip-18 /usr/bin/llvm-strip \
    && rm -rf /var/lib/apt/lists/*

# Download vmlinux.h for the current kernel BTF headers (CO-RE)
# In production this is generated from the running kernel via bpftool
WORKDIR /src/probes/include

# Use a pre-generated vmlinux.h for x86_64 (covers Linux 5.15+)
RUN curl -fsSL \
    "https://raw.githubusercontent.com/libbpf/libbpf-bootstrap/master/vmlinux/x86/vmlinux.h" \
    -o vmlinux.h

# Copy all probe sources
WORKDIR /src
COPY probes/ ./probes/
COPY Makefile ./

# Compile all probes to .bpf.o
RUN make probes CLANG=clang

# ─── Stage 2: Go application builder ─────────────────────────────────────────
FROM golang:1.22-bookworm AS go-builder

WORKDIR /app

# Cache dependency downloads separately from source builds
COPY go.mod go.sum ./
RUN go mod download

# Copy source
COPY collector/ ./collector/

# Build the collector binary
# CGO_ENABLED=0 for a fully static binary (works without glibc in scratch)
# However we need libbpf which requires cgo — use CGO_ENABLED=1 with static libbpf
RUN apt-get update && apt-get install -y --no-install-recommends \
        libbpf-dev \
        libelf-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

ENV CGO_ENABLED=1
ENV GOOS=linux
ENV GOARCH=${TARGETARCH}

RUN go build \
        -ldflags="-extldflags '-static' -s -w \
                  -X main.BuildVersion=$(git describe --tags --always 2>/dev/null || echo dev) \
                  -X main.BuildTime=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        -tags netgo,osusergo \
        -o /app/collector \
        ./collector/

# ─── Stage 3: Minimal runtime image ──────────────────────────────────────────
# Uses Debian Bookworm slim to get the dynamic linker, libbpf, and libelf
# that the CGO binary needs at runtime. NOT scratch because libbpf requires them.
FROM debian:bookworm-slim

# Runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
        libbpf1 \
        libelf1 \
        zlib1g \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r ccdt \
    && useradd -r -g ccdt -s /sbin/nologin ccdt

# eBPF programs must be loaded from root (or with CAP_BPF + CAP_SYS_ADMIN).
# The container runs as root in the DaemonSet but drops all other caps.
# See infra/kubernetes/layer1-nervous/daemonset.yaml for the securityContext.

WORKDIR /app

# Copy compiled eBPF objects from stage 1
COPY --from=ebpf-builder /src/probes/*.bpf.o ./probes/

# Copy the Go binary from stage 2
COPY --from=go-builder /app/collector ./collector

# Expose Prometheus metrics + REST API port
EXPOSE 9100

# Health check — used by Kubernetes liveness probe
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9100/health || exit 1

# Run as root — required for eBPF program loading
# The DaemonSet spec sets: securityContext.capabilities.add: [BPF, PERFMON, SYS_ADMIN]
USER root

ENV BPF_OBJECT_DIR=/app/probes \
    LISTEN_ADDR=:9100 \
    LOG_LEVEL=INFO

ENTRYPOINT ["/app/collector"]
