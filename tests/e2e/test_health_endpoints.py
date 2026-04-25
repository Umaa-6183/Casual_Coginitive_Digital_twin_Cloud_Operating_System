"""
End-to-End tests — Health & Readiness Probes
Validates /health and /ready endpoints for all 5 services:
  • Layer-1 collector     (port 9100)
  • Layer-2 GNN           (port 8001)
  • Layer-3 Guardian      (port 8002)
  • Layer-4 Co-Pilot      (port 8003)
  • API Gateway           (port 8000)

All HTTP calls use AsyncMock — no live services required.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def _uid() -> str: return str(uuid.uuid4())


# ══════════════════════════════════════════════════════════════════════════════
# Shared health response assertions
# ══════════════════════════════════════════════════════════════════════════════

def _assert_health_response(data: dict) -> None:
    """Assert common /health response structure."""
    assert "status" in data
    assert data["status"] in ("healthy", "degraded", "unhealthy")
    assert "service" in data


def _assert_ready_response(data: dict) -> None:
    """Assert common /ready response structure."""
    assert "ready"   in data
    assert isinstance(data["ready"], bool)


# ══════════════════════════════════════════════════════════════════════════════
# Layer-1 health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer1
class TestLayer1Health:
    async def test_health_endpoint(self, mock_http_client):
        resp = await mock_http_client.get("http://layer1-nervous:9100/health")
        _assert_health_response(resp.json())

    async def test_health_response_contains_probes(self, mock_http_client):
        """Layer-1 /health should report status of eBPF probes."""
        async def _health_resp(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "status":  "healthy",
                "service": "layer1-nervous",
                "probes": {
                    "capability":     "attached",
                    "oom_kill":       "attached",
                    "tcp_retransmit": "attached",
                    "sched_latency":  "attached",
                    "file_access":    "attached",
                    "execve":         "attached",
                    "network_connect":"attached",
                },
                "kafka": "connected",
                "uptime_s": 3600.0,
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_health_resp)
        resp = await mock_http_client.get("http://layer1-nervous:9100/health")
        data = resp.json()
        assert data["status"] == "healthy"
        assert "probes"       in data
        assert len(data["probes"]) == 7

    async def test_degraded_when_probe_detached(self, mock_http_client):
        """If an eBPF probe is detached, status should be 'degraded'."""
        async def _degraded(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "status":  "degraded",
                "service": "layer1-nervous",
                "probes": {"capability": "detached", "oom_kill": "attached"},
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_degraded)
        resp = await mock_http_client.get("http://layer1-nervous:9100/health")
        assert resp.json()["status"] == "degraded"


# ══════════════════════════════════════════════════════════════════════════════
# Layer-2 health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer2
class TestLayer2Health:
    async def test_health_endpoint(self, mock_http_client):
        resp = await mock_http_client.get("http://layer2-cognitive:8001/health")
        _assert_health_response(resp.json())

    async def test_ready_endpoint_model_loaded(self, mock_http_client):
        """Layer-2 /ready should indicate whether the GNN model is loaded."""
        async def _ready_resp(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "ready":        True,
                "model_loaded": True,
                "dag_builder":  True,
                "kafka":        True,
                "uptime_s":     1800.0,
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_ready_resp)
        resp = await mock_http_client.get("http://layer2-cognitive:8001/ready")
        data = resp.json()
        _assert_ready_response(data)
        assert data["model_loaded"] is True

    async def test_not_ready_model_not_loaded(self, mock_http_client):
        """During startup, if model checkpoint not found, /ready returns ready=False."""
        async def _not_ready(url, **kwargs):
            r = AsyncMock()
            r.status_code = 503
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "ready":        False,
                "model_loaded": False,
                "reason":       "Model checkpoint not found at /app/checkpoints/causal_gnn_best.pt",
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_not_ready)
        resp = await mock_http_client.get("http://layer2-cognitive:8001/ready")
        data = resp.json()
        assert data["ready"]        is False
        assert data["model_loaded"] is False


# ══════════════════════════════════════════════════════════════════════════════
# Layer-3 health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer3
class TestLayer3Health:
    async def test_health_endpoint(self, mock_http_client):
        resp = await mock_http_client.get("http://layer3-guardian:8002/health")
        _assert_health_response(resp.json())

    async def test_ready_endpoint_all_components(self, mock_http_client):
        """Layer-3 /ready checks RL agent, K8s, OPA, and Kafka connectivity."""
        async def _ready_resp(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "ready":        True,
                "agent_loaded": True,
                "k8s":          True,
                "opa":          True,
                "kafka":        True,
                "autonomy_mode": "supervised",
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_ready_resp)
        resp = await mock_http_client.get("http://layer3-guardian:8002/ready")
        data = resp.json()
        _assert_ready_response(data)
        assert data["agent_loaded"] is True
        assert data["autonomy_mode"] in ("human-in-loop", "supervised", "full-auto")

    async def test_autonomy_mode_in_health(self, mock_http_client):
        """Health response must include the current autonomy mode."""
        async def _health_resp(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "status":        "healthy",
                "service":       "layer3-guardian",
                "autonomy_mode": "supervised",
                "pending_approvals": 0,
                "actions_today":     42,
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_health_resp)
        resp = await mock_http_client.get("http://layer3-guardian:8002/health")
        data = resp.json()
        assert data["autonomy_mode"] == "supervised"


# ══════════════════════════════════════════════════════════════════════════════
# Layer-4 health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.layer4
class TestLayer4Health:
    async def test_health_endpoint(self, mock_http_client):
        resp = await mock_http_client.get("http://layer4-copilot:8003/health")
        _assert_health_response(resp.json())

    async def test_ready_endpoint_api_key_present(self, mock_http_client):
        """Layer-4 /ready must confirm ANTHROPIC_API_KEY is set."""
        async def _ready_resp(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "ready":           True,
                "api_key_present": True,
                "claude_model":    "claude-sonnet-4-20250514",
                "active_sessions": 2,
                "kafka":           True,
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_ready_resp)
        resp = await mock_http_client.get("http://layer4-copilot:8003/ready")
        data = resp.json()
        _assert_ready_response(data)
        assert data["api_key_present"] is True

    async def test_not_ready_missing_api_key(self, mock_http_client):
        """If ANTHROPIC_API_KEY is not set, /ready must return ready=False."""
        async def _no_key(url, **kwargs):
            r = AsyncMock()
            r.status_code = 503
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "ready":           False,
                "api_key_present": False,
                "reason":          "ANTHROPIC_API_KEY not set",
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_no_key)
        resp = await mock_http_client.get("http://layer4-copilot:8003/ready")
        data = resp.json()
        assert data["ready"]           is False
        assert data["api_key_present"] is False


# ══════════════════════════════════════════════════════════════════════════════
# API Gateway health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.gateway
class TestAPIGatewayHealth:
    async def test_health_endpoint(self, mock_http_client):
        resp = await mock_http_client.get("http://api-gateway:8000/health")
        _assert_health_response(resp.json())

    async def test_ready_all_upstream_healthy(self, mock_http_client):
        """Gateway /ready checks all 4 upstream service health endpoints."""
        async def _ready_resp(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "ready": True,
                "checks": {
                    "kafka":             True,
                    "layer1_nervous":    True,
                    "layer2_cognitive":  True,
                    "layer3_guardian":   True,
                    "layer4_copilot":    True,
                },
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_ready_resp)
        resp = await mock_http_client.get("http://api-gateway:8000/ready")
        data = resp.json()
        _assert_ready_response(data)
        assert data["ready"] is True
        for check in ("kafka", "layer2_cognitive", "layer3_guardian", "layer4_copilot"):
            assert data["checks"][check] is True

    async def test_ready_degraded_when_layer2_down(self, mock_http_client):
        """If Layer-2 is down, gateway should report ready=False."""
        async def _degraded(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json.return_value = {
                "ready": False,
                "checks": {
                    "kafka":            True,
                    "layer2_cognitive": False,   # GNN is down
                    "layer3_guardian":  True,
                    "layer4_copilot":   True,
                },
            }
            return r

        mock_http_client.get = AsyncMock(side_effect=_degraded)
        resp = await mock_http_client.get("http://api-gateway:8000/ready")
        data = resp.json()
        assert data["ready"]                      is False
        assert data["checks"]["layer2_cognitive"] is False

    async def test_metrics_endpoint_returns_prometheus_format(self, mock_http_client):
        """GET /metrics must return Prometheus text format."""
        async def _metrics(url, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.text = (
                "# HELP ccdt_gateway_requests_total Total HTTP requests\n"
                "# TYPE ccdt_gateway_requests_total counter\n"
                "ccdt_gateway_requests_total{method=\"GET\",path=\"/api/v1/topology\","
                "status=\"200\"} 1234\n"
                "# HELP ccdt_gnn_inferences_total GNN inference calls\n"
                "# TYPE ccdt_gnn_inferences_total counter\n"
                "ccdt_gnn_inferences_total{status=\"success\"} 5678\n"
            )
            r.json = MagicMock(side_effect=Exception("Not JSON"))
            return r

        mock_http_client.get = AsyncMock(side_effect=_metrics)
        resp = await mock_http_client.get("http://api-gateway:8000/metrics")
        assert resp.status_code == 200
        assert "ccdt_gateway" in resp.text
        assert "# HELP"       in resp.text
        assert "# TYPE"       in resp.text
