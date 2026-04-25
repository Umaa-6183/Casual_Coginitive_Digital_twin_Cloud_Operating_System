"""
Unit tests — Layer-2 Cognitive Core (Causal GNN)
Tests NodeFeatures tensor layout, GnnInferenceResult properties,
TopologySnapshot graph operations, severity classification,
counterfactuals, and causal chain ordering.

All tests are network-free and sub-50ms.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.graph_pb2 import (
    NodeClass, IncidentType, NodeType, EdgeType, CausalDirection,
    NodeFeatures, EdgeFeatures,
    TopologyNode, TopologyEdge, CausalChainNode, TopFeature,
    CounterfactualResult, GnnInferenceResult, TopologySnapshot,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# NodeFeatures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer2
class TestNodeFeatures:
    def test_to_tensor_list_length(self):
        f = NodeFeatures()
        assert len(f.to_tensor_list()) == 16

    def test_tensor_index_ordering(self):
        """cpu_utilization must be index 0 — matches causal_gnn.py NODE_FEAT_DIM layout."""
        f = NodeFeatures(
            cpu_utilization=0.55,
            memory_utilization=0.70,
            oom_kill_rate=0.30,
        )
        t = f.to_tensor_list()
        assert t[0] == pytest.approx(0.55)   # cpu_utilization index 0
        assert t[1] == pytest.approx(0.70)   # memory_utilization index 1
        assert t[10] == pytest.approx(0.30)  # oom_kill_rate index 10

    def test_zero_features_zero_tensor(self):
        f = NodeFeatures()
        assert all(v == 0.0 for v in f.to_tensor_list())

    def test_anomaly_score_zero_for_healthy(self):
        f = NodeFeatures()
        assert f.anomaly_score() == 0.0

    def test_anomaly_score_max_all_signals(self):
        f = NodeFeatures(
            syscall_anomaly_score=1.0,
            capability_event_rate=1.0,
            oom_kill_rate=1.0,
            sensitive_file_rate=1.0,
            execve_rate=1.0,
            tcp_retransmit_rate=1.0,
        )
        score = f.anomaly_score()
        assert 0.0 <= score <= 1.0
        assert score > 0.5   # should be high when all signals maxed

    def test_anomaly_score_range_always_0_to_1(self):
        for cpu in (0.0, 0.5, 1.0):
            for oom in (0.0, 0.3, 0.9):
                f = NodeFeatures(cpu_utilization=cpu, oom_kill_rate=oom)
                s = f.anomaly_score()
                assert 0.0 <= s <= 1.0, f"score={s} out of range for cpu={cpu}, oom={oom}"

    def test_tensor_all_values_are_floats(self):
        f = NodeFeatures(cpu_utilization=0.5, restart_count=3.0)
        for v in f.to_tensor_list():
            assert isinstance(v, float)

    @pytest.mark.parametrize("field_name,idx", [
        ("cpu_utilization",       0),
        ("memory_utilization",    1),
        ("net_rx_bytes_rate",     2),
        ("net_tx_bytes_rate",     3),
        ("disk_read_rate",        4),
        ("disk_write_rate",       5),
        ("restart_count",         6),
        ("ready_replicas_ratio",  7),
        ("pod_age_seconds",       8),
        ("capability_event_rate", 9),
        ("oom_kill_rate",        10),
        ("tcp_retransmit_rate",  11),
        ("sched_latency_p99_us", 12),
        ("sensitive_file_rate",  13),
        ("syscall_anomaly_score",14),
        ("execve_rate",          15),
    ])
    def test_feature_index_mapping(self, field_name, idx):
        f = NodeFeatures(**{field_name: 0.77})
        assert f.to_tensor_list()[idx] == pytest.approx(0.77)


# ══════════════════════════════════════════════════════════════════════════════
# TopologyNode
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer2
class TestTopologyNode:
    def test_summary_healthy(self, healthy_node):
        s = healthy_node.summary()
        assert healthy_node.name in s
        assert "HEALTHY" in s
        assert "ROOT CAUSE" not in s

    def test_summary_root_cause_fault(self, fault_node):
        s = fault_node.summary()
        assert "ROOT CAUSE" in s
        assert "FAULT" in s
        assert fault_node.name in s

    def test_summary_attack_node(self, attack_node):
        s = attack_node.summary()
        assert "ATTACK" in s

    def test_is_anomalous_above_threshold(self, fault_node):
        assert fault_node.is_anomalous(threshold=0.1) is True

    def test_is_anomalous_healthy_below_threshold(self, healthy_node):
        assert healthy_node.is_anomalous(threshold=0.9) is False

    def test_is_anomalous_default_threshold(self, fault_node):
        assert fault_node.is_anomalous() is True

    def test_serialize_roundtrip_with_features(self, fault_node):
        raw  = fault_node.SerializeToString()
        back = TopologyNode.FromString(raw)
        assert back.node_id        == fault_node.node_id
        assert back.name           == fault_node.name
        assert back.is_root_cause  is True
        assert back.class_confidence == pytest.approx(0.91)

    def test_labels_preserved(self):
        node = TopologyNode(
            node_id="n1", name="svc",
            labels={"team": "payments", "tier": "backend"},
        )
        raw  = node.SerializeToString()
        back = TopologyNode.FromString(raw)
        assert back.labels.get("team") == "payments"

    def test_pod_ips_list(self):
        node = TopologyNode(
            node_id="n1", name="svc",
            pod_ips=["10.0.0.1", "10.0.0.2"],
        )
        raw  = node.SerializeToString()
        back = TopologyNode.FromString(raw)
        assert "10.0.0.1" in back.pod_ips
        assert len(back.pod_ips) == 2


# ══════════════════════════════════════════════════════════════════════════════
# GnnInferenceResult
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer2
class TestGnnInferenceResult:
    def test_is_active_incident_fault(self, fault_inference):
        assert fault_inference.is_active_incident is True

    def test_is_active_incident_attack(self, attack_inference):
        assert attack_inference.is_active_incident is True

    def test_is_not_active_heartbeat(self, heartbeat_inference):
        assert heartbeat_inference.is_active_incident is False

    def test_is_not_active_none(self):
        inf = GnnInferenceResult(incident_type=IncidentType.INCIDENT_NONE,
                                  graph_confidence=0.01)
        assert inf.is_active_incident is False

    def test_severity_low_healthy(self, heartbeat_inference):
        assert heartbeat_inference.severity == "none"

    def test_severity_medium_fault_low_confidence(self):
        inf = GnnInferenceResult(incident_type=IncidentType.INCIDENT_FAULT,
                                  graph_confidence=0.55)
        assert inf.severity in ("medium", "low")

    def test_severity_high_fault(self, fault_inference):
        assert fault_inference.severity == "high"

    def test_severity_critical_attack(self, attack_inference):
        assert attack_inference.severity == "critical"

    def test_severity_critical_fault_attack(self):
        inf = GnnInferenceResult(incident_type=IncidentType.INCIDENT_FAULT_ATTACK,
                                  graph_confidence=0.85)
        assert inf.severity == "critical"

    def test_fault_nodes_returns_only_fault(self, fault_inference):
        fault_nodes = fault_inference.fault_nodes()
        assert len(fault_nodes) == 1
        assert fault_nodes[0].node_class == NodeClass.NODE_CLASS_FAULT

    def test_attack_nodes_returns_only_attack(self, attack_inference):
        attack_nodes = attack_inference.attack_nodes()
        assert len(attack_nodes) == 1
        assert attack_nodes[0].node_class == NodeClass.NODE_CLASS_ATTACK

    def test_fault_nodes_empty_for_heartbeat(self, heartbeat_inference):
        assert heartbeat_inference.fault_nodes() == []

    def test_nl_summary_contains_root_cause(self, fault_inference):
        s = fault_inference.nl_summary()
        assert fault_inference.root_cause_node_name in s
        assert "FAULT" in s

    def test_nl_summary_contains_blast_radius(self, fault_inference):
        s = fault_inference.nl_summary()
        assert str(fault_inference.blast_radius_count) in s

    def test_nl_summary_heartbeat(self, heartbeat_inference):
        s = heartbeat_inference.nl_summary()
        assert "healthy" in s.lower() or "nominal" in s.lower()

    def test_nl_summary_attack_severity(self, attack_inference):
        s = attack_inference.nl_summary()
        assert "ATTACK" in s
        assert "CRITICAL" in s.upper() or "critical" in s.lower()

    def test_serialize_roundtrip(self, fault_inference):
        raw    = fault_inference.SerializeToString()
        loaded = GnnInferenceResult.FromString(raw)
        assert loaded.inference_id         == fault_inference.inference_id
        assert loaded.root_cause_node_name == fault_inference.root_cause_node_name
        assert loaded.graph_confidence     == pytest.approx(fault_inference.graph_confidence)
        assert len(loaded.node_classifications) == fault_inference.node_count
        assert len(loaded.top_features)         == len(fault_inference.top_features)

    def test_causal_chain_preserved(self, fault_inference):
        raw    = fault_inference.SerializeToString()
        loaded = GnnInferenceResult.FromString(raw)
        assert len(loaded.causal_chain) == 1
        assert loaded.causal_chain[0].node_name == fault_inference.root_cause_node_name

    def test_inference_latency_positive(self, fault_inference):
        assert fault_inference.inference_latency_ms > 0

    def test_counterfactual_result(self):
        cf = CounterfactualResult(
            node_id="n1",
            node_name="db-svc",
            incident_prob_if_healthy=0.05,
            incident_prob_actual=0.88,
            causal_effect=0.83,
            explanation="If db-svc were healthy, incident probability drops to 5%",
        )
        assert cf.causal_effect == pytest.approx(0.83)
        raw  = cf.SerializeToString()
        back = CounterfactualResult.FromString(raw)
        assert back.incident_prob_actual == pytest.approx(0.88)

    def test_top_features_importance_range(self, fault_inference):
        for feat in fault_inference.top_features:
            assert 0.0 <= feat.importance <= 1.0

    def test_schema_ver(self, fault_inference):
        assert fault_inference.schema_ver == "1.0"

    @pytest.mark.parametrize("incident_type,expected_active", [
        (IncidentType.INCIDENT_NONE,         False),
        (IncidentType.INCIDENT_FAULT,        True),
        (IncidentType.INCIDENT_ATTACK,       True),
        (IncidentType.INCIDENT_FAULT_ATTACK, True),
        (IncidentType.INCIDENT_PERFORMANCE,  True),
        (IncidentType.INCIDENT_RESOURCE,     True),
    ])
    def test_is_active_incident_all_types(self, incident_type, expected_active):
        inf = GnnInferenceResult(incident_type=incident_type, graph_confidence=0.8)
        assert inf.is_active_incident is expected_active


# ══════════════════════════════════════════════════════════════════════════════
# TopologySnapshot
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer2
class TestTopologySnapshot:
    def test_node_count(self, topology_snapshot):
        assert topology_snapshot.node_count == 2

    def test_edge_count(self, topology_snapshot):
        assert topology_snapshot.edge_count == 1

    def test_get_node_found(self, topology_snapshot, healthy_node):
        node = topology_snapshot.get_node(healthy_node.node_id)
        assert node is not None
        assert node.name == healthy_node.name

    def test_get_node_not_found(self, topology_snapshot):
        assert topology_snapshot.get_node("nonexistent-id") is None

    def test_get_neighbors_direct_edge(self, topology_snapshot, healthy_node, fault_node):
        neighbors = topology_snapshot.get_neighbors(healthy_node.node_id)
        neighbor_ids = {n.node_id for n in neighbors}
        assert fault_node.node_id in neighbor_ids

    def test_get_neighbors_isolated_node(self):
        node_a = TopologyNode(node_id="a", name="a")
        node_b = TopologyNode(node_id="b", name="b")
        node_c = TopologyNode(node_id="c", name="c")
        # Only edge: a → b (c has no edges)
        snap = TopologySnapshot(
            nodes=[node_a, node_b, node_c],
            edges=[TopologyEdge(edge_id="e1", source_node_id="a", target_node_id="b")],
        )
        assert snap.get_neighbors("c") == []

    def test_get_neighbors_multi_hop(self):
        """get_neighbors returns only direct neighbors, not transitive."""
        nodes = [TopologyNode(node_id=f"n{i}", name=f"svc-{i}") for i in range(4)]
        edges = [
            TopologyEdge(edge_id="e01", source_node_id="n0", target_node_id="n1"),
            TopologyEdge(edge_id="e12", source_node_id="n1", target_node_id="n2"),
            TopologyEdge(edge_id="e23", source_node_id="n2", target_node_id="n3"),
        ]
        snap = TopologySnapshot(nodes=nodes, edges=edges)
        neighbors_n0 = snap.get_neighbors("n0")
        assert len(neighbors_n0) == 1
        assert neighbors_n0[0].node_id == "n1"

    def test_serialize_roundtrip(self, topology_snapshot):
        raw  = topology_snapshot.SerializeToString()
        back = TopologySnapshot.FromString(raw)
        assert back.node_count        == topology_snapshot.node_count
        assert back.edge_count        == topology_snapshot.edge_count
        assert back.snapshot_id       == topology_snapshot.snapshot_id
        assert back.cluster_name      == topology_snapshot.cluster_name

    def test_empty_snapshot(self):
        snap = TopologySnapshot()
        assert snap.node_count == 0
        assert snap.edge_count == 0
        assert snap.get_node("x") is None

    def test_large_topology(self):
        """Performance: 100-node topology should be fast."""
        nodes = [TopologyNode(node_id=f"svc-{i}", name=f"service-{i}") for i in range(100)]
        edges = [
            TopologyEdge(edge_id=f"e{i}", source_node_id=f"svc-{i}",
                         target_node_id=f"svc-{(i+1) % 100}")
            for i in range(100)
        ]
        snap = TopologySnapshot(nodes=nodes, edges=edges)
        assert snap.node_count == 100
        assert snap.edge_count == 100
        # get_neighbors must complete quickly
        neighbors = snap.get_neighbors("svc-0")
        assert len(neighbors) == 1

    def test_causal_edges_flagged(self):
        n1 = TopologyNode(node_id="a", name="a")
        n2 = TopologyNode(node_id="b", name="b")
        causal_edge = TopologyEdge(
            edge_id="e1", source_node_id="a", target_node_id="b",
            is_causal=True,
            features=EdgeFeatures(causal_strength=0.87),
        )
        snap = TopologySnapshot(nodes=[n1, n2], edges=[causal_edge])
        assert snap.edges[0].is_causal is True
        assert snap.edges[0].features.causal_strength == pytest.approx(0.87)


# ══════════════════════════════════════════════════════════════════════════════
# CausalChainNode
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer2
class TestCausalChainNode:
    def test_construction(self):
        node = CausalChainNode(
            node_id="svc-1",
            node_name="auth-svc",
            node_class=NodeClass.NODE_CLASS_ATTACK,
            causal_score=0.91,
            do_calculus_prob=0.88,
            explanation="Privilege escalation detected",
            depth=0,
        )
        assert node.causal_score     == pytest.approx(0.91)
        assert node.do_calculus_prob == pytest.approx(0.88)
        assert node.depth            == 0

    def test_serialize_roundtrip(self):
        node = CausalChainNode(
            node_id="x", node_name="db",
            causal_score=0.75, depth=2,
            explanation="Database connection pool exhausted",
        )
        raw  = node.SerializeToString()
        back = CausalChainNode.FromString(raw)
        assert back.node_name  == "db"
        assert back.causal_score == pytest.approx(0.75)
        assert back.depth      == 2


# ══════════════════════════════════════════════════════════════════════════════
# NodeClass + IncidentType enum checks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.layer2
class TestEnums:
    def test_node_class_values_distinct(self):
        classes = {
            NodeClass.NODE_CLASS_UNKNOWN,
            NodeClass.NODE_CLASS_HEALTHY,
            NodeClass.NODE_CLASS_FAULT,
            NodeClass.NODE_CLASS_ATTACK,
        }
        assert len(classes) == 4

    def test_incident_type_values_distinct(self):
        types = {
            IncidentType.INCIDENT_UNKNOWN,
            IncidentType.INCIDENT_NONE,
            IncidentType.INCIDENT_FAULT,
            IncidentType.INCIDENT_ATTACK,
            IncidentType.INCIDENT_FAULT_ATTACK,
            IncidentType.INCIDENT_PERFORMANCE,
            IncidentType.INCIDENT_RESOURCE,
        }
        assert len(types) == 7

    @pytest.mark.parametrize("cls,name_fragment", [
        (NodeClass.NODE_CLASS_HEALTHY, "HEALTHY"),
        (NodeClass.NODE_CLASS_FAULT,   "FAULT"),
        (NodeClass.NODE_CLASS_ATTACK,  "ATTACK"),
    ])
    def test_node_class_name(self, cls, name_fragment):
        assert name_fragment in NodeClass(cls).name
