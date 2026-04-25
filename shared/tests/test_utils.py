"""Tests for CCDT shared utils — logging and metrics."""
from __future__ import annotations

import json
import logging
import os
import io
import time

import pytest

from ccdt.shared.utils.logging import (
    get_logger, configure_logging, CCDTLogger,
    _correlation_id, _request_id, _incident_id,
    JSONFormatter, PrettyFormatter, _redact_value,
)
from ccdt.shared.utils.metrics import (
    registry, LatencyTimer, node_count_bucket, set_build_info,
    LAYER1_EBPF_EVENTS, LAYER2_GNN_INFERENCES, LAYER2_GNN_LATENCY,
    LAYER3_ACTIONS, LAYER4_CHAT_REQUESTS, LAYER4_TOKENS_IN,
    CCDT_INCIDENTS, GW_REQUESTS,
)


# ── Logging tests ──────────────────────────────────────────────────────────────

class TestJSONFormatter:
    def _format(self, level, msg, **kwargs) -> dict:
        formatter = JSONFormatter()
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(formatter)
        logger = logging.getLogger(f"test.json.{id(self)}")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        record = logger.makeRecord(
            "test", level, "test_file.py", 10, msg, (), None
        )
        for k, v in kwargs.items():
            setattr(record, k, v)
        line = formatter.format(record)
        return json.loads(line)

    def test_required_fields_present(self):
        d = self._format(logging.INFO, "test message")
        assert "ts" in d
        assert "level" in d
        assert "msg" in d
        assert "service" in d

    def test_level_info(self):
        d = self._format(logging.INFO, "info message")
        assert d["level"] == "INFO"

    def test_level_error(self):
        d = self._format(logging.ERROR, "error message")
        assert d["level"] == "ERROR"

    def test_extra_fields(self):
        d = self._format(logging.INFO, "with extras", latency_ms=42.1, node="my-node")
        assert d.get("latency_ms") == 42.1
        assert d.get("node") == "my-node"

    def test_correlation_id_injected(self):
        _correlation_id.set("corr-test-123")
        d = self._format(logging.INFO, "correlated")
        assert d.get("correlation_id") == "corr-test-123"
        _correlation_id.set("")  # cleanup


class TestRedactValue:
    def test_redacts_api_key(self):
        d = {"api_key": "sk-ant-super-secret"}
        result = _redact_value(d)
        assert result["api_key"] == "***REDACTED***"

    def test_redacts_password(self):
        d = {"password": "hunter2", "username": "alice"}
        result = _redact_value(d)
        assert result["password"] == "***REDACTED***"
        assert result["username"] == "alice"   # not redacted

    def test_redacts_nested(self):
        d = {"db": {"password": "secret", "host": "localhost"}}
        result = _redact_value(d)
        assert result["db"]["password"] == "***REDACTED***"
        assert result["db"]["host"] == "localhost"

    def test_passthrough_non_sensitive(self):
        d = {"node_count": 17, "latency_ms": 38.5}
        result = _redact_value(d)
        assert result == d

    def test_redacts_anthropic_key_in_string(self):
        s = "ANTHROPIC_API_KEY=sk-ant-abcdefg12345"
        result = _redact_value(s)
        assert "sk-ant-" not in result


class TestCCDTLogger:
    def setup_method(self):
        configure_logging(level="DEBUG", service="test-service", json_format=False)
        self.log = get_logger("test.ccdt_logger")

    def test_get_logger_returns_ccdt_logger(self):
        assert isinstance(get_logger("some.module"), CCDTLogger)

    def test_get_logger_cached(self):
        l1 = get_logger("cached.test")
        l2 = get_logger("cached.test")
        assert l1 is l2

    def test_info_does_not_raise(self):
        self.log.info("test message", key="value", count=42)

    def test_error_does_not_raise(self):
        self.log.error("error occurred", code=500)

    def test_warning_does_not_raise(self):
        self.log.warning("something is off", metric=0.95)

    def test_is_enabled_for(self):
        assert self.log.isEnabledFor(logging.DEBUG) is True

    def test_new_correlation_id(self):
        cid = CCDTLogger.new_correlation_id()
        assert len(cid) == 36  # UUID4 format
        assert _correlation_id.get() == cid

    def test_set_incident_id(self):
        CCDTLogger.set_incident_id("inc-001")
        assert _incident_id.get() == "inc-001"

    def test_bind_context_manager(self):
        # Should not raise and should reset after context
        with self.log.bind(request_id="req-xyz", user="alice"):
            self.log.info("inside bind")
        self.log.info("outside bind")  # should work without those fields

    def test_timer_logs_latency(self):
        with self.log.timer("test_operation", extra_field="abc"):
            time.sleep(0.001)

    def test_timer_logs_on_error(self):
        with pytest.raises(ValueError):
            with self.log.timer("failing_operation"):
                raise ValueError("oops")

    def test_audit_log(self):
        self.log.audit(
            "guardian_action",
            audit_id="a1b2",
            actor="rl-policy",
            target="payment-svc",
            outcome="SUCCEEDED",
            action="restart_pod",
        )


# ── Metrics tests ─────────────────────────────────────────────────────────────

class TestMetrics:
    def test_layer1_counter_increments(self):
        before = LAYER1_EBPF_EVENTS.labels(
            event_type="oom_kill", node="node-1", severity="HIGH"
        )._value.get()
        LAYER1_EBPF_EVENTS.labels(
            event_type="oom_kill", node="node-1", severity="HIGH"
        ).inc()
        after = LAYER1_EBPF_EVENTS.labels(
            event_type="oom_kill", node="node-1", severity="HIGH"
        )._value.get()
        assert after == before + 1

    def test_layer2_inference_counter(self):
        LAYER2_GNN_INFERENCES.labels(
            incident_type="FAULT", is_heartbeat="false"
        ).inc()

    def test_layer2_latency_histogram(self):
        LAYER2_GNN_LATENCY.labels(node_count_bucket="11-20").observe(0.038)

    def test_layer3_actions_counter(self):
        LAYER3_ACTIONS.labels(
            action_name="restart_pod",
            status="SUCCEEDED",
            autonomy_mode="supervised",
        ).inc()

    def test_layer4_chat_requests(self):
        LAYER4_CHAT_REQUESTS.labels(
            request_type="user_message", status="success"
        ).inc()

    def test_layer4_tokens_counter(self):
        LAYER4_TOKENS_IN.labels(model="claude-sonnet-4-20250514").inc(1000)

    def test_gateway_requests(self):
        GW_REQUESTS.labels(method="POST", path="/api/v1/chat", status="200").inc()

    def test_ccdt_incidents(self):
        CCDT_INCIDENTS.labels(
            incident_type="FAULT", severity="HIGH", resolved="true"
        ).inc()

    def test_registry_is_custom(self):
        # CCDT metrics should use our custom registry, not the default
        from prometheus_client import REGISTRY as DEFAULT
        assert registry is not DEFAULT


class TestLatencyTimer:
    def test_records_histogram(self):
        with LatencyTimer(LAYER2_GNN_LATENCY, labels={"node_count_bucket": "0-5"}):
            time.sleep(0.001)

    def test_elapsed_ms_positive(self):
        timer = LatencyTimer(LAYER2_GNN_LATENCY)
        timer._start = time.perf_counter()
        time.sleep(0.001)
        assert timer.elapsed_ms > 0

    def test_works_without_labels(self):
        from ccdt.shared.utils.metrics import LAYER2_GRAPH_NODE_COUNT
        with LatencyTimer(LAYER2_GRAPH_NODE_COUNT):
            pass


class TestNodeCountBucket:
    @pytest.mark.parametrize("n,expected", [
        (0, "0-5"), (5, "0-5"),
        (6, "6-10"), (10, "6-10"),
        (11, "11-20"), (20, "11-20"),
        (21, "21-50"), (50, "21-50"),
        (51, "50+"), (1000, "50+"),
    ])
    def test_bucketing(self, n, expected):
        assert node_count_bucket(n) == expected


class TestSetBuildInfo:
    def test_does_not_raise(self):
        set_build_info(
            version="1.0.0",
            git_commit="abc123",
            build_date="2025-01-15",
        )


class TestMakeMetricsAsgiApp:
    def test_returns_callable(self):
        from ccdt.shared.utils.metrics import make_metrics_asgi_app
        app = make_metrics_asgi_app()
        assert callable(app)
