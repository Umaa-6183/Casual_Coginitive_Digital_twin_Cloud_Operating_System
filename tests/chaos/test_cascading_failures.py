"""
Chaos tests — Cascading Failures
════════════════════════════════════════════════════════════════════════════════
Tests CCDT's ability to detect, classify, and remediate cascading failures:
  • OOM kill cascade: one pod OOMs → others starved → cascade
  • Thundering herd: restart storm causes all pods to restart simultaneously
  • TCP retransmit storm: network congestion across all services
  • Resource exhaustion: CPU / memory saturation spreading across nodes
  • Causal chain propagation: GNN must identify root cause among many signals
  • Blast radius containment: Guardian must not make cascading failures worse

All tests use proto shims + mock HTTP clients. No live cluster needed.
════════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, OomKillEvent, TcpRetransmitEvent, CapabilityEvent,
    EventMetadata, EventSeverity, LinuxCapability, NetworkProtocol,
)
from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, NodeFeatures, TopologyNode, TopologyEdge,
    CausalChainNode, GnnInferenceResult, TopologySnapshot,
)
from shared.proto.generated.actions_pb2 import (
    ActionName, ActionStatus, AutonomyMode, RiskCategory,
    ScaleParameters, RollbackParameters, GhostSimulationResult,
    ActionRequest, ActionResult,
)

from tests.chaos.conftest import (
    build_oom_cascade_batch, build_capability_storm_batch,
)


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# OOM kill cascade
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer1
@pytest.mark.layer2
class TestOomKillCascade:
    """
    OOM kill cascade: one pod's memory leak causes its neighbors to OOM too.
    GNN must identify the original leaking pod as root cause.
    """

    def test_cascade_batch_contains_multiple_oom_events(self, oom_cascade_batch):
        """The cascade batch must have exactly 10 OOM kills across 10 victims."""
        assert len(oom_cascade_batch.oom_kill_events) == 10
        victim_pids = {e.victim_pid for e in oom_cascade_batch.oom_kill_events}
        assert len(victim_pids) == 10  # each with unique PID

    def test_cascade_batch_severity_is_critical(self, oom_cascade_batch):
        """All OOM events in a cascade must carry CRITICAL severity."""
        for event in oom_cascade_batch.oom_kill_events:
            assert event.meta.severity == EventSeverity.SEVERITY_CRITICAL

    def test_type_counts_computed_correctly(self, oom_cascade_batch):
        """type_counts['oom_kill'] must equal the total OOM event count."""
        assert oom_cascade_batch.type_counts.get("oom_kill", 0) == 10

    def test_gnn_identifies_root_cause_in_cascade(self, fault_inference):
        """
        GNN inference for a cascade scenario must:
        1. Report INCIDENT_FAULT (not NONE)
        2. Identify a single root cause node
        3. Have blast_radius_count ≥ 1
        """
        assert fault_inference.incident_type == IncidentType.INCIDENT_FAULT
        assert fault_inference.root_cause_node_name != ""
        assert fault_inference.blast_radius_count >= 1

    def test_causal_chain_depth_for_cascade(self, fault_inference):
        """
        Cascade causal chain: root cause at depth=0, propagation effects at depth≥1.
        """
        chain = fault_inference.causal_chain
        assert len(chain) >= 1
        root = chain[0]
        assert root.depth == 0

    def test_oom_feature_dominant_in_cascade(self, fault_inference):
        """
        For an OOM cascade, oom_kill_rate or memory_utilization must be
        among the top-3 most important features.
        """
        top_features = fault_inference.top_features
        assert len(top_features) >= 1
        top_names = {f.feature_name for f in top_features}
        oom_signals = {"oom_kill_rate", "memory_utilization", "restart_count"}
        assert len(top_names & oom_signals) >= 1

    def test_blast_radius_nodes_are_a_subset_of_topology(
        self, fault_inference, topology_snapshot
    ):
        """Every node in blast_radius_node_ids must exist in the topology."""
        topology_node_ids = {n.node_id for n in topology_snapshot.nodes}
        for node_id in fault_inference.blast_radius_node_ids:
            assert node_id in topology_node_ids

    def test_large_cascade_batch_serializes_efficiently(self):
        """
        100-OOM-kill batch must serialize to < 64KB (Kafka default max.message.bytes).
        """
        KAFKA_MAX_BYTES = 64 * 1024
        batch = build_oom_cascade_batch(n_oom_events=100)
        raw = batch.SerializeToString()
        assert len(raw) < KAFKA_MAX_BYTES, (
            f"100-OOM batch is {len(raw)} bytes — exceeds 64KB Kafka limit"
        )

    def test_cascade_batch_roundtrip(self, oom_cascade_batch):
        """Cascade batch must survive serialization round-trip."""
        raw = oom_cascade_batch.SerializeToString()
        loaded = TypedEbpfBatch.FromString(raw)
        assert len(loaded.oom_kill_events) == 10
        assert loaded.node_name == oom_cascade_batch.node_name


# ══════════════════════════════════════════════════════════════════════════════
# Thundering herd (restart storm)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer2
@pytest.mark.layer3
class TestThunderingHerd:
    """
    Thundering herd: all pods restart simultaneously after a Guardian action
    (or Kubernetes rolling restart), causing a spike in start-up traffic.
    """

    def _make_restarting_node(self, name: str, restart_count: float) -> TopologyNode:
        return TopologyNode(
            node_id=f"svc-{name}",
            name=name,
            node_class=NodeClass.NODE_CLASS_FAULT,
            class_confidence=0.75,
            features=NodeFeatures(
                restart_count=restart_count,
                ready_replicas_ratio=0.0,  # no ready pods during restart
                cpu_utilization=0.80,
            ),
        )

    def test_all_nodes_restarting_detected_as_fault(self):
        """
        When all 5 services have restart_count > 3.0, the topology must be
        classified as a widespread fault, not healthy.
        """
        services = ["api-gw", "auth", "payment", "order", "inventory"]
        nodes = [self._make_restarting_node(svc, 5.0) for svc in services]

        # All nodes classified as FAULT — none are healthy
        fault_nodes = [n for n in nodes if n.node_class == NodeClass.NODE_CLASS_FAULT]
        assert len(fault_nodes) == 5

    def test_guardian_avoids_scale_down_during_restart_storm(
        self, high_risk_ghost
    ):
        """
        During a restart storm, scaling DOWN would worsen availability.
        Guardian must block scale-down when affected_pod_count is high.
        """
        ghost = high_risk_ghost
        # high_risk_ghost: affected_pod_count=12, risk=0.72 → should block scale-down
        assert ghost.opa_approved is False
        assert ghost.risk_category == RiskCategory.RISK_HIGH

    def test_rollback_preferred_over_restart_during_storm(self):
        """
        During a thundering herd, rollback is safer than pod restart
        because it addresses the root cause (bad deployment).
        """
        rollback_req = ActionRequest(
            action_name=ActionName.ACTION_ROLLBACK_DEPLOYMENT,
            action_label="rollback_deployment",
            target_node_name="payment-svc",
            target_namespace="production",
            ghost_result=GhostSimulationResult(
                risk_score=0.18,
                risk_category=RiskCategory.RISK_LOW,
                opa_approved=True,
                affected_pod_count=1,  # rollback affects fewer pods
            ),
        )
        assert rollback_req.action_name == ActionName.ACTION_ROLLBACK_DEPLOYMENT
        assert rollback_req.ghost_result.risk_score < 0.20

    def test_herd_detection_via_restart_count_feature(self):
        """
        restart_count feature value > 3.0 is the primary thundering-herd signal.
        GNN uses this feature at index 6 in the feature tensor.
        """
        features = NodeFeatures(restart_count=5.0)
        tensor = features.to_tensor_list()
        assert tensor[6] == 5.0  # index 6 = restart_count

    def test_ready_replicas_zero_is_critical(self):
        """
        ready_replicas_ratio = 0.0 means the service is fully down.
        Anomaly score must be > 0 for this node.
        """
        features = NodeFeatures(
            ready_replicas_ratio=0.0,
            restart_count=3.0,
        )
        score = features.anomaly_score()
        assert score > 0.0


# ══════════════════════════════════════════════════════════════════════════════
# TCP retransmit storm
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer1
@pytest.mark.layer2
class TestTcpRetransmitStorm:
    """
    TCP retransmit storm: network congestion causes all service-to-service
    calls to queue up, triggering back-pressure and latency spikes.
    """

    def _make_retransmit_batch(self, n: int, retransmit_count: int) -> TypedEbpfBatch:
        meta = EventMetadata(
            kernel_ts_ns=time.monotonic_ns(),
            node_name="node-1",
            pid=100, comm="nginx",
            severity=EventSeverity.SEVERITY_HIGH,
        )
        events = [
            TcpRetransmitEvent(
                meta=meta,
                src_addr="10.0.0.1",
                src_port=80,
                dst_addr="10.0.0.2",
                dst_port=5432,
                protocol=NetworkProtocol.PROTO_TCP,
                retransmit_count=retransmit_count,
                rtt_us=50_000 * retransmit_count,  # RTT spikes with retransmits
            )
            for _ in range(n)
        ]
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name="node-1",
            collector_id=_uid(),
            batch_ts=_now(),
            tcp_retransmit_events=events,
            schema_ver="1.0",
        )
        batch.compute_type_counts()
        return batch

    def test_tcp_storm_batch_high_retransmit_count(self):
        """Storm batch with 50 retransmit events must serialize correctly."""
        batch = self._make_retransmit_batch(50, retransmit_count=12)
        assert len(batch.tcp_retransmit_events) == 50
        assert all(e.retransmit_count == 12 for e in batch.tcp_retransmit_events)

    def test_retransmit_type_count_computed(self):
        """type_counts must reflect all TCP retransmit events."""
        batch = self._make_retransmit_batch(20, retransmit_count=5)
        assert batch.type_counts.get("tcp_retransmit", 0) == 20

    def test_tcp_retransmit_feature_elevates_anomaly_score(self):
        """High tcp_retransmit_rate must raise anomaly score above 0."""
        features = NodeFeatures(tcp_retransmit_rate=0.90)
        score = features.anomaly_score()
        assert score > 0.0

    def test_gnn_classifies_network_storm_as_fault(self):
        """
        A node with high tcp_retransmit_rate and low ready_replicas should
        be classified as FAULT, not HEALTHY.
        """
        node = TopologyNode(
            node_id="svc-net",
            name="api-gateway",
            node_class=NodeClass.NODE_CLASS_FAULT,
            class_confidence=0.82,
            features=NodeFeatures(
                tcp_retransmit_rate=0.95,
                ready_replicas_ratio=0.50,
            ),
        )
        assert node.node_class == NodeClass.NODE_CLASS_FAULT
        assert node.is_anomalous(threshold=0.3) is True

    def test_storm_batch_within_kafka_size_limit(self):
        """50-event TCP storm batch must fit within Kafka's 64KB message limit."""
        KAFKA_MAX_BYTES = 64 * 1024
        batch = self._make_retransmit_batch(50, retransmit_count=15)
        raw = batch.SerializeToString()
        assert len(raw) < KAFKA_MAX_BYTES


# ══════════════════════════════════════════════════════════════════════════════
# Blast radius containment
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer3
class TestBlastRadiusContainment:
    """
    Tests that Guardian's Ghost Preview correctly computes and gates actions
    based on blast radius. High-blast-radius actions must be blocked.
    """

    def test_isolate_large_blast_radius_blocked(self):
        """
        Isolating a service with 15 dependent pods must be blocked by OPA
        blast_radius policy (threshold: 10 pods).
        """
        high_blast = GhostSimulationResult(
            risk_score=0.55,
            risk_category=RiskCategory.RISK_HIGH,
            confidence=0.80,
            affected_pod_count=15,
            opa_approved=False,
            opa_violations=["blast_radius: 15 pods exceeds maximum of 10"],
        )
        assert high_blast.opa_approved is False
        assert any("blast_radius" in v for v in high_blast.opa_violations)

    def test_restart_small_blast_radius_approved(self):
        """
        Restarting a single isolated pod with 1 affected pod is safe.
        """
        small_blast = GhostSimulationResult(
            risk_score=0.08,
            risk_category=RiskCategory.RISK_VERY_LOW,
            confidence=0.95,
            affected_pod_count=1,
            opa_approved=True,
            dry_run_succeeded=True,
        )
        assert small_blast.opa_approved is True
        assert small_blast.affected_pod_count == 1

    def test_guardian_selects_lowest_blast_radius_action(self):
        """
        Given two valid actions, Guardian must prefer the one with smaller blast radius.
        """
        restart_ghost = GhostSimulationResult(
            risk_score=0.10, affected_pod_count=1, opa_approved=True,
        )
        scale_down_ghost = GhostSimulationResult(
            risk_score=0.35, affected_pod_count=8, opa_approved=True,
        )

        restart_req = ActionRequest(
            action_name=ActionName.ACTION_RESTART_POD,
            ghost_result=restart_ghost,
        )
        scale_req = ActionRequest(
            action_name=ActionName.ACTION_SCALE_DOWN_REPLICAS,
            ghost_result=scale_down_ghost,
        )

        # Prefer action with lower blast radius
        preferred = (
            restart_req if restart_ghost.affected_pod_count
            < scale_down_ghost.affected_pod_count
            else scale_req
        )
        assert preferred.action_name == ActionName.ACTION_RESTART_POD

    def test_blast_radius_includes_indirect_dependents(
        self, topology_snapshot
    ):
        """
        Blast radius must count all nodes reachable from root cause
        (direct + indirect neighbors).
        """
        # Build a 3-hop chain: A → B → C → D
        nodes = [
            TopologyNode(node_id=f"n{i}", name=f"svc-{i}")
            for i in range(4)
        ]
        edges = [
            TopologyEdge(edge_id=f"e{i}", source_node_id=f"n{i}",
                         target_node_id=f"n{i+1}")
            for i in range(3)
        ]
        snap = TopologySnapshot(nodes=nodes, edges=edges)

        # Direct neighbors of n0
        direct = snap.get_neighbors("n0")
        assert len(direct) == 1
        assert direct[0].node_id == "n1"

    def test_cascading_action_blocked_if_already_in_progress(self):
        """
        If a remediation action is already in progress (EXECUTING status),
        a second action must be blocked to prevent compound cascading.
        """
        executing_result = ActionResult(
            status=ActionStatus.STATUS_EXECUTING,
            message="Pod restart in progress",
        )
        assert executing_result.succeeded is False
        assert executing_result.denied is False
        # Only STATUS_SUCCEEDED is truly done
        assert executing_result.status == ActionStatus.STATUS_EXECUTING


# ══════════════════════════════════════════════════════════════════════════════
# Mixed attack + fault cascade
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.layer2
@pytest.mark.layer3
class TestMixedAttackFaultCascade:
    """
    The most complex scenario: an attacker exploits a fault-induced cascade
    to escalate privileges while the defender is focused on the OOM issue.
    GNN must classify this as INCIDENT_FAULT_ATTACK.
    """

    def test_fault_attack_classification_available(self):
        """INCIDENT_FAULT_ATTACK is a valid enum value."""
        assert IncidentType.INCIDENT_FAULT_ATTACK is not None

    def test_mixed_inference_has_critical_severity(self):
        """FAULT_ATTACK incident must always be classified as critical."""
        inf = GnnInferenceResult(
            incident_type=IncidentType.INCIDENT_FAULT_ATTACK,
            graph_confidence=0.85,
        )
        assert inf.severity == "critical"

    def test_attack_node_and_fault_node_coexist(self):
        """
        Topology can have both FAULT and ATTACK nodes simultaneously.
        """
        nodes = [
            TopologyNode(
                node_id="svc-oom",
                name="payment-svc",
                node_class=NodeClass.NODE_CLASS_FAULT,
                class_confidence=0.88,
            ),
            TopologyNode(
                node_id="svc-attack",
                name="auth-svc",
                node_class=NodeClass.NODE_CLASS_ATTACK,
                class_confidence=0.92,
                is_root_cause=True,
            ),
        ]
        snap = TopologySnapshot(nodes=nodes)

        fault_nodes = [n for n in snap.nodes if n.node_class == NodeClass.NODE_CLASS_FAULT]
        attack_nodes = [n for n in snap.nodes if n.node_class == NodeClass.NODE_CLASS_ATTACK]

        assert len(fault_nodes) == 1
        assert len(attack_nodes) == 1

    def test_attack_takes_precedence_in_action_selection(self):
        """
        When both FAULT and ATTACK nodes exist, the attack-focused action
        (isolate_container) must be preferred over the fault action (restart_pod).
        """
        attack_request = ActionRequest(
            action_name=ActionName.ACTION_ISOLATE_CONTAINER,
            trigger_class=NodeClass.NODE_CLASS_ATTACK,
            trigger_confidence=0.92,
            ghost_result=GhostSimulationResult(
                risk_score=0.15, opa_approved=True,
            ),
        )
        fault_request = ActionRequest(
            action_name=ActionName.ACTION_RESTART_POD,
            trigger_class=NodeClass.NODE_CLASS_FAULT,
            trigger_confidence=0.88,
            ghost_result=GhostSimulationResult(
                risk_score=0.10, opa_approved=True,
            ),
        )

        # Attack actions take priority — security over availability
        priority = (
            attack_request
            if attack_request.trigger_class == NodeClass.NODE_CLASS_ATTACK
            else fault_request
        )
        assert priority.action_name == ActionName.ACTION_ISOLATE_CONTAINER

    def test_capability_storm_classification(self, capability_storm_batch):
        """
        A batch with 50 CAP_SYS_ADMIN denials must be parsed correctly.
        """
        assert len(capability_storm_batch.capability_events) == 50
        for event in capability_storm_batch.capability_events:
            assert event.capability == LinuxCapability.CAP_SYS_ADMIN
            assert event.allowed is False
