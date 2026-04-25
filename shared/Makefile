# ═══════════════════════════════════════════════════════════════════════════════
# CCDT Shared — Makefile
# ═══════════════════════════════════════════════════════════════════════════════
# Usage:
#   make install        Install ccdt-shared in editable mode (dev extras)
#   make proto          Generate Python pb2 stubs from .proto files
#   make lint           Run ruff + mypy
#   make test           Run pytest with coverage
#   make clean          Remove generated artefacts and caches
#   make all            install + proto + lint + test
# ═══════════════════════════════════════════════════════════════════════════════

SHELL  := /bin/bash
PYTHON := python3
PIP    := pip3
PROTOC := python3 -m grpc_tools.protoc

PROTO_DIR    := proto
GENERATED    := proto/generated
PROTO_FILES  := $(wildcard $(PROTO_DIR)/*.proto)
PROTO_SRCS   := events.proto graph.proto actions.proto copilot.proto

.PHONY: all install proto proto-check lint format test clean help

# ── Default target ─────────────────────────────────────────────────────────────
all: install proto lint test

help:
	@echo "CCDT Shared Makefile targets:"
	@echo "  install      Install ccdt-shared in editable mode with dev extras"
	@echo "  proto        Compile .proto → Python pb2 stubs"
	@echo "  proto-check  Verify proto files can be compiled (dry-run)"
	@echo "  lint         Run ruff + mypy"
	@echo "  format       Auto-format with ruff + black"
	@echo "  test         Run pytest with coverage"
	@echo "  clean        Remove generated artefacts and __pycache__"

# ── Install ────────────────────────────────────────────────────────────────────
install:
	@echo "→ Installing ccdt-shared in editable mode..."
	$(PIP) install -e ".[dev,proto,structlog]" --quiet
	@echo "✓ ccdt-shared installed"

# ── Protocol Buffer code generation ───────────────────────────────────────────
proto: $(GENERATED)/__init__.py $(patsubst %.proto,$(GENERATED)/%_pb2.py,$(PROTO_SRCS))
	@echo "✓ Proto stubs generated in $(GENERATED)/"

# Compile each proto file → pb2.py + pb2.pyi + pb2_grpc.py
$(GENERATED)/%_pb2.py: $(PROTO_DIR)/%.proto
	@echo "→ Compiling $<..."
	@mkdir -p $(GENERATED)
	$(PROTOC) \
		--proto_path=$(PROTO_DIR) \
		--proto_path=$(shell python3 -c "import grpc_tools; import os; print(os.path.dirname(grpc_tools.__file__))") \
		--python_out=$(GENERATED) \
		--pyi_out=$(GENERATED) \
		--grpc_python_out=$(GENERATED) \
		$<
	@# Fix relative imports in generated gRPC files (protoc uses absolute imports)
	@if [ -f $(GENERATED)/$*_pb2_grpc.py ]; then \
		sed -i 's/^import \(.*\)_pb2/from ccdt.shared.proto.generated import \1_pb2/' \
			$(GENERATED)/$*_pb2_grpc.py 2>/dev/null || true; \
	fi
	@echo "✓ Generated $(GENERATED)/$*_pb2.py"

proto-check:
	@echo "→ Checking proto files for syntax errors..."
	@which protoc > /dev/null 2>&1 || $(PIP) install grpcio-tools --quiet
	@for proto in $(PROTO_SRCS); do \
		echo "  Checking $$proto..."; \
		$(PROTOC) \
			--proto_path=$(PROTO_DIR) \
			--proto_path=$(shell python3 -c "import grpc_tools; import os; print(os.path.dirname(grpc_tools.__file__))") \
			--python_out=/tmp \
			$(PROTO_DIR)/$$proto && echo "  ✓ $$proto OK" || echo "  ✗ $$proto FAILED"; \
	done

# ── Linting ────────────────────────────────────────────────────────────────────
lint:
	@echo "→ Running ruff..."
	$(PYTHON) -m ruff check . --exclude generated/
	@echo "→ Running mypy..."
	$(PYTHON) -m mypy ccdt/shared --ignore-missing-imports \
		--exclude "generated/" \
		--exclude "tests/" || true
	@echo "✓ Linting complete"

format:
	@echo "→ Formatting with ruff..."
	$(PYTHON) -m ruff check . --fix --exclude generated/
	$(PYTHON) -m ruff format . --exclude generated/
	@echo "✓ Formatting complete"

# ── Tests ──────────────────────────────────────────────────────────────────────
test:
	@echo "→ Running pytest..."
	$(PYTHON) -m pytest tests/ -v \
		--cov=ccdt.shared \
		--cov-report=term-missing \
		--cov-report=xml:coverage.xml \
		--cov-fail-under=80 \
		-x
	@echo "✓ Tests complete"

test-fast:
	$(PYTHON) -m pytest tests/ -v -x --no-cov

# ── Clean ──────────────────────────────────────────────────────────────────────
clean:
	@echo "→ Cleaning generated files and caches..."
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@rm -f coverage.xml .coverage
	@rm -rf build/ dist/ .ruff_cache/ .mypy_cache/ .pytest_cache/
	@echo "  (Keeping proto shim files in $(GENERATED)/ — run make proto to regenerate)"
	@echo "✓ Clean complete"

clean-proto:
	@echo "→ Removing compiled proto stubs..."
	@find $(GENERATED) -name "*_pb2*.py" -not -name "__init__*" -delete
	@find $(GENERATED) -name "*.pyi"     -delete
	@echo "✓ Proto stubs removed (shims preserved)"
