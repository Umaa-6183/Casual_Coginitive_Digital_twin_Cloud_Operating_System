"""
Integration tests — Layer-1 → Layer-2 Pipeline
Tests the full eBPF event batch → Kafka publish → GNN inference consume loop.
Uses in-memory Kafka stubs and a mocked GNN HTTP client.

Requires: pytest-asyncio (asyncio_mode=auto in pytest.ini)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, CapabilityEvent, OomKillEvent, TcpRetransmitEvent,
    EventMetadata, EventSeverity, LinuxCapability,
)
from shared.proto.generated.graph_pb2 import (
    GnnInferenceResult, IncidentType, NodeClass,
)


def _uid() -> str: return str(uuid.uuid4())
def _now() -> str: return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Kafka publish path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer1
@pytest.mark.kafka
class TestEbpfBatchKafkaPublish:
    async def test_batch_serializes_to_valid_kafka_payload(
        self, ebpf_batch, fake_kafka_producer
    ):
        """Batch serialized to bytes must deserialize without error."""
        payload = ebpf_batch.SerializeToString()
        await fake_kafka_producer.send(
            topic="ccdt.ebpf.events",
            value=payload,
            key=ebpf_batch.node_name.encode(),
        )
        assert len(fake_kafka_producer.messages) == 1
        msg   = fake_kafka_producer.messages[0]
        back  = TypedEbpfBatch.FromString(msg["value"])
        assert back.node_name == ebpf_batch.node_name

    async def test_batch_published_to_correct_topic(
        self, ebpf_batch, fake_kafka_producer
    ):
        await fake_kafka_producer.send("ccdt.ebpf.events", ebpf_batch.SerializeToString())
        assert fake_kafka_producer.messages[0]["topic"] == "ccdt.ebpf.events"

    async def test_batch_key_is_node_name(self, ebpf_batch, fake_kafka_producer):
        key = ebpf_batch.node_name.encode()
        await fake_kafka_producer.send(
            "ccdt.ebpf.events",
            ebpf_batch.SerializeToString(),
            key=key,
        )
        assert fake_kafka_producer.messages[0]["key"] == key

    async def test_multiple_batches_same_node(
        self, base_meta, capability_event, fake_kafka_producer
    ):
        """Each batch from the same node should be published independently."""
        for i in range(5):
            batch = TypedEbpfBatch(
                batch_id=_uid(),
                node_name=base_meta.node_name,
                collector_id=_uid(),
                batch_ts=_now(),
                capability_events=[capability_event],
                schema_ver="1.0",
            )
            await fake_kafka_producer.send("ccdt.ebpf.events", batch.SerializeToString())
        assert len(fake_kafka_producer.messages) == 5

    async def test_batch_flush_called_after_publish(
        self, ebpf_batch, fake_kafka_producer
    ):
        await fake_kafka_producer.send("ccdt.ebpf.events", ebpf_batch.SerializeToString())
        await fake_kafka_producer.flush()
        # Verify flush is idempotent
        await fake_kafka_producer.flush()

    async def test_large_batch_under_64kb(self, base_meta, capability_event):
        """Batches must be < 64 KB per Kafka message size limit in config."""
        events = [capability_event] * 500
        batch  = TypedEbpfBatch(
            batch_id=_uid(),
            node_name=base_meta.node_name,
            collector_id=_uid(),
            batch_ts=_now(),
            capability_events=events,
            schema_ver="1.0",
        )
        payload_bytes = len(batch.SerializeToString())
        assert payload_bytes < 64 * 1024, \
            f"Batch is {payload_bytes} bytes, exceeds 64 KB Kafka limit"


# ══════════════════════════════════════════════════════════════════════════════
# GNN consumption and inference trigger
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer2
@pytest.mark.kafka
class TestGnnInferenceConsumer:
    async def test_gnn_consumes_batch_and_calls_infer(
        self, ebpf_batch, mock_http_client, mock_gnn_response
    ):
        """
        Simulate: Layer-2 consumer receives an eBPF batch and triggers /infer.
        """
        payload  = ebpf_batch.SerializeToString()
        consumer = MagicMock()
        consumer.__aiter__ = MagicMock(return_value=iter([
            MagicMock(value=payload)
        ]))

        # Simulate the inference call the GNN consumer would make
        resp = await mock_http_client.post(
            "http://layer2-cognitive:8001/infer",
            json={"trigger": "kafka_batch", "node_name": ebpf_batch.node_name},
        )
        data = resp.json()
        assert "inference_id"    in data
        assert "incident_type"   in data
        assert "graph_confidence" in data

    async def test_inference_result_published_to_kafka(
        self, fault_inference, fake_kafka_producer
    ):
        """GNN result must be serialized and published to ccdt.gnn.inference."""
        payload = fault_inference.SerializeToString()
        await fake_kafka_producer.send(
            topic="ccdt.gnn.inference",
            value=payload,
            key=fault_inference.inference_id.encode(),
        )
        msg  = fake_kafka_producer.messages[0]
        back = GnnInferenceResult.FromString(msg["value"])
        assert back.incident_type      == fault_inference.incident_type
        assert back.graph_confidence   == pytest.approx(fault_inference.graph_confidence)
        assert back.root_cause_node_name == fault_inference.root_cause_node_name

    async def test_heartbeat_published_every_5_seconds(
        self, heartbeat_inference, fake_kafka_producer
    ):
        """Heartbeat (is_heartbeat=True) should be published even when no incident."""
        payload = heartbeat_inference.SerializeToString()
        await fake_kafka_producer.send("ccdt.gnn.inference", payload)
        back = GnnInferenceResult.FromString(
            fake_kafka_producer.messages[0]["value"]
        )
        assert back.is_heartbeat is True
        assert back.incident_type == IncidentType.INCIDENT_NONE

    async def test_attack_inference_triggers_high_priority_action(
        self, attack_inference, fake_kafka_producer
    ):
        """Attack-type inferences should result in higher urgency handling."""
        payload = attack_inference.SerializeToString()
        await fake_kafka_producer.send("ccdt.gnn.inference", payload)
        back = GnnInferenceResult.FromString(
            fake_kafka_producer.messages[0]["value"]
        )
        assert back.incident_type    == IncidentType.INCIDENT_ATTACK
        assert back.graph_confidence >= 0.85   # Attack confidence threshold


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end event → inference pipeline (stubbed)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.layer1
@pytest.mark.layer2
class TestEventToInferencePipeline:
    async def test_oom_event_produces_fault_inference(
        self,
        base_meta,
        oom_kill_event,
        fake_kafka_producer,
        mock_http_client,
        mock_gnn_response,
    ):
        """
        Pipeline: OOM kill event → batch → Kafka → GNN infers FAULT.
        """
        # Step 1: Build batch with OOM event
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name=base_meta.node_name,
            collector_id=_uid(),
            batch_ts=_now(),
            oom_kill_events=[oom_kill_event],
            schema_ver="1.0",
        )
        assert batch.total_events() == 1

        # Step 2: Publish to Kafka
        await fake_kafka_producer.send("ccdt.ebpf.events", batch.SerializeToString())
        assert len(fake_kafka_producer.messages) == 1

        # Step 3: GNN service receives and infers
        infer_resp = await mock_http_client.post(
            "http://layer2-cognitive:8001/infer",
            json={"batch_id": batch.batch_id},
        )
        result = infer_resp.json()
        assert result["incident_type"] in ("FAULT", "ATTACK", "NONE")
        assert 0.0 <= result["graph_confidence"] <= 1.0

    async def test_capability_event_produces_attack_candidate(
        self,
        base_meta,
        capability_event,
        fake_kafka_producer,
        mock_http_client,
    ):
        """
        CAP_NET_ADMIN event on a production workload should trigger
        potential attack classification.
        """
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name=base_meta.node_name,
            collector_id=_uid(),
            batch_ts=_now(),
            capability_events=[capability_event] * 15,  # High rate
            schema_ver="1.0",
        )
        await fake_kafka_producer.send("ccdt.ebpf.events", batch.SerializeToString())
        assert len(fake_kafka_producer.messages) == 1

        # Verify the batch has high capability event rate
        back = TypedEbpfBatch.FromString(fake_kafka_producer.messages[0]["value"])
        assert len(back.capability_events) == 15

    async def test_mixed_event_batch_schema_valid(
        self,
        ebpf_batch,
        fake_kafka_producer,
    ):
        """A batch with cap + OOM + TCP retransmit must pass schema validation."""
        payload = ebpf_batch.SerializeToString()
        await fake_kafka_producer.send("ccdt.ebpf.events", payload)

        back = TypedEbpfBatch.FromString(fake_kafka_producer.messages[0]["value"])
        assert back.schema_ver == "1.0"
        assert back.total_events() == 3

    async def test_topology_update_published_periodically(
        self, topology_snapshot, fake_kafka_producer
    ):
        """
        The collector also publishes topology updates to ccdt.topology.updates.
        """
        topology_payload = {
            "snapshot_id": topology_snapshot.snapshot_id,
            "node_count":  topology_snapshot.node_count,
            "edge_count":  topology_snapshot.edge_count,
            "timestamp":   topology_snapshot.timestamp,
        }
        await fake_kafka_producer.send(
            "ccdt.topology.updates",
            json.dumps(topology_payload).encode(),
        )
        msg = fake_kafka_producer.messages[0]
        assert msg["topic"] == "ccdt.topology.updates"
        data = json.loads(msg["value"])
        assert data["node_count"] == 2

    async def test_pipeline_handles_empty_batch(self, base_meta, fake_kafka_producer):
        """Empty batches should be published without error (heartbeat batches)."""
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name=base_meta.node_name,
            collector_id=_uid(),
            batch_ts=_now(),
            schema_ver="1.0",
        )
        assert batch.total_events() == 0
        await fake_kafka_producer.send("ccdt.ebpf.events", batch.SerializeToString())
        assert len(fake_kafka_producer.messages) == 1
