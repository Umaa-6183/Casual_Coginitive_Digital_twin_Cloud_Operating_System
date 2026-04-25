# ══════════════════════════════════════════════════════════════════════════════
# CCDT — Developer Makefile
# ══════════════════════════════════════════════════════════════════════════════
#
# Quick start:
#   make setup      — first-time env setup (copy .env, install pre-commit)
#   make dev        — start full local stack (all layers + infra)
#   make test       — run all tests (unit + integration + e2e)
#   make lint       — run all linters
#   make build      — build all Docker images
#   make help       — show this message
#
# ══════════════════════════════════════════════════════════════════════════════

SHELL := /bin/bash
.ONESHELL:   # run each recipe in a single shell (preserves cd)
.SHELLFLAGS  := -eu -o pipefail -c
.DEFAULT_GOAL := help

# ── Project config ─────────────────────────────────────────────────────────────
PROJECT       := ccdt
REGISTRY      := ghcr.io/your-org/ccdt
VERSION       ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")
GIT_SHA       := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BUILD_DATE    := $(shell date -u +"%Y-%m-%dT%H:%M:%SZ")

# ── Python config ──────────────────────────────────────────────────────────────
PYTHON        := python3
PIP           := pip3
PYTHONPATH    := .
PYTEST        := PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest
RUFF          := $(PYTHON) -m ruff
MYPY          := $(PYTHON) -m mypy

# ── Go config ──────────────────────────────────────────────────────────────────
GO            := go
GOLINT        := golangci-lint

# ── Docker compose ─────────────────────────────────────────────────────────────
DC            := docker compose
DC_MON        := $(DC) --profile monitoring
DC_EBPF       := $(DC) --profile ebpf
DC_TOOLS      := $(DC) --profile tools

# ── Colours ────────────────────────────────────────────────────────────────────
BOLD  := $(shell tput bold 2>/dev/null || echo "")
RESET := $(shell tput sgr0 2>/dev/null || echo "")
GREEN := $(shell tput setaf 2 2>/dev/null || echo "")
BLUE  := $(shell tput setaf 4 2>/dev/null || echo "")
CYAN  := $(shell tput setaf 6 2>/dev/null || echo "")

# ══════════════════════════════════════════════════════════════════════════════
# Help
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: help
help: ## Show this help message
	@echo "$(BOLD)$(BLUE)CCDT — Cognitive Digital Twin$(RESET)"
	@echo "$(CYAN)Version: $(VERSION)  SHA: $(GIT_SHA)$(RESET)"
	@echo ""
	@echo "$(BOLD)Usage:$(RESET)"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ { \
		printf "  $(GREEN)%-26s$(RESET) %s\n", $$1, $$2 \
	}' $(MAKEFILE_LIST)
	@echo ""
	@echo "$(BOLD)Profiles (append to docker commands):$(RESET)"
	@echo "  $(GREEN)make dev-monitoring$(RESET)        — include Prometheus + Grafana"
	@echo "  $(GREEN)make dev-ebpf$(RESET)              — include Layer-1 eBPF (Linux + root)"
	@echo "  $(GREEN)make dev-tools$(RESET)             — include Kafka UI"

# ══════════════════════════════════════════════════════════════════════════════
# First-time setup
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: setup
setup: ## First-time project setup (copy .env, install hooks, install shared lib)
	@echo "$(BOLD)Setting up CCDT development environment ...$(RESET)"

	# Copy example env file
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "  ✓ Created .env from .env.example — set ANTHROPIC_API_KEY"; \
	else \
		echo "  ✓ .env already exists"; \
	fi

	# Install pre-commit hooks
	@if command -v pre-commit &>/dev/null; then \
		pre-commit install; \
		echo "  ✓ pre-commit hooks installed"; \
	else \
		echo "  ⚠ pre-commit not found — run: pip install pre-commit"; \
	fi

	# Install shared library in editable mode
	@$(PIP) install -e shared/ --quiet
	@echo "  ✓ ccdt-shared installed"

	# Install root-level test dependencies
	@$(PIP) install \
		pytest==8.2.2 \
		pytest-asyncio==0.23.7 \
		pytest-cov==5.0.0 \
		pytest-timeout==2.3.1 \
		ruff==0.4.8 \
		mypy==1.10.0 \
		--quiet
	@echo "  ✓ Python test/lint tools installed"
	@echo ""
	@echo "$(GREEN)Setup complete! Run: make dev$(RESET)"

.PHONY: setup-shared
setup-shared: ## Re-install the shared library (after proto/schema changes)
	$(PIP) install -e shared/ --quiet
	@echo "✓ ccdt-shared reinstalled"

# ══════════════════════════════════════════════════════════════════════════════
# Local development
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: dev
dev: ## Start full local stack (Kafka + Redis + OPA + Layers 2-4 + Gateway + Dashboard)
	@echo "$(BOLD)Starting CCDT local stack ...$(RESET)"
	$(DC) up -d
	@echo ""
	@echo "$(GREEN)$(BOLD)CCDT is running:$(RESET)"
	@echo "  Dashboard   →  http://localhost:3000"
	@echo "  API Gateway →  http://localhost:8000"
	@echo "  GNN Server  →  http://localhost:8001"
	@echo "  Guardian    →  http://localhost:8002"
	@echo "  Co-Pilot    →  http://localhost:8003"
	@echo "  OPA         →  http://localhost:8181"
	@echo "  Kafka       →  localhost:9092"
	@echo ""
	@echo "Logs: make dev-logs | Status: make dev-status"

.PHONY: dev-monitoring
dev-monitoring: ## Start with Prometheus + Grafana
	$(DC_MON) up -d
	@echo "Grafana → http://localhost:3001 (admin / ccdt-dev)"
	@echo "Prometheus → http://localhost:9090"

.PHONY: dev-ebpf
dev-ebpf: ## Start with Layer-1 eBPF collector (requires Linux + root)
	@if [ "$(shell uname)" != "Linux" ]; then \
		echo "Layer-1 eBPF requires Linux. Current OS: $(shell uname)"; \
		exit 1; \
	fi
	$(DC_EBPF) up -d

.PHONY: dev-tools
dev-tools: ## Start with Kafka UI
	$(DC_TOOLS) up -d
	@echo "Kafka UI → http://localhost:8080"

.PHONY: dev-logs
dev-logs: ## Tail logs from all running services
	$(DC) logs -f --tail=50

.PHONY: dev-logs-%
dev-logs-%: ## Tail logs for a specific service (e.g. make dev-logs-layer2-gnn)
	$(DC) logs -f --tail=100 $*

.PHONY: dev-status
dev-status: ## Show status of all running containers
	$(DC) ps

.PHONY: dev-down
dev-down: ## Stop and remove all containers (preserves volumes)
	$(DC) down

.PHONY: dev-clean
dev-clean: ## Stop containers AND remove volumes (complete reset)
	$(DC) down -v --remove-orphans
	@echo "✓ All containers and volumes removed"

.PHONY: dev-restart-%
dev-restart-%: ## Restart a specific service (e.g. make dev-restart-layer3-guardian)
	$(DC) restart $*
	$(DC) logs -f --tail=30 $*

# ══════════════════════════════════════════════════════════════════════════════
# Build
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: build
build: ## Build all Docker images
	$(DC) build \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg GIT_SHA=$(GIT_SHA) \
		--build-arg VERSION=$(VERSION)

.PHONY: build-%
build-%: ## Build a specific service image (e.g. make build-layer2-gnn)
	$(DC) build $* \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg GIT_SHA=$(GIT_SHA) \
		--build-arg VERSION=$(VERSION)

.PHONY: layer1-build
layer1-build: ## Build Layer-1 Go eBPF agent (native — no Docker)
	@echo "$(BOLD)Building Layer-1 eBPF agent ...$(RESET)"
	$(MAKE) -C services/layer1-nervous all
	@echo "✓ Layer-1 binary at services/layer1-nervous/bin/collector"

.PHONY: push
push: build ## Build and push all images to GHCR (requires docker login)
	@echo "$(BOLD)Pushing images to $(REGISTRY) ...$(RESET)"
	for svc in layer1-nervous layer2-cognitive layer3-guardian layer4-copilot api-gateway dashboard; do \
		docker tag ccdt/$${svc}:dev $(REGISTRY)/$${svc}:$(VERSION); \
		docker push $(REGISTRY)/$${svc}:$(VERSION); \
		echo "  ✓ Pushed $(REGISTRY)/$${svc}:$(VERSION)"; \
	done

# ══════════════════════════════════════════════════════════════════════════════
# Testing
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: test
test: test-shared test-unit test-integration test-e2e ## Run all tests (shared → unit → integration → e2e)

.PHONY: test-shared
test-shared: ## Run shared library unit tests
	@echo "$(BOLD)Running shared library tests ...$(RESET)"
	$(PYTEST) shared/tests/ \
		-v --tb=short \
		--cov=shared \
		--cov-report=term-missing \
		--cov-fail-under=80

.PHONY: test-unit
test-unit: ## Run all layer unit tests (no external services)
	@echo "$(BOLD)Running unit tests ...$(RESET)"
	$(PYTEST) tests/unit/ \
		-v --tb=short \
		-m "unit" \
		--cov=services --cov=apps \
		--cov-report=term-missing \
		--cov-report=html:.coverage-reports/unit \
		--timeout=120

.PHONY: test-integration
test-integration: ## Run integration tests (in-memory Kafka, no Docker required)
	@echo "$(BOLD)Running integration tests ...$(RESET)"
	$(PYTEST) tests/integration/ \
		-v --tb=short \
		-m "integration" \
		--cov=services --cov=apps \
		--cov-report=xml:.coverage-reports/integration.xml \
		--timeout=120

.PHONY: test-e2e
test-e2e: ## Run E2E tests (full 4-layer pipeline, mock transports)
	@echo "$(BOLD)Running E2E tests ...$(RESET)"
	$(PYTEST) tests/e2e/ \
		-v --tb=long \
		-m "e2e" \
		--timeout=180

.PHONY: test-chaos
test-chaos: ## Run chaos test suite
	@echo "$(BOLD)Running chaos tests ...$(RESET)"
	$(PYTEST) tests/chaos/ \
		-v --tb=short \
		-m "chaos" \
		--timeout=300

.PHONY: chaos-run
chaos-run: ## Run chaos runner CLI with resilience gate (≥80%)
	$(PYTHON) tests/chaos/chaos_runner.py \
		--verbose \
		--fail-below 0.80 \
		--output .chaos-report.json
	@echo "Report saved to .chaos-report.json"

.PHONY: chaos-run-%
chaos-run-%: ## Run specific chaos suite (e.g. make chaos-run-kafka)
	$(PYTHON) tests/chaos/chaos_runner.py \
		--verbose \
		--suite $* \
		--fail-below 0.80

.PHONY: test-layer1
test-layer1: ## Run Layer-1 Go tests
	@echo "$(BOLD)Running Layer-1 Go tests ...$(RESET)"
	cd services/layer1-nervous && \
		$(GO) test ./collector/... -v -race -count=1 -timeout=120s

.PHONY: test-layer2
test-layer2: ## Run Layer-2 GNN unit tests
	$(PYTEST) tests/unit/test_layer2_gnn.py -v --tb=short -m "unit and layer2"

.PHONY: test-layer3
test-layer3: ## Run Layer-3 Guardian unit tests
	$(PYTEST) tests/unit/test_layer3_guardian.py -v --tb=short -m "unit and layer3"

.PHONY: test-layer4
test-layer4: ## Run Layer-4 Co-Pilot unit tests
	$(PYTEST) tests/unit/test_layer4_copilot.py -v --tb=short -m "unit and layer4"

.PHONY: test-opa
test-opa: ## Validate and test OPA Rego policies
	@echo "$(BOLD)Validating OPA policies ...$(RESET)"
	@for policy in services/layer3-guardian/opa/policies/*.rego; do \
		echo -n "  Checking $$(basename $$policy) ... "; \
		opa check "$$policy" && echo "OK"; \
	done
	@if ls services/layer3-guardian/opa/policies/*_test.rego 1>/dev/null 2>&1; then \
		opa test services/layer3-guardian/opa/policies/ -v; \
	else \
		echo "  (no *_test.rego files found)"; \
	fi

.PHONY: test-dashboard
test-dashboard: ## Run Dashboard Vitest unit + component tests
	cd apps/dashboard && npm test -- --reporter=verbose

.PHONY: coverage
coverage: ## Run all tests with coverage, open HTML report
	$(PYTEST) tests/ \
		--ignore=tests/chaos \
		--cov=services --cov=apps --cov=shared \
		--cov-report=html:.coverage-reports/all \
		--cov-report=term-missing \
		--cov-fail-under=75
	@echo "Opening coverage report ..."
	@open .coverage-reports/all/index.html 2>/dev/null || \
		xdg-open .coverage-reports/all/index.html 2>/dev/null || \
		echo "Open .coverage-reports/all/index.html in your browser"

# ══════════════════════════════════════════════════════════════════════════════
# Linting & formatting
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: lint
lint: lint-go lint-python lint-dashboard lint-helm ## Run all linters

.PHONY: lint-go
lint-go: ## Lint Layer-1 Go code
	@echo "$(BOLD)Go lint ...$(RESET)"
	cd services/layer1-nervous && $(GO) vet ./collector/...
	cd services/layer1-nervous && $(GOLINT) run ./collector/... --timeout=5m

.PHONY: lint-python
lint-python: ## Lint all Python services and shared library
	@echo "$(BOLD)Python lint (ruff) ...$(RESET)"
	$(RUFF) check \
		shared/ \
		services/layer2-cognitive/ \
		services/layer3-guardian/ \
		services/layer4-copilot/ \
		apps/api-gateway/ \
		tests/ \
		--output-format=text

.PHONY: lint-dashboard
lint-dashboard: ## Lint Dashboard TypeScript/React
	@echo "$(BOLD)Dashboard lint (eslint) ...$(RESET)"
	cd apps/dashboard && npm run lint

.PHONY: lint-helm
lint-helm: ## Lint Helm chart
	@echo "$(BOLD)Helm lint ...$(RESET)"
	helm lint infra/helm/ccdt \
		--strict \
		--set global.imageTag=lint \
		--set secrets.anthropicApiKey=sk-ant-test

.PHONY: fmt
fmt: fmt-python fmt-go ## Auto-format all code

.PHONY: fmt-python
fmt-python: ## Auto-format Python code with ruff
	$(RUFF) format \
		shared/ \
		services/layer2-cognitive/ \
		services/layer3-guardian/ \
		services/layer4-copilot/ \
		apps/api-gateway/ \
		tests/
	$(RUFF) check --fix \
		shared/ \
		services/layer2-cognitive/ \
		services/layer3-guardian/ \
		services/layer4-copilot/ \
		apps/api-gateway/ \
		tests/

.PHONY: fmt-go
fmt-go: ## Auto-format Go code
	cd services/layer1-nervous && $(GO) fmt ./...

.PHONY: typecheck
typecheck: ## Run mypy type checking on all Python services
	@echo "$(BOLD)Type checking (mypy) ...$(RESET)"
	$(MYPY) \
		services/layer2-cognitive/ \
		services/layer3-guardian/ \
		services/layer4-copilot/ \
		apps/api-gateway/ \
		--ignore-missing-imports \
		--no-strict-optional \
		--follow-imports=skip \
		--exclude "training/"

# ══════════════════════════════════════════════════════════════════════════════
# Training
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: train-gnn
train-gnn: ## Train the Causal GNN model (Layer-2) — ~10 min on CPU
	@echo "$(BOLD)Training Causal GNN ...$(RESET)"
	@mkdir -p checkpoints/gnn checkpoints/rl
	$(DC) run --rm \
		--entrypoint "" \
		-e PYTHONPATH=/app \
		layer2-gnn \
		python train_gnn.py \
		--epochs 50 \
		--num-samples 4000 \
		--num-workers 0 \
		--device cpu \
		--checkpoint-dir /app/checkpoints

.PHONY: train-gnn-quick
train-gnn-quick: ## Quick GNN smoke-test (3 epochs, 200 samples — ~30 sec)
	@echo "$(BOLD)Quick GNN training smoke test ...$(RESET)"
	@mkdir -p checkpoints/gnn checkpoints/rl
	$(DC) run --rm \
		--entrypoint "" \
		-e PYTHONPATH=/app \
		layer2-gnn \
		python train_gnn.py --quick --checkpoint-dir /app/checkpoints

.PHONY: train-rl
train-rl: ## Train the Guardian RL agent (Layer-3) — ~25 min on CPU
	@echo "$(BOLD)Training Guardian RL agent ...$(RESET)"
	@mkdir -p checkpoints/gnn checkpoints/rl
	$(DC) run --rm \
		--entrypoint "" \
		-e PYTHONPATH=/app \
		layer3-guardian \
		python train_rl.py \
		--timesteps 500000 \
		--n-envs 2 \
		--device cpu \
		--checkpoint-dir /app/checkpoints

.PHONY: train-rl-quick
train-rl-quick: ## Quick RL smoke-test (5000 timesteps, 1 env — ~60 sec)
	@echo "$(BOLD)Quick RL training smoke test ...$(RESET)"
	@mkdir -p checkpoints/gnn checkpoints/rl
	$(DC) run --rm \
		--entrypoint "" \
		-e PYTHONPATH=/app \
		layer3-guardian \
		python train_rl.py --quick --checkpoint-dir /app/checkpoints

.PHONY: eval-gnn
eval-gnn: ## Evaluate trained GNN on held-out test scenarios
	@mkdir -p checkpoints/gnn checkpoints/rl
	$(DC) run --rm \
		--entrypoint "" \
		-e PYTHONPATH=/app \
		layer2-gnn \
		python train_gnn.py \
		--epochs 1 \
		--num-samples 200 \
		--num-workers 0 \
		--device cpu \
		--checkpoint-dir /app/checkpoints

# ══════════════════════════════════════════════════════════════════════════════
# Kubernetes
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: k8s-deploy
k8s-deploy: ## Deploy to Kubernetes (current kubectl context)
	@echo "$(BOLD)Deploying CCDT to Kubernetes ...$(RESET)"
	@echo "Context: $$(kubectl config current-context)"
	@echo "Namespace: ccdt"
	@read -p "Continue? [y/N] " ans && [ "$$ans" = "y" ] || exit 0
	kubectl apply -f infra/kubernetes/namespace.yaml
	kubectl apply -f infra/kubernetes/secrets.yaml         --namespace ccdt || true
	kubectl apply -f infra/kubernetes/layer1-nervous/      --namespace ccdt
	kubectl apply -f infra/kubernetes/layer2-cognitive/    --namespace ccdt
	kubectl apply -f infra/kubernetes/layer3-guardian/     --namespace ccdt
	kubectl apply -f infra/kubernetes/layer4-copilot/      --namespace ccdt
	kubectl apply -f infra/kubernetes/api-gateway/         --namespace ccdt
	kubectl apply -f infra/kubernetes/monitoring/          --namespace ccdt
	@echo "$(GREEN)✓ CCDT deployed$(RESET)"

.PHONY: k8s-helm-install
k8s-helm-install: ## Install CCDT via Helm
	@echo "Installing CCDT via Helm (version=$(VERSION)) ..."
	helm repo add bitnami             https://charts.bitnami.com/bitnami
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update
	cd infra/helm/ccdt && helm dependency update
	helm upgrade --install ccdt ./infra/helm/ccdt \
		--namespace ccdt \
		--create-namespace \
		--set global.imageTag=$(VERSION) \
		--set global.imageRegistry=$(REGISTRY) \
		--set secrets.anthropicApiKey=$${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY not set} \
		--wait --timeout 15m

.PHONY: k8s-status
k8s-status: ## Show Kubernetes pod status
	kubectl get pods -n ccdt -o wide

.PHONY: k8s-logs-%
k8s-logs-%: ## Tail K8s logs for service (e.g. make k8s-logs-layer2-gnn)
	kubectl logs -n ccdt \
		-l app=ccdt-$* \
		-f --tail=100

.PHONY: k8s-forward
k8s-forward: ## Port-forward all services to localhost
	kubectl port-forward svc/ccdt-api-gateway   -n ccdt 8000:8000 &
	kubectl port-forward svc/ccdt-layer2-cognitive -n ccdt 8001:8001 &
	kubectl port-forward svc/ccdt-layer3-guardian  -n ccdt 8002:8002 &
	kubectl port-forward svc/ccdt-layer4-copilot   -n ccdt 8003:8003 &
	kubectl port-forward svc/ccdt-dashboard        -n ccdt 3000:3000 &
	@echo "Port-forwarding active. Kill all with: pkill -f 'kubectl port-forward'"

.PHONY: k8s-guardian-supervised
k8s-guardian-supervised: ## Switch Guardian to supervised autonomy mode (K8s)
	kubectl set env deployment/ccdt-layer3-guardian \
		-n ccdt AUTONOMY_MODE=supervised
	kubectl rollout status deployment/ccdt-layer3-guardian -n ccdt

.PHONY: k8s-guardian-human-loop
k8s-guardian-human-loop: ## Switch Guardian to human-in-loop mode (K8s — safest)
	kubectl set env deployment/ccdt-layer3-guardian \
		-n ccdt AUTONOMY_MODE=human-in-loop
	kubectl rollout status deployment/ccdt-layer3-guardian -n ccdt

.PHONY: helm-render
helm-render: ## Render Helm templates to stdout (for inspection)
	helm template ccdt ./infra/helm/ccdt \
		--namespace ccdt \
		--set global.imageTag=$(VERSION) \
		--set secrets.anthropicApiKey=sk-ant-dev

.PHONY: helm-diff
helm-diff: ## Show diff between deployed and local Helm chart (requires helm-diff plugin)
	helm diff upgrade ccdt ./infra/helm/ccdt \
		--namespace ccdt \
		--set global.imageTag=$(VERSION)

# ══════════════════════════════════════════════════════════════════════════════
# Proto
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: proto
proto: ## Regenerate Python proto shims from .proto files
	@echo "$(BOLD)Regenerating proto shims ...$(RESET)"
	$(MAKE) -C shared proto
	@echo "✓ Proto shims regenerated"

.PHONY: proto-validate
proto-validate: ## Validate all proto files compile correctly
	$(MAKE) -C shared lint

# ══════════════════════════════════════════════════════════════════════════════
# Observability
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: metrics
metrics: ## Query metrics from all running services
	@echo "$(BOLD)Layer-2 GNN metrics:$(RESET)"
	@curl -s http://localhost:8001/metrics | grep -E "^ccdt_" | head -20
	@echo ""
	@echo "$(BOLD)Layer-3 Guardian metrics:$(RESET)"
	@curl -s http://localhost:8002/metrics | grep -E "^ccdt_" | head -20
	@echo ""
	@echo "$(BOLD)Layer-4 Co-Pilot metrics:$(RESET)"
	@curl -s http://localhost:8003/metrics | grep -E "^ccdt_" | head -20

.PHONY: health
health: ## Check health of all running services
	@echo "$(BOLD)CCDT Service Health:$(RESET)"
	@services="8000 8001 8002 8003"; \
	names="api-gateway layer2-gnn layer3-guardian layer4-copilot"; \
	for port in $$services; do \
		status=$$(curl -fsS -o /dev/null -w "%{http_code}" \
			http://localhost:$$port/health --connect-timeout 2 2>/dev/null || echo "000"); \
		icon="✅"; [ "$$status" != "200" ] && icon="❌"; \
		echo "  $$icon  :$$port  HTTP $$status"; \
	done
	@echo -n "  OPA :8181  "; \
	status=$$(curl -fsS -o /dev/null -w "%{http_code}" http://localhost:8181/health --connect-timeout 2 2>/dev/null || echo "000"); \
    [ "$$status" = "200" ] && echo "✅ healthy" || echo "❌ unhealthy"

# ══════════════════════════════════════════════════════════════════════════════
# Cleanup
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: clean
clean: ## Remove build artifacts, .pyc, __pycache__, coverage files
	@echo "$(BOLD)Cleaning build artifacts ...$(RESET)"
	# Python
	find . -name "*.pyc"        -delete
	find . -name "__pycache__"  -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.egg-info"   -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name ".pytest_cache" -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name ".mypy_cache"   -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name ".ruff_cache"   -type d -exec rm -rf {} + 2>/dev/null || true
	# Go
	rm -f services/layer1-nervous/bin/collector
	rm -f services/layer1-nervous/coverage-layer1.out
	# Coverage reports
	rm -rf .coverage-reports/
	rm -f coverage*.xml .coverage
	# Chaos report
	rm -f .chaos-report.json
	# Dashboard build
	rm -rf apps/dashboard/dist apps/dashboard/.vite
	@echo "✓ Clean complete"

.PHONY: clean-docker
clean-docker: ## Remove all CCDT Docker images (frees disk)
	docker rmi $$(docker images 'ccdt/*' -q) 2>/dev/null || true
	@echo "✓ CCDT Docker images removed"

# ══════════════════════════════════════════════════════════════════════════════
# CI simulation (run what CI would run, locally)
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: ci
ci: lint test build ## Simulate full CI pipeline locally
	@echo "$(GREEN)$(BOLD)✓ All CI checks passed locally$(RESET)"

.PHONY: ci-fast
ci-fast: lint-python test-shared test-unit ## Fast CI subset (lint + unit tests only, ~2 min)
	@echo "$(GREEN)✓ Fast CI checks passed$(RESET)"

# Prevent accidental deletion of phony targets
.PHONY: all
all: help
