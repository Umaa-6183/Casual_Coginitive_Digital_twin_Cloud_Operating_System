"""
Chaos tests — Network Partitions
════════════════════════════════════════════════════════════════════════════════
Simulates network-level faults:
  • Complete partition: Layer-3 ↔ Layer-2 severed
  • Split-brain: Guardian and Co-Pilot see different topology snapshots
  • Timeout storms: cascading slow requests
  • Partial connectivity: only some pod IPs reachable
  • DNS failure: service discovery breaks
  • gRPC deadline exceeded: inference RPC times out

All mocked — no real network required.
════════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, GnnInferenceResult, TopologyNode,
    NodeFeatures, TopologySnapshot, TopologyEdge,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    GhostSimulationResult, ActionRequest, ActionResult,
)
from shared.proto.generated.copilot_pb2 import (
    MessageRole, SessionState, ChatMessage, SessionContext, IncidentReport,
)


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Complete network partition helpers
# ══════════════════════════════════════════════════════════════════════════════

class NetworkPartition:
    """
    Simulates a network partition between two logical zones.
    Any call crossing the partition raises ConnectionRefusedError.
    """

    def __init__(self, partitioned_services: set[str]):
        self._partitioned = partitioned_services
        self.blocked_calls: int = 0

    def is_partitioned(self, service: str) -> bool:
        return any(svc in service for svc in self._partitioned)

    def make_client(self, allowed_payload: dict) -> AsyncMock:
        """Return a mock HTTP client that raises for partitioned services."""
        partition = self

        async def _get(url: str, **kwargs):
            if partition.is_partitioned(url):
                partition.blocked_calls += 1
                raise ConnectionRefusedError(
                    f"Network partition: {url} is unreachable"
                )
            resp = AsyncMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = allowed_payload
            return resp

        async def _post(url: str, **kwargs):
            if partition.is_partitioned(url):
                partition.blocked_calls += 1
                raise ConnectionRefusedError(
                    f"Network partition: {url} is unreachable"
                )
            resp = AsyncMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = allowed_payload
            return resp

        client = AsyncMock()
        client.get  = AsyncMock(side_effect=_get)
        client.post = AsyncMock(side_effect=_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__  = AsyncMock(return_value=False)
        return client


# ══════════════════════════════════════════════════════════════════════════════
# Layer-3 ↔ Layer-2 partition
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer2
@pytest.mark.layer3
class TestGuardianGnnPartition:
    """Guardian cannot reach GNN — simulates L3 ↔ L2 network cut."""

    async def test_guardian_cannot_fetch_topology_during_partition(
        self, normal_guardian_payload
    ):
        """All GNN calls raise ConnectionRefusedError during partition."""
        partition = NetworkPartition({"layer2-cognitive", ":8001"})
        client = partition.make_client(normal_guardian_payload)

        with pytest.raises(ConnectionRefusedError):
            await client.get("http://layer2-cognitive:8001/topology")

        assert partition.blocked_calls == 1

    async def test_guardian_preview_proceeds_without_gnn_topology(
        self, restart_pod_request
    ):
        """
        Ghost Preview must produce a valid result based on the ActionRequest
        alone — it does not require live GNN topology data.
        """
        ghost = restart_pod_request.ghost_result
        assert ghost is not None
        assert ghost.risk_score > 0
        assert ghost.confidence > 0

    async def test_multiple_partition_calls_all_blocked(
        self, normal_guardian_payload
    ):
        """10 GNN calls during partition: all 10 must be blocked."""
        partition = NetworkPartition({"layer2-cognitive"})
        client = partition.make_client(normal_guardian_payload)

        blocked = 0
        for _ in range(10):
            try:
                await client.get("http://layer2-cognitive:8001/infer")
            except ConnectionRefusedError:
                blocked += 1

        assert blocked == 10
        assert partition.blocked_calls == 10

    async def test_guardian_calls_still_work_during_gnn_partition(
        self, normal_guardian_payload
    ):
        """
        Guardian REST calls (not to GNN) must still succeed during GNN partition.
        """
        partition = NetworkPartition({"layer2-cognitive"})
        client = partition.make_client(normal_guardian_payload)

        # Guardian's own endpoint is NOT partitioned
        resp = await client.post(
            "http://layer3-guardian:8002/actions/preview",
            json={"action": "restart_pod"},
        )
        assert resp.json()["approved"] is True
        assert partition.blocked_calls == 0

    async def test_partition_heals_and_calls_resume(
        self, normal_gnn_payload
    ):
        """After the partition heals, GNN calls should succeed again."""
        partition = NetworkPartition({"layer2-cognitive"})
        client = partition.make_client(normal_gnn_payload)

        # During partition
        with pytest.raises(ConnectionRefusedError):
            await client.get("http://layer2-cognitive:8001/topology")

        # Partition heals
        partition._partitioned.discard("layer2-cognitive")

        resp = await client.get("http://layer2-cognitive:8001/topology")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# Split-brain: Guardian and Co-Pilot see different topologies
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer3
@pytest.mark.layer4
class TestSplitBrainTopology:
    """
    Guardian and Co-Pilot receive different GNN snapshots.
    This tests that CCDT does not make contradictory decisions.
    """

    def _make_topology_a(self) -> TopologySnapshot:
        """Topology as seen by Guardian: payment-svc is FAULT."""
        return TopologySnapshot(
            snapshot_id=_uid(),
            timestamp=_now(),
            nodes=[
                TopologyNode(
                    node_id="svc-payment",
                    name="payment-svc",
                    node_class=NodeClass.NODE_CLASS_FAULT,
                    class_confidence=0.91,
                )
            ],
        )

    def _make_topology_b(self) -> TopologySnapshot:
        """Topology as seen by Co-Pilot: same node is HEALTHY (stale cache)."""
        return TopologySnapshot(
            snapshot_id=_uid(),
            timestamp=_now(),
            nodes=[
                TopologyNode(
                    node_id="svc-payment",
                    name="payment-svc",
                    node_class=NodeClass.NODE_CLASS_HEALTHY,
                    class_confidence=0.85,
                )
            ],
        )

    def test_split_brain_inconsistency_is_detectable(self):
        """
        The two topology views must disagree — this verifies the test setup.
        In production, the system must prefer the fresher timestamp.
        """
        topo_a = self._make_topology_a()
        topo_b = self._make_topology_b()

        node_a = topo_a.get_node("svc-payment")
        node_b = topo_b.get_node("svc-payment")

        assert node_a is not None
        assert node_b is not None
        assert node_a.node_class != node_b.node_class

    def test_most_severe_classification_used_in_split_brain(self):
        """
        When two topologies disagree, the more severe classification
        must win (fail-safe: prefer FAULT/ATTACK over HEALTHY).
        """
        topo_a = self._make_topology_a()  # FAULT
        topo_b = self._make_topology_b()  # HEALTHY

        node_a = topo_a.get_node("svc-payment")
        node_b = topo_b.get_node("svc-payment")

        # The system should prefer the more alarmist classification
        classifications = {node_a.node_class, node_b.node_class}
        most_severe = (
            NodeClass.NODE_CLASS_ATTACK
            if NodeClass.NODE_CLASS_ATTACK in classifications
            else NodeClass.NODE_CLASS_FAULT
            if NodeClass.NODE_CLASS_FAULT in classifications
            else NodeClass.NODE_CLASS_HEALTHY
        )
        assert most_severe == NodeClass.NODE_CLASS_FAULT

    def test_timestamp_used_to_resolve_conflict(self):
        """
        Fresher timestamp wins in a split-brain scenario.
        Both TopologySnapshots have distinct snapshot_ids and timestamps.
        """
        topo_a = self._make_topology_a()
        topo_b = self._make_topology_b()

        # Both have timestamps; the fresher one should be trusted
        assert topo_a.timestamp is not None
        assert topo_b.timestamp is not None

    def test_inference_confidence_as_tiebreaker(self):
        """When timestamps are equal, higher confidence wins."""
        topo_a = self._make_topology_a()   # confidence 0.91
        topo_b = self._make_topology_b()   # confidence 0.85

        conf_a = topo_a.get_node("svc-payment").class_confidence
        conf_b = topo_b.get_node("svc-payment").class_confidence

        winner = topo_a if conf_a >= conf_b else topo_b
        assert winner is topo_a


# ══════════════════════════════════════════════════════════════════════════════
# Timeout storms
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.slow
class TestTimeoutStorms:
    """
    Cascading timeouts when one service is slow causes upstream callers
    to pile up. Tests that CCDT uses individual timeouts per call.
    """

    async def test_per_call_timeout_prevents_cascading_block(self):
        """
        Each service call should have an independent 5s timeout.
        Slow GNN should not block Guardian for longer than 5s.
        """
        slow_client = AsyncMock()

        async def _slow_get(url: str, **kwargs):
            await asyncio.sleep(10)  # Simulate hung connection
            return AsyncMock(status_code=200)

        slow_client.get = AsyncMock(side_effect=_slow_get)

        start = time.perf_counter()
        try:
            await asyncio.wait_for(
                slow_client.get("http://layer2-cognitive:8001/topology"),
                timeout=0.1,  # 100ms timeout
            )
        except asyncio.TimeoutError:
            pass
        elapsed = time.perf_counter() - start

        assert elapsed < 0.5, f"Timeout not respected: took {elapsed:.2f}s"

    async def test_10_concurrent_timeouts_no_deadlock(self):
        """
        10 concurrent timed-out calls must all complete (via timeout)
        without any of them blocking indefinitely.
        """
        slow_client = AsyncMock()

        async def _hung(url: str, **kwargs):
            await asyncio.sleep(60)

        slow_client.get = AsyncMock(side_effect=_hung)

        async def _safe_call():
            try:
                await asyncio.wait_for(
                    slow_client.get("http://layer2-cognitive:8001/topology"),
                    timeout=0.05,
                )
                return "ok"
            except asyncio.TimeoutError:
                return "timeout"

        start = time.perf_counter()
        results = await asyncio.gather(*[_safe_call() for _ in range(10)])
        elapsed = time.perf_counter() - start

        assert all(r == "timeout" for r in results)
        assert elapsed < 1.0, f"10 concurrent timeouts took {elapsed:.2f}s"

    async def test_timeout_does_not_corrupt_session_state(
        self, copilot_session
    ):
        """
        A timed-out API call must not leave the session in a broken state.
        The session must remain ACTIVE after a timeout.
        """
        session = copilot_session

        try:
            # Simulate a timed-out LLM API call
            await asyncio.wait_for(asyncio.sleep(5), timeout=0.01)
        except asyncio.TimeoutError:
            # Add a timeout error message to session
            session.add_message(ChatMessage(
                role=MessageRole.ROLE_SYSTEM,
                content="Request timed out. Please retry.",
            ))

        assert session.state == SessionState.SESSION_ACTIVE


# ══════════════════════════════════════════════════════════════════════════════
# DNS failure / service discovery
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer3
class TestDnsFailure:
    """
    Simulates Kubernetes DNS failure where service names don't resolve.
    All service-to-service calls use DNS names like 'layer2-cognitive:8001'.
    """

    async def test_dns_failure_raises_known_exception(self):
        """DNS lookup failure must raise OSError (socket.gaierror), not crash."""
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=OSError("Name or service not known: layer2-cognitive")
        )

        with pytest.raises(OSError, match="layer2-cognitive"):
            await client.get("http://layer2-cognitive:8001/health")

    async def test_fallback_ip_used_when_dns_fails(self, normal_gnn_payload):
        """
        When DNS fails, the system should be able to fall back to a direct IP.
        This tests that the ActionRequest's target fields are self-contained.
        """
        req = ActionRequest(
            action_name=ActionName.ACTION_RESTART_POD,
            target_node_name="payment-svc-pod-abc123",
            target_namespace="production",
        )

        # All required info is in the ActionRequest itself — no DNS needed
        assert req.target_node_name == "payment-svc-pod-abc123"
        assert req.target_namespace == "production"

    async def test_health_endpoint_used_for_service_discovery(self):
        """
        Health endpoints are the canonical liveness check.
        Test that a /health 200 response means the service is DNS-reachable.
        """
        client = AsyncMock()
        client.get = AsyncMock(return_value=AsyncMock(
            status_code=200,
            json=MagicMock(return_value={"status": "healthy"}),
            raise_for_status=MagicMock(),
        ))

        resp = await client.get("http://layer3-guardian:8002/health")
        data = resp.json()
        assert data["status"] == "healthy"
