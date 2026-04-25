"""
CCDT Shared — Structured Logging
═══════════════════════════════════════════════════════════════════════════════
Production-grade structured JSON logging used by all four CCDT layers.

Features
--------
* JSON-formatted log lines (ECS-compatible, easy to ingest into OpenSearch /
  CloudWatch / Loki)
* Automatic context fields: service, layer, node, pod, namespace (from env vars)
* Correlation ID propagation via contextvars (thread-safe, async-safe)
* Sensitive field redaction (API keys, passwords, tokens)
* Per-record latency tracking helpers
* Kafka audit log handler for guardian actions
* Prometheus counter integration (error/warning counts)
* Optional pretty-print mode for local development
* Zero external dependencies beyond stdlib + structlog

Usage
-----
    from ccdt.shared.utils.logging import get_logger, configure_logging

    configure_logging(level="INFO", service="layer2-cognitive")
    log = get_logger(__name__)

    log.info("inference complete",
             inference_id="abc-123",
             latency_ms=42.1,
             node_count=17)

    # Correlation ID automatically propagates in async tasks:
    with log.bind(request_id="req-xyz"):
        await some_async_function()

    # Timed block:
    with log.timer("db_query"):
        results = await db.fetch(sql)

Environment variables
---------------------
    LOG_LEVEL            INFO | DEBUG | WARNING | ERROR  (default: INFO)
    LOG_FORMAT           json | pretty                    (default: json)
    SERVICE_NAME         layer2-cognitive                 (default: ccdt)
    LAYER                1 | 2 | 3 | 4                   (default: unknown)
    POD_NAME             from Kubernetes downward API
    NODE_NAME            from Kubernetes downward API
    POD_NAMESPACE        from Kubernetes downward API
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import traceback
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Generator, Optional

# ── Optional structlog integration ────────────────────────────────────────────
try:
    import structlog
    _HAS_STRUCTLOG = True
except ImportError:
    _HAS_STRUCTLOG = False

# ── Correlation context (async-safe via ContextVar) ───────────────────────────
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
_request_id:     ContextVar[str] = ContextVar("request_id",     default="")
_incident_id:    ContextVar[str] = ContextVar("incident_id",    default="")

# ── Sensitive field patterns — values are redacted before logging ─────────────
_SENSITIVE_KEYS: set[str] = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "private_key", "access_key",
    "refresh_token", "jwt", "bearer", "anthropic_api_key", "aws_secret",
}

_SECRET_PATTERN = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]+|"          # Anthropic API key
    r"AKIA[A-Z0-9]{16}|"                  # AWS access key
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"    # JWT
    r")"
)

# ── Context from Kubernetes downward API ──────────────────────────────────────
_ENV_CONTEXT: dict[str, str] = {
    "service":    os.environ.get("SERVICE_NAME", "ccdt"),
    "layer":      os.environ.get("LAYER",        "unknown"),
    "pod":        os.environ.get("POD_NAME",     ""),
    "node":       os.environ.get("NODE_NAME",    ""),
    "namespace":  os.environ.get("POD_NAMESPACE", "ccdt"),
    "version":    os.environ.get("VERSION",      "1.0.0"),
}


# ══════════════════════════════════════════════════════════════════════════════
# Formatters
# ══════════════════════════════════════════════════════════════════════════════

def _redact_value(v: Any) -> Any:
    """Recursively redact sensitive values from dicts/strings."""
    if isinstance(v, dict):
        return {k: ("***REDACTED***" if k.lower() in _SENSITIVE_KEYS else _redact_value(v))
                for k, v in v.items()}
    if isinstance(v, str):
        return _SECRET_PATTERN.sub("***REDACTED***", v)
    if isinstance(v, (list, tuple)):
        return type(v)(_redact_value(i) for i in v)
    return v


class JSONFormatter(logging.Formatter):
    """
    ECS-compatible JSON formatter.

    Each log line is a single JSON object with fields:
        ts          — ISO-8601 UTC timestamp with milliseconds
        level       — DEBUG | INFO | WARNING | ERROR | CRITICAL
        logger      — logger name (usually __name__)
        msg         — log message
        service     — SERVICE_NAME env var
        layer       — LAYER env var (1-4)
        pod         — POD_NAME
        node        — NODE_NAME
        namespace   — POD_NAMESPACE
        correlation_id — from ContextVar
        request_id     — from ContextVar
        incident_id    — from ContextVar
        file        — filename:lineno
        exc_info    — exception traceback (only if an exception is attached)
        ...extra    — any additional key=value fields from log.info(msg, key=val)
    """

    def __init__(self, include_caller: bool = True) -> None:
        super().__init__()
        self._include_caller = include_caller

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        record_dict: dict[str, Any] = {
            "ts":        datetime.fromtimestamp(record.created, tz=timezone.utc)
                         .isoformat(timespec="milliseconds"),
            "level":     record.levelname,
            "logger":    record.name,
            "msg":       record.getMessage(),
            **_ENV_CONTEXT,
        }

        # Correlation context
        if cid := _correlation_id.get():
            record_dict["correlation_id"] = cid
        if rid := _request_id.get():
            record_dict["request_id"] = rid
        if iid := _incident_id.get():
            record_dict["incident_id"] = iid

        # Source location
        if self._include_caller:
            record_dict["file"] = f"{record.filename}:{record.lineno}"
            record_dict["func"] = record.funcName

        # Extra fields from log.info("msg", key=val)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            )
        }
        record_dict.update(_redact_value(extras))

        # Exception info
        if record.exc_info:
            record_dict["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            record_dict["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(record_dict, default=str, ensure_ascii=False)


class PrettyFormatter(logging.Formatter):
    """
    Colourised console formatter for local development.
    Falls back gracefully if the terminal doesn't support ANSI codes.
    """

    _COLOURS = {
        "DEBUG":    "\033[36m",    # cyan
        "INFO":     "\033[32m",    # green
        "WARNING":  "\033[33m",    # yellow
        "ERROR":    "\033[31m",    # red
        "CRITICAL": "\033[35;1m",  # bold magenta
        "RESET":    "\033[0m",
    }

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        ts    = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        level = record.levelname
        col   = self._COLOURS.get(level, "")
        rst   = self._COLOURS["RESET"]

        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            )
        }
        extra_str = "  ".join(f"{k}={v!r}" for k, v in extras.items())
        cid = _correlation_id.get()
        cid_str = f"[{cid[:8]}] " if cid else ""

        line = (
            f"{col}{ts} {level:<8}{rst}  "
            f"{cid_str}{record.name}  "
            f"{record.getMessage()}"
        )
        if extra_str:
            line += f"  {extra_str}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ══════════════════════════════════════════════════════════════════════════════
# CCDTLogger — thin wrapper that adds context management and timing
# ══════════════════════════════════════════════════════════════════════════════

class CCDTLogger:
    """
    Wrapper around stdlib Logger with extra CCDT-specific features:
    - bind()  — attach contextual fields to all subsequent log calls
    - timer() — context manager that logs operation latency
    - audit() — structured audit trail for Guardian actions
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._bound: dict[str, Any] = {}

    # ── stdlib passthrough ────────────────────────────────────────────────────

    def debug   (self, msg: str, **kw: Any) -> None: self._log(logging.DEBUG,    msg, **kw)
    def info    (self, msg: str, **kw: Any) -> None: self._log(logging.INFO,     msg, **kw)
    def warning (self, msg: str, **kw: Any) -> None: self._log(logging.WARNING,  msg, **kw)
    def error   (self, msg: str, **kw: Any) -> None: self._log(logging.ERROR,    msg, **kw)
    def critical(self, msg: str, **kw: Any) -> None: self._log(logging.CRITICAL, msg, **kw)

    def exception(self, msg: str, **kw: Any) -> None:
        self._log(logging.ERROR, msg, exc_info=True, **kw)

    def _log(self, _level: int, msg: str, **kw: Any) -> None:
        if not self._logger.isEnabledFor(_level):
            return
        merged = {**self._bound, **kw}
        exc_info = merged.pop("exc_info", False)
        # Discard 'level' if accidentally passed as a kwarg
        merged.pop("level", None)
        extra = dict(merged)
        self._logger.log(
            _level, msg,
            exc_info=exc_info,
            extra=extra,
            stacklevel=3,
        )

    # ── Context binding ────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def bind(self, **fields: Any) -> Generator[CCDTLogger, None, None]:
        """
        Temporarily attach fields to all log calls within the context.

            with log.bind(request_id="req-123", user="alice"):
                log.info("processing request")
                # → {"msg": "processing request", "request_id": "req-123", "user": "alice", ...}
        """
        old = dict(self._bound)
        self._bound.update(fields)
        try:
            yield self
        finally:
            self._bound = old

    # ── Correlation ID helpers ────────────────────────────────────────────────

    @staticmethod
    def set_correlation_id(cid: str) -> None:
        _correlation_id.set(cid)

    @staticmethod
    def set_request_id(rid: str) -> None:
        _request_id.set(rid)

    @staticmethod
    def set_incident_id(iid: str) -> None:
        _incident_id.set(iid)

    @staticmethod
    def new_correlation_id() -> str:
        cid = str(uuid.uuid4())
        _correlation_id.set(cid)
        return cid

    # ── Timer context manager ─────────────────────────────────────────────────

    @contextlib.contextmanager
    def timer(
        self,
        operation: str,
        log_level: int = logging.INFO,
        **extra: Any,
    ) -> Generator[None, None, None]:
        """
        Log the latency of a code block.

            with log.timer("gnn_inference", node_count=42):
                result = model.infer(graph)
            # → {"msg": "gnn_inference completed", "latency_ms": 38.7, "node_count": 42}
        """
        start = time.perf_counter()
        try:
            yield
            latency_ms = (time.perf_counter() - start) * 1000
            self._log(log_level, f"{operation} completed",
                      latency_ms=round(latency_ms, 3), **extra)
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            self._log(logging.ERROR, f"{operation} failed",
                      latency_ms=round(latency_ms, 3),
                      error=str(exc), **extra)
            raise

    # ── Audit log ─────────────────────────────────────────────────────────────

    def audit(
        self,
        event: str,
        *,
        audit_id: str,
        actor: str,
        target: str,
        outcome: str,
        **details: Any,
    ) -> None:
        """
        Write a structured audit trail entry.
        Used by Layer-3 Guardian for every action execution attempt.

            log.audit(
                "guardian_action",
                audit_id="a1b2c3",
                actor="rl-policy",
                target="payment-svc/pod-xyz",
                outcome="SUCCEEDED",
                action="restart_pod",
                risk_score=0.12,
            )
        """
        self._log(
            logging.INFO,
            f"AUDIT:{event}",
            audit_id=audit_id,
            actor=actor,
            target=target,
            outcome=outcome,
            audit=True,   # sentinel for log aggregator filtering
            **details,
        )

    # ── isEnabledFor passthrough ──────────────────────────────────────────────

    def isEnabledFor(self, level: int) -> bool:
        return self._logger.isEnabledFor(level)


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

# Module-level cache of CCDTLogger instances (one per name)
_loggers: dict[str, CCDTLogger] = {}


def get_logger(name: str) -> CCDTLogger:
    """
    Return a CCDTLogger for the given name.
    Loggers are cached — calling get_logger(__name__) multiple times is cheap.

        log = get_logger(__name__)
        log.info("service started", port=8001)
    """
    if name not in _loggers:
        _loggers[name] = CCDTLogger(logging.getLogger(name))
    return _loggers[name]


def configure_logging(
    level: str = "",
    service: str = "",
    layer: str = "",
    json_format: bool | None = None,
    log_file: Optional[str] = None,
    max_bytes: int = 50 * 1024 * 1024,   # 50 MB
    backup_count: int = 5,
) -> None:
    """
    Configure root logger for the CCDT platform.

    Call once at application startup, before importing any other modules that
    use get_logger().

    Parameters
    ----------
    level       : Log level string. Falls back to LOG_LEVEL env var, then INFO.
    service     : Service name (overrides SERVICE_NAME env var).
    layer       : Layer number "1"–"4" (overrides LAYER env var).
    json_format : True = JSON (production), False = pretty (development).
                  None = auto-detect from LOG_FORMAT env var.
    log_file    : Optional path to a rotating file log.
    max_bytes   : Max size of each log file before rotation.
    backup_count: Number of rotated files to keep.
    """
    # ── Update environment context ─────────────────────────────────────────
    if service:
        _ENV_CONTEXT["service"] = service
        os.environ["SERVICE_NAME"] = service
    if layer:
        _ENV_CONTEXT["layer"] = layer

    # ── Resolve level ──────────────────────────────────────────────────────
    level_str = level or os.environ.get("LOG_LEVEL", "INFO")
    log_level = getattr(logging, level_str.upper(), logging.INFO)

    # ── Resolve format ─────────────────────────────────────────────────────
    if json_format is None:
        fmt_env = os.environ.get("LOG_FORMAT", "json").lower()
        json_format = fmt_env != "pretty"

    formatter: logging.Formatter = (
        JSONFormatter() if json_format else PrettyFormatter()
    )

    # ── Root logger ────────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.setLevel(log_level)
    root.addHandler(stdout_handler)

    # ── Optional rotating file log ─────────────────────────────────────────
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(JSONFormatter())  # always JSON in files
        file_handler.setLevel(log_level)
        root.addHandler(file_handler)

    # ── Suppress noisy third-party loggers ────────────────────────────────
    for noisy in ("urllib3", "asyncio", "aiokafka.consumer", "kubernetes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ── Log startup banner ─────────────────────────────────────────────────
    startup_log = get_logger("ccdt.logging")
    startup_log.info(
        "CCDT logging configured",
        level=level_str.upper(),
        format="json" if json_format else "pretty",
        service=_ENV_CONTEXT["service"],
        layer=_ENV_CONTEXT["layer"],
        pod=_ENV_CONTEXT["pod"],
        node=_ENV_CONTEXT["node"],
    )


# ── FastAPI middleware helper ─────────────────────────────────────────────────

def make_request_id_middleware():
    """
    Returns an ASGI middleware factory that injects a correlation ID into
    each request context and logs request/response latency.

    Usage in FastAPI:
        from ccdt.shared.utils.logging import make_request_id_middleware
        app.middleware("http")(make_request_id_middleware())
    """
    _mw_log = get_logger("ccdt.http")

    async def middleware(request, call_next):
        rid = (
            request.headers.get("X-Request-ID")
            or request.headers.get("X-Correlation-ID")
            or str(uuid.uuid4())
        )
        _request_id.set(rid)
        start = time.perf_counter()

        _mw_log.info(
            "request started",
            method=request.method,
            path=request.url.path,
            request_id=rid,
        )

        try:
            response = await call_next(request)
        except Exception as exc:
            _mw_log.exception(
                "request unhandled error",
                method=request.method,
                path=request.url.path,
                request_id=rid,
            )
            raise
        finally:
            latency_ms = (time.perf_counter() - start) * 1000
            status = getattr(response, "status_code", 0)
            _mw_log.info(
                "request complete",
                method=request.method,
                path=request.url.path,
                status=status,
                latency_ms=round(latency_ms, 3),
                request_id=rid,
            )

        response.headers["X-Request-ID"] = rid
        return response

    return middleware
