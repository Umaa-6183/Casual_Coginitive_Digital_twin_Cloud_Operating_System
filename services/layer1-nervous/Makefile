# CCDT Layer-1 Nervous System — Makefile
#
# Targets:
#   make probes        — compile all .bpf.c → .bpf.o (requires clang + libbpf)
#   make vmlinux       — generate vmlinux.h from running kernel (requires bpftool)
#   make build         — compile Go collector binary
#   make docker        — build Docker image
#   make push          — push Docker image to registry
#   make test          — run Go unit tests
#   make lint          — run golangci-lint
#   make clean         — remove all build artefacts
#   make dev           — build + run locally without Docker (for development)
#   make fmt           — gofmt all Go sources

# ─── Configuration ────────────────────────────────────────────────────────────
REGISTRY    ?= ghcr.io/ccdt
IMAGE_NAME  ?= layer1-nervous
IMAGE_TAG   ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
IMAGE       := $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

# Go settings
BINARY      := collector
GO          := go
GOFLAGS     := -ldflags="-s -w -X main.BuildVersion=$(IMAGE_TAG) \
                          -X main.BuildTime=$(shell date -u +%Y-%m-%dT%H:%M:%SZ)"
CGO_ENABLED := 1

# BPF settings
CLANG       ?= clang
LLC         ?= llc
BPFTOOL     ?= bpftool
ARCH        := $(shell uname -m | sed 's/x86_64/x86/' | sed 's/aarch64/arm64/')

# Probe source and output directories
PROBE_SRC_DIR  := probes
PROBE_OBJ_DIR  := probes
VMLINUX_HEADER := probes/include/vmlinux.h

# All probe source files
PROBE_SOURCES := $(wildcard $(PROBE_SRC_DIR)/*.bpf.c)
PROBE_OBJECTS := $(PROBE_SOURCES:.bpf.c=.bpf.o)

# Clang flags for BPF CO-RE compilation
BPF_CFLAGS := \
    -g \
    -O2 \
    -target bpf \
    -D__TARGET_ARCH_$(ARCH) \
    -I$(PROBE_SRC_DIR)/include \
    -I/usr/include/$(shell uname -m)-linux-gnu \
    -Wno-unused-value \
    -Wno-pointer-sign \
    -Wno-compare-distinct-pointer-types \
    -fno-stack-protector

# ─── Default target ───────────────────────────────────────────────────────────
.PHONY: all
all: probes build

# ─── vmlinux.h ────────────────────────────────────────────────────────────────
# Generate vmlinux.h from the running kernel's BTF data.
# Requires: bpftool and kernel built with CONFIG_DEBUG_INFO_BTF=y
.PHONY: vmlinux
vmlinux: $(VMLINUX_HEADER)

$(VMLINUX_HEADER):
	@echo "Generating vmlinux.h from running kernel..."
	@mkdir -p $(PROBE_SRC_DIR)/include
	@if command -v $(BPFTOOL) >/dev/null 2>&1; then \
	    $(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@; \
	    echo "Generated: $@"; \
	else \
	    echo "bpftool not found — downloading pre-built vmlinux.h..."; \
	    curl -fsSL \
	      "https://raw.githubusercontent.com/libbpf/libbpf-bootstrap/master/vmlinux/x86/vmlinux.h" \
	      -o $@; \
	    echo "Downloaded: $@"; \
	fi

# ─── BPF probe compilation ────────────────────────────────────────────────────
.PHONY: probes
probes: $(VMLINUX_HEADER) $(PROBE_OBJECTS)
	@echo "All probes compiled: $(PROBE_OBJECTS)"

# Pattern rule: compile each .bpf.c → .bpf.o
$(PROBE_OBJ_DIR)/%.bpf.o: $(PROBE_SRC_DIR)/%.bpf.c $(VMLINUX_HEADER)
	@echo "  BPF  $< → $@"
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@
	@# Strip debug symbols from BPF objects to reduce size
	@if command -v llvm-strip >/dev/null 2>&1; then \
	    llvm-strip -g $@; \
	fi

# ─── Go binary ────────────────────────────────────────────────────────────────
.PHONY: build
build: probes
	@echo "Building Go binary: $(BINARY)"
	CGO_ENABLED=$(CGO_ENABLED) GOOS=linux \
	    $(GO) build $(GOFLAGS) -o $(BINARY) ./collector/
	@echo "Built: ./$(BINARY)"

# ─── Go module management ────────────────────────────────────────────────────
.PHONY: mod-download
mod-download:
	$(GO) mod download

.PHONY: mod-tidy
mod-tidy:
	$(GO) mod tidy

# ─── Testing ──────────────────────────────────────────────────────────────────
.PHONY: test
test:
	@echo "Running Go unit tests..."
	CGO_ENABLED=0 $(GO) test -v -race -timeout 60s ./collector/...

.PHONY: test-cover
test-cover:
	CGO_ENABLED=0 $(GO) test -v -race -coverprofile=coverage.out ./collector/...
	$(GO) tool cover -html=coverage.out -o coverage.html
	@echo "Coverage report: coverage.html"

# ─── Linting ─────────────────────────────────────────────────────────────────
.PHONY: lint
lint:
	@if command -v golangci-lint >/dev/null 2>&1; then \
	    golangci-lint run ./collector/...; \
	else \
	    echo "golangci-lint not found — install from https://golangci-lint.run/"; \
	fi

.PHONY: fmt
fmt:
	$(GO) fmt ./collector/...
	@echo "Formatted"

.PHONY: vet
vet:
	$(GO) vet ./collector/...

# ─── Docker ───────────────────────────────────────────────────────────────────
.PHONY: docker
docker:
	@echo "Building Docker image: $(IMAGE)"
	docker build \
	    --build-arg TARGETARCH=$(shell uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/') \
	    -t $(IMAGE) \
	    -t $(REGISTRY)/$(IMAGE_NAME):latest \
	    -f Dockerfile \
	    .
	@echo "Built: $(IMAGE)"

.PHONY: push
push: docker
	docker push $(IMAGE)
	docker push $(REGISTRY)/$(IMAGE_NAME):latest

.PHONY: docker-run
docker-run: docker
	docker run --rm \
	    --privileged \
	    --pid=host \
	    --net=host \
	    -v /sys:/sys:ro \
	    -v /sys/kernel/btf/vmlinux:/sys/kernel/btf/vmlinux:ro \
	    -e NODE_NAME=$(shell hostname) \
	    -e KAFKA_BOOTSTRAP_SERVERS=kafka:9092 \
	    -e AUTH_DISABLED=true \
	    -p 9100:9100 \
	    $(IMAGE)

# ─── Dev (run without Docker) ─────────────────────────────────────────────────
# Requires: clang, libbpf-dev, Go 1.22+, and a Linux host with BTF support
.PHONY: dev
dev: build
	@echo "Running collector locally..."
	@echo "NOTE: Must run as root or with CAP_BPF + CAP_SYS_ADMIN"
	sudo ./$(BINARY) \
	    --kafka $(KAFKA_BOOTSTRAP_SERVERS) \
	    --node $(shell hostname) \
	    --listen :9100 \
	    --objects ./probes

# Check probe BPF verifier output (requires kernel 5.15+ and root)
.PHONY: verify-probes
verify-probes: probes
	@echo "Verifying BPF programs with kernel verifier..."
	@for obj in $(PROBE_OBJECTS); do \
	    echo "  Checking: $$obj"; \
	    $(BPFTOOL) prog load $$obj /sys/fs/bpf/ccdt_test && \
	    $(BPFTOOL) prog delete pinned /sys/fs/bpf/ccdt_test && \
	    echo "  OK: $$obj" || \
	    echo "  FAIL: $$obj"; \
	done

# List eBPF maps after loading (debug helper)
.PHONY: list-maps
list-maps:
	$(BPFTOOL) map list

# ─── Cleanup ─────────────────────────────────────────────────────────────────
.PHONY: clean
clean:
	@echo "Cleaning build artefacts..."
	rm -f $(PROBE_OBJECTS)
	rm -f $(BINARY)
	rm -f coverage.out coverage.html
	@echo "Clean"

.PHONY: clean-all
clean-all: clean
	rm -f $(VMLINUX_HEADER)

# ─── Help ─────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "CCDT Layer-1 Nervous System — Makefile targets"
	@echo "──────────────────────────────────────────────"
	@echo "  make probes         Compile all .bpf.c probes to .bpf.o objects"
	@echo "  make vmlinux        Generate probes/include/vmlinux.h from running kernel"
	@echo "  make build          Compile Go collector binary"
	@echo "  make docker         Build Docker image ($(IMAGE))"
	@echo "  make push           Build + push Docker image"
	@echo "  make docker-run     Build + run container locally (--privileged)"
	@echo "  make dev            Build + run binary directly on the host (requires root)"
	@echo "  make test           Run Go unit tests"
	@echo "  make lint           Run golangci-lint"
	@echo "  make fmt            Run gofmt"
	@echo "  make clean          Remove .bpf.o and binary artefacts"
	@echo "  make clean-all      Remove artefacts + vmlinux.h"
	@echo "  make verify-probes  Load each probe into kernel verifier (requires root)"
	@echo ""
	@echo "Configuration (override with environment variables):"
	@echo "  REGISTRY=$(REGISTRY)"
	@echo "  IMAGE_TAG=$(IMAGE_TAG)"
	@echo "  CLANG=$(CLANG)"
	@echo "  BPFTOOL=$(BPFTOOL)"
	@echo ""
