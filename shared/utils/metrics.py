"""
CCDT Shared — Prometheus Metrics Registry
═══════════════════════════════════════════════════════════════════════════════
Centralized Prometheus metrics definitions shared across all four CCDT layers.

Architecture
------------
Each layer imports this module to register its metrics into the SHARED registry.
The registry is exposed on every service's /metrics HTTP endpoint (port 9090)
via prometheus_client's WSGI app.

Design decisions
----------------
* All metric names are prefixed with "ccdt_" for easy Grafana filtering.
* Label cardinality is intentionally kept low (< 10 values per label).
* Histograms use custom buckets tuned for CCDT operation timing ranges.
* Every metric has a meaningful description for auto-generated dashboards.

Usage
-----
    from ccdt.shared.utils.metrics import (
        LAYER1_EBPF_EVENTS,
        LAYER2_GNN_LATENCY,
        LAYER3_ACTIONS_TOTAL,
        LAYER4_TOKENS_IN,
        registry,
        start_metrics_server,
    )

    # Increment a counter
    LAYER1_EBPF_EVENTS.labels(
        event_type="capability",
        node="ip-10-0-1-42",
        severity="HIGH",
    ).inc()

    # Observe a histogram
    with LAYER2_GNN_LATENCY.labels(node_count="10-20").time():
        result = model.infer(graph)

    # Set a gauge
    LAYER3_GUARDIAN_MODE.labels(mode="supervised").set(1)

    # Expose metrics endpoint
    start_metrics_server(port=9090)

Environment variables
---------------------
    METRICS_PORT     Port for the /metrics HTTP endpoint (default: 9090)
    SERVICE_NAME     Injected as the "service" label on all metrics
    LAYER            Injected as the "layer" label on all metrics
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        Info,
        Summary,
        generate_latest,
        start_http_server,
        REGISTRY as DEFAULT_REGISTRY,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    # Stub implementations — real metrics require: pip install prometheus-client
    _PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _LM:
        def inc(self, a=1): pass
        def dec(self, a=1): pass
        def set(self, v): pass
        def observe(self, v): pass
        def info(self, v): pass
        @property
        def _value(self):
            return type("V",(),{"get":lambda s:0.0})()

    class _M:
        def __init__(self, *a, **kw): pass
        def labels(self, **kw): return _LM()
        def inc(self, a=1): pass
        def set(self, v): pass
        def observe(self, v): pass
        def info(self, v): pass

    class Counter(_M): pass
    class Gauge(_M): pass
    class Histogram(_M):
        def time(self):
            import contextlib
            @contextlib.contextmanager
            def _c(): yield
            return _c()
    class Summary(_M): pass
    class Info:
        def __init__(self, *a, **kw): pass
        def info(self, v): pass

    class CollectorRegistry:
        def __init__(self, **kw): pass

    DEFAULT_REGISTRY = CollectorRegistry()

    def generate_latest(registry=None): return b"# prometheus_client not installed\n"
    def start_http_server(port, addr="0.0.0.0", registry=None): pass

# ── CCDT-scoped registry (separate from the default to avoid conflicts) ────────
registry = CollectorRegistry(auto_describe=True)

# ── Build information metric ───────────────────────────────────────────────────
CCDT_BUILD_INFO = Info(
    "ccdt_build",
    "CCDT platform build information",
    namespace="",
    registry=registry,
)

# ── Common histogram bucket sets ──────────────────────────────────────────────
# Tuned for CCDT's expected latency ranges

# Sub-millisecond to ~30 seconds: for HTTP service calls
HTTP_BUCKETS = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1,
    0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

# 1 ms to 120 seconds: for model inference (GNN forward pass can be slow on CPU)
INFERENCE_BUCKETS = (
    0.001, 0.005, 0.01, 0.05, 0.1, 0.25,
    0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0,
)

# 10 ms to 600 seconds: for LLM API calls (Claude streaming)
LLM_BUCKETS = (
    0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0,
    10.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0,
)

# Token count buckets (for Claude API token usage histograms)
TOKEN_BUCKETS = (
    10, 50, 100, 200, 500, 1000, 2000, 4000, 8000, 16000, 32000,
)

# Kubernetes action execution latency
K8S_ACTION_BUCKETS = (
    0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
)

# OPA evaluation latency
OPA_BUCKETS = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
)


def _c(name: str, doc: str, labels: list[str] | None = None) -> Counter:
    return Counter(name, doc, labels or [], namespace="ccdt", registry=registry)


def _h(name: str, doc: str, labels: list[str] | None = None,
       buckets: tuple = HTTP_BUCKETS) -> Histogram:
    return Histogram(name, doc, labels or [], buckets=buckets,
                     namespace="ccdt", registry=registry)


def _g(name: str, doc: str, labels: list[str] | None = None) -> Gauge:
    return Gauge(name, doc, labels or [], namespace="ccdt", registry=registry)


def _s(name: str, doc: str, labels: list[str] | None = None) -> Summary:
    return Summary(name, doc, labels or [], namespace="ccdt", registry=registry)


# ══════════════════════════════════════════════════════════════════════════════
# Layer-1 — eBPF Nervous System
# ══════════════════════════════════════════════════════════════════════════════

LAYER1_EBPF_EVENTS = _c(
    "ebpf_events_total",
    "Total eBPF events captured by the Layer-1 collector",
    ["event_type", "node", "severity"],
)
"""Labels: event_type ∈ {capability,oom_kill,tcp_retransmit,sched_latency,
                          file_access,syscall,execve,network_connect}
           node — Kubernetes node name
           severity ∈ {INFO,LOW,MEDIUM,HIGH,CRITICAL}"""

LAYER1_KAFKA_PRODUCE_TOTAL = _c(
    "kafka_produce_total",
    "Total Kafka messages produced to ccdt.ebpf.events",
    ["node", "status"],
)
"""Labels: status ∈ {success,error}"""

LAYER1_KAFKA_PRODUCE_LATENCY = _h(
    "kafka_produce_duration_seconds",
    "Latency of Kafka produce calls from Layer-1 collector",
    ["node"],
    buckets=(0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
)

LAYER1_RING_BUFFER_DROP = _c(
    "ring_buffer_drops_total",
    "eBPF ring buffer drop events — buffer too small for event rate",
    ["node", "probe"],
)

LAYER1_BATCH_SIZE = _h(
    "batch_size_events",
    "Number of events per Kafka batch from Layer-1 collector",
    ["node"],
    buckets=(1, 5, 10, 25, 50, 100, 200, 500),
)

LAYER1_OOM_KILLS = _c(
    "oom_kills_total",
    "Total OOM kill events observed by Layer-1 collector",
    ["node", "namespace", "pod"],
)

LAYER1_CAPABILITY_CHECKS = _c(
    "capability_checks_total",
    "Total Linux capability checks observed",
    ["node", "capability", "allowed"],
)

LAYER1_SENSITIVE_FILE_ACCESSES = _c(
    "sensitive_file_accesses_total",
    "Total accesses to sensitive files (e.g. /etc/passwd, /proc/keys)",
    ["node", "filepath", "access_type"],
)

LAYER1_COLLECTOR_UP = _g(
    "collector_up",
    "Whether the Layer-1 eBPF collector is running on this node (1=up, 0=down)",
    ["node"],
)

LAYER1_SCHED_LATENCY_US = _h(
    "sched_latency_microseconds",
    "Scheduler wake-up latency in microseconds from Layer-1 eBPF tracepoint",
    ["node", "cpu"],
    buckets=(10, 50, 100, 500, 1000, 5000, 10000, 50000, 100000, 500000),
)

# ══════════════════════════════════════════════════════════════════════════════
# Layer-2 — Causal GNN Cognitive Core
# ══════════════════════════════════════════════════════════════════════════════

LAYER2_GNN_INFERENCES = _c(
    "gnn_inferences_total",
    "Total GNN inference runs completed by Layer-2",
    ["incident_type", "is_heartbeat"],
)

LAYER2_GNN_LATENCY = _h(
    "gnn_inference_duration_seconds",
    "End-to-end latency of one GNN inference run (graph build + forward pass + publish)",
    ["node_count_bucket"],
    buckets=INFERENCE_BUCKETS,
)
"""node_count_bucket: ∈ {0-5, 6-10, 11-20, 21-50, 50+}"""

LAYER2_GNN_CONFIDENCE = _h(
    "gnn_confidence",
    "Graph-level GNN classification confidence distribution",
    ["incident_type"],
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

LAYER2_ROOT_CAUSE_CONFIDENCE = _h(
    "root_cause_confidence",
    "Root cause identification confidence from GNN attention mechanism",
    [],
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

LAYER2_BLAST_RADIUS = _h(
    "blast_radius_nodes",
    "Number of nodes in the blast radius per incident",
    ["incident_type"],
    buckets=(0, 1, 2, 3, 5, 10, 20, 50),
)

LAYER2_GRAPH_NODE_COUNT = _h(
    "graph_node_count",
    "Number of topology nodes in the graph at inference time",
    [],
    buckets=(1, 5, 10, 20, 30, 40, 50, 75, 100),
)

LAYER2_KAFKA_CONSUMER_LAG = _g(
    "kafka_consumer_lag_events",
    "Number of unprocessed eBPF events in the ccdt.ebpf.events Kafka topic",
    ["partition"],
)

LAYER2_NODE_CLASSIFICATIONS = _c(
    "node_classifications_total",
    "Total node classifications by class",
    ["node_class"],
)
"""node_class ∈ {HEALTHY, FAULT, ATTACK}"""

LAYER2_INFERENCE_QUEUE_DEPTH = _g(
    "gnn_inference_queue_depth",
    "Current depth of the GNN inference request queue (for HPA custom metric)",
    [],
)

LAYER2_MODEL_LOADED = _g(
    "gnn_model_loaded",
    "Whether the GNN model is loaded and ready for inference (1=ready, 0=loading)",
    ["model_version"],
)

# ══════════════════════════════════════════════════════════════════════════════
# Layer-3 — Guardian (RL + OPA)
# ══════════════════════════════════════════════════════════════════════════════

LAYER3_ACTIONS = _c(
    "guardian_actions_total",
    "Total remediation actions attempted by Layer-3 Guardian",
    ["action_name", "status", "autonomy_mode"],
)
"""status ∈ {SUCCEEDED,FAILED,DENIED,TIMEOUT,ROLLED_BACK,AWAITING_APPROVAL}"""

LAYER3_ACTION_LATENCY = _h(
    "guardian_action_duration_seconds",
    "Time from action request to Kubernetes API confirmation",
    ["action_name"],
    buckets=K8S_ACTION_BUCKETS,
)

LAYER3_OPA_DECISIONS = _c(
    "guardian_opa_decisions_total",
    "Total OPA policy gate decisions",
    ["policy", "decision"],
)
"""policy ∈ {no_privilege_escalation,cpu_threshold,egress_control,
             lateral_movement,oom_notification}
   decision ∈ {allow,deny}"""

LAYER3_OPA_DENIALS = _c(
    "guardian_opa_denials_total",
    "Total OPA policy denials (subset of decisions where decision=deny)",
    ["policy", "action_name"],
)

LAYER3_OPA_LATENCY = _h(
    "guardian_opa_eval_duration_seconds",
    "Time for OPA policy evaluation per decision",
    ["policy"],
    buckets=OPA_BUCKETS,
)

LAYER3_GHOST_LATENCY = _h(
    "guardian_ghost_preview_duration_seconds",
    "Latency of Ghost dry-run simulation (Kubernetes dry-run + risk scoring)",
    ["action_name"],
    buckets=K8S_ACTION_BUCKETS,
)

LAYER3_GHOST_RISK_SCORES = _h(
    "guardian_ghost_risk_score",
    "Distribution of Ghost simulation risk scores",
    ["action_name"],
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

LAYER3_RL_Q_VALUES = _h(
    "guardian_rl_q_value",
    "RL policy Q-values for selected actions",
    ["action_name"],
    buckets=(-5.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 5.0, 10.0),
)

LAYER3_APPROVAL_LATENCY = _h(
    "guardian_approval_wait_seconds",
    "Time waiting for human approval in supervised/human-in-loop mode",
    ["action_name", "outcome"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)

LAYER3_ROLLBACKS = _c(
    "guardian_rollbacks_total",
    "Total automatic rollbacks triggered after failed actions",
    ["action_name", "reason"],
)

LAYER3_GUARDIAN_MODE = _g(
    "guardian_autonomy_mode",
    "Current Guardian autonomy mode (gauge per mode, 1=active 0=inactive)",
    ["mode"],
)
"""mode ∈ {supervised, human-in-loop, full-auto}"""

LAYER3_KAFKA_ACTIONS_PUBLISHED = _c(
    "guardian_actions_published_total",
    "Total action results published to ccdt.guardian.actions Kafka topic",
    ["status"],
)

# ══════════════════════════════════════════════════════════════════════════════
# Layer-4 — Co-Pilot (Claude AI)
# ══════════════════════════════════════════════════════════════════════════════

LAYER4_CHAT_REQUESTS = _c(
    "copilot_chat_requests_total",
    "Total chat requests received by Layer-4 Co-Pilot",
    ["request_type", "status"],
)
"""request_type ∈ {user_message,auto_incident,tool_call}"""

LAYER4_CHAT_LATENCY = _h(
    "copilot_chat_duration_seconds",
    "End-to-end latency of a Co-Pilot chat response (including all tool-use rounds)",
    ["message_type"],
    buckets=LLM_BUCKETS,
)

LAYER4_TOKENS_IN = _c(
    "copilot_tokens_input_total",
    "Total input tokens sent to the Claude API by Layer-4",
    ["model"],
)

LAYER4_TOKENS_OUT = _c(
    "copilot_tokens_output_total",
    "Total output tokens received from the Claude API",
    ["model"],
)

LAYER4_TOKENS_CACHE_READ = _c(
    "copilot_tokens_cache_read_total",
    "Total prompt cache read tokens (reduces API cost)",
    ["model"],
)

LAYER4_TOKENS_CACHE_WRITE = _c(
    "copilot_tokens_cache_write_total",
    "Total prompt cache write tokens",
    ["model"],
)

LAYER4_TOKEN_USAGE = _h(
    "copilot_tokens_per_request",
    "Distribution of total tokens (input+output) per Co-Pilot request",
    ["direction"],
    buckets=TOKEN_BUCKETS,
)

LAYER4_TOOL_CALLS = _c(
    "copilot_tool_calls_total",
    "Total tool calls made by Claude during Co-Pilot responses",
    ["tool_name", "status"],
)

LAYER4_TOOL_LATENCY = _h(
    "copilot_tool_duration_seconds",
    "Latency of each tool call execution within a Co-Pilot response",
    ["tool_name"],
    buckets=HTTP_BUCKETS,
)

LAYER4_TOOL_ROUNDS = _h(
    "copilot_tool_rounds_per_request",
    "Number of tool-use iteration rounds per Co-Pilot response",
    [],
    buckets=(0, 1, 2, 3, 4, 5, 6),
)

LAYER4_ACTIVE_SESSIONS = _g(
    "copilot_active_sessions",
    "Number of currently active Co-Pilot conversation sessions",
    [],
)

LAYER4_SESSION_TURNS = _h(
    "copilot_session_turns",
    "Number of turns in a completed Co-Pilot session",
    [],
    buckets=(1, 2, 3, 5, 8, 10, 15, 20),
)

LAYER4_ANTHROPIC_API_ERRORS = _c(
    "copilot_anthropic_api_errors_total",
    "Total errors from the Anthropic Claude API",
    ["error_type", "model"],
)
"""error_type ∈ {rate_limit, timeout, overload, auth, unknown}"""

LAYER4_INCIDENT_REPORTS_INJECTED = _c(
    "copilot_incident_reports_injected_total",
    "Total auto-generated incident reports injected into Co-Pilot sessions",
    ["incident_type", "severity"],
)

LAYER4_ESTIMATED_COST_USD = _c(
    "copilot_estimated_cost_usd_total",
    "Accumulated estimated USD cost of Claude API calls",
    ["model"],
)

# ══════════════════════════════════════════════════════════════════════════════
# API Gateway
# ══════════════════════════════════════════════════════════════════════════════

GW_REQUESTS = _c(
    "gateway_requests_total",
    "Total HTTP requests handled by the API Gateway",
    ["method", "path", "status"],
)

GW_LATENCY = _h(
    "gateway_request_duration_seconds",
    "API Gateway request latency",
    ["method", "path"],
    buckets=HTTP_BUCKETS,
)

GW_AUTH_FAILURES = _c(
    "gateway_auth_failures_total",
    "Total authentication failures at the API Gateway",
    ["reason"],
)

GW_RATE_LIMITED = _c(
    "gateway_rate_limited_total",
    "Total requests rejected by the API Gateway rate limiter",
    ["subject"],
)

GW_WS_CONNECTIONS = _g(
    "gateway_ws_connections",
    "Number of active WebSocket connections to the API Gateway",
    [],
)

# ══════════════════════════════════════════════════════════════════════════════
# Platform-wide / cross-layer
# ══════════════════════════════════════════════════════════════════════════════

CCDT_INCIDENTS = _c(
    "incidents_total",
    "Total incidents detected by the CCDT platform",
    ["incident_type", "severity", "resolved"],
)

CCDT_INCIDENT_DURATION = _h(
    "incident_duration_seconds",
    "Duration from detection to resolution for each incident",
    ["incident_type", "severity"],
    buckets=(10, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200),
)

CCDT_INCIDENT_MTTR = _g(
    "incident_mean_time_to_resolve_seconds",
    "Rolling mean time to resolve over the last 24 hours",
    ["incident_type"],
)

CCDT_FALSE_POSITIVES = _c(
    "false_positives_total",
    "Total incidents marked as false positive by operators",
    ["incident_type"],
)

CCDT_KAFKA_TOPIC_LAG = _g(
    "kafka_topic_lag_messages",
    "Consumer group lag for each CCDT Kafka topic",
    ["topic", "consumer_group", "partition"],
)

CCDT_PIPELINE_E2E_LATENCY = _h(
    "pipeline_e2e_latency_seconds",
    "End-to-end latency from eBPF event capture to Guardian action execution",
    ["incident_type"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)


# ══════════════════════════════════════════════════════════════════════════════
# Helper utilities
# ══════════════════════════════════════════════════════════════════════════════

def set_build_info(
    version: str = "",
    git_commit: str = "",
    build_date: str = "",
    go_version: str = "",
    python_version: str = "",
) -> None:
    """
    Set the ccdt_build_info metric (shown in Grafana dashboards).

        from ccdt.shared.utils.metrics import set_build_info
        set_build_info(version="1.0.0", git_commit="abc123", build_date="2025-01-15")
    """
    import platform
    CCDT_BUILD_INFO.info({
        "version":        version or os.environ.get("VERSION", "unknown"),
        "git_commit":     git_commit or os.environ.get("GIT_COMMIT", "unknown"),
        "build_date":     build_date or os.environ.get("BUILD_DATE", "unknown"),
        "python_version": python_version or platform.python_version(),
        "service":        os.environ.get("SERVICE_NAME", "ccdt"),
        "layer":          os.environ.get("LAYER", "unknown"),
    })


def node_count_bucket(n: int) -> str:
    """Map a node count to a label-safe bucket string for LAYER2_GNN_LATENCY."""
    if n <= 5:   return "0-5"
    if n <= 10:  return "6-10"
    if n <= 20:  return "11-20"
    if n <= 50:  return "21-50"
    return "50+"


class LatencyTimer:
    """
    Context manager + decorator for timing code blocks and recording
    them to a Histogram.

        with LatencyTimer(LAYER2_GNN_LATENCY, labels={"node_count_bucket": "11-20"}):
            result = model.infer(graph)
    """

    def __init__(self, histogram: Histogram, labels: dict[str, str] | None = None) -> None:
        self._histogram = histogram
        self._labels = labels or {}
        self._start: float = 0.0

    def __enter__(self) -> "LatencyTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_) -> None:
        elapsed = time.perf_counter() - self._start
        if self._labels:
            self._histogram.labels(**self._labels).observe(elapsed)
        else:
            self._histogram.observe(elapsed)

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000


def start_metrics_server(
    port: int = 0,
    addr: str = "0.0.0.0",
    use_default_registry: bool = False,
) -> None:
    """
    Start the Prometheus metrics HTTP server on the given port.

    Runs in a daemon thread — will not block the main application.
    The /metrics endpoint is served at http://addr:port/metrics.

        from ccdt.shared.utils.metrics import start_metrics_server
        start_metrics_server(port=9090)

    Parameters
    ----------
    port                : Port to listen on. Falls back to METRICS_PORT env var,
                          then 9090.
    addr                : Bind address.
    use_default_registry: If True, also exposes default Python metrics
                          (GC, process memory etc.) in addition to CCDT metrics.
    """
    resolved_port = port or int(os.environ.get("METRICS_PORT", 9090))
    reg = DEFAULT_REGISTRY if use_default_registry else registry
    start_http_server(resolved_port, addr=addr, registry=reg)


def make_metrics_asgi_app(use_default_registry: bool = False):
    """
    Return a minimal ASGI app that serves /metrics.
    Mount alongside a FastAPI app:

        from starlette.routing import Mount
        from ccdt.shared.utils.metrics import make_metrics_asgi_app
        app.mount("/metrics", make_metrics_asgi_app())
    """
    reg = DEFAULT_REGISTRY if use_default_registry else registry

    async def metrics_app(scope, receive, send):
        if scope["type"] != "http":
            return
        output  = generate_latest(reg)
        headers = [
            (b"content-type",   CONTENT_TYPE_LATEST.encode()),
            (b"content-length", str(len(output)).encode()),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": output})

    return metrics_app
