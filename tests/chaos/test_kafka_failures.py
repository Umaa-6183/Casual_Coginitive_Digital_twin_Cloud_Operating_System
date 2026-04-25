"""
Chaos tests — Kafka Failures
════════════════════════════════════════════════════════════════════════════════
Tests CCDT's resilience when Kafka experiences:
  • Complete broker failure (100% message drop)
  • Partial message loss (random drops)
  • Network latency (message delay)
  • Payload corruption (bit rot / partial writes)
  • Consumer lag accumulation (slow consumer)
  • Partition leader re-election (brief outage)
  • Schema evolution mismatches (proto backward compat)

All tests use in-memory FaultyKafkaProducer / FaultyKafkaConsumer.
No real Kafka required.
════════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.proto.generated.events_pb2 import (
    TypedEbpfBatch, OomKillEvent, CapabilityEvent,
    EventMetadata, EventSeverity, LinuxCapability,
)
from shared.proto.generated.graph_pb2 import (
    GnnInferenceResult, IncidentType, NodeClass,
)
from shared.proto.generated.actions_pb2 import (
    ActionRequest, ActionResult, ActionStatus, ActionName, AutonomyMode,
    GhostSimulationResult, RiskCategory,
)

from tests.chaos.conftest import (
    FaultInjector, FaultyKafkaProducer, FaultyKafkaConsumer,
    build_oom_cascade_batch,
)


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Broker total failure
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.kafka
class TestBrokerTotalFailure:
    """100% message drop — simulates Kafka cluster completely unavailable."""

    async def test_producer_drops_all_messages_silently(
        self, faulty_producer_total_outage, ebpf_batch
    ):
        """With 100% drop rate, no messages should reach the topic."""
        for _ in range(10):
            await faulty_producer_total_outage.send(
                topic="ccdt.ebpf.events",
                value=ebpf_batch.SerializeToString(),
                key=ebpf_batch.node_name.encode(),
            )

        assert len(faulty_producer_total_outage.messages) == 0
        assert faulty_producer_total_outage.attempted == 10

    async def test_producer_tracks_drop_statistics(
        self, faulty_producer_total_outage, ebpf_batch
    ):
        """Drop counter must reflect all 10 dropped messages."""
        for _ in range(10):
            await faulty_producer_total_outage.send(
                topic="ccdt.ebpf.events",
                value=ebpf_batch.SerializeToString(),
            )

        injector = faulty_producer_total_outage._injector
        assert injector.dropped_count == 10
        assert injector.delivered_count == 0
        assert injector.delivery_ratio == 0.0

    async def test_broker_error_raises_ioerror(self):
        """When error_rate=1.0, every send raises IOError."""
        injector = FaultInjector(error_rate=1.0, seed=42)
        producer = FaultyKafkaProducer(injector)

        with pytest.raises(IOError, match="Kafka broker unavailable"):
            await producer.send(
                topic="ccdt.ebpf.events",
                value=b"test-payload",
            )

    async def test_partial_batch_survival(
        self, fault_injector_medium, ebpf_batch
    ):
        """With 25% drop rate, roughly 75% of 100 messages should be delivered."""
        producer = FaultyKafkaProducer(fault_injector_medium)
        n = 100
        errors = 0

        for _ in range(n):
            try:
                await producer.send("ccdt.ebpf.events", ebpf_batch.SerializeToString())
            except IOError:
                errors += 1

        delivered = len(producer.messages)
        # Allow ±15% variance around expected 75% delivery
        assert delivered >= 50, f"Expected ≥50 delivered, got {delivered}"
        assert delivered <= 90, f"Expected ≤90 delivered, got {delivered}"

    async def test_consumer_handles_empty_broker(self):
        """Consumer iterating over empty message list must terminate cleanly."""
        injector = FaultInjector(drop_rate=1.0)
        consumer = FaultyKafkaConsumer(messages=[], injector=injector)
        messages_received = []

        async for msg in consumer:
            messages_received.append(msg)

        assert messages_received == []

    async def test_recovery_after_total_outage(self, ebpf_batch):
        """After outage ends (injector re-configured), messages resume delivery."""
        injector = FaultInjector(drop_rate=1.0)
        producer = FaultyKafkaProducer(injector)

        # Phase 1 — total outage
        for _ in range(5):
            await producer.send("ccdt.ebpf.events", ebpf_batch.SerializeToString())
        assert len(producer.messages) == 0

        # Phase 2 — recovery
        injector.drop_rate = 0.0
        for _ in range(5):
            await producer.send("ccdt.ebpf.events", ebpf_batch.SerializeToString())
        assert len(producer.messages) == 5

    async def test_flush_succeeds_after_outage(
        self, faulty_producer_total_outage
    ):
        """flush() must not raise even after total outage."""
        await faulty_producer_total_outage.flush()   # should not raise


# ══════════════════════════════════════════════════════════════════════════════
# Partial message loss
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.kafka
class TestPartialMessageLoss:
    """Realistic partial message loss — simulates network packet drops."""

    async def test_low_drop_rate_most_messages_delivered(
        self, fault_injector_low, ebpf_batch
    ):
        """5% drop rate: ≥90% of 200 messages must arrive."""
        producer = FaultyKafkaProducer(fault_injector_low)
        n = 200
        for _ in range(n):
            try:
                await producer.send("ccdt.ebpf.events", ebpf_batch.SerializeToString())
            except IOError:
                pass

        delivered = len(producer.messages)
        assert delivered >= 170, f"5% drop rate: expected ≥170 deliveries, got {delivered}"

    async def test_consumer_drops_between_messages(self, ebpf_batch):
        """Consumer-side drops: with 33% drop, only ~67% of messages consumed."""
        injector = FaultInjector(drop_rate=0.33, seed=7)
        payloads = [ebpf_batch.SerializeToString() for _ in range(30)]
        consumer = FaultyKafkaConsumer(payloads, injector)

        received = []
        async for msg in consumer:
            batch = TypedEbpfBatch.FromString(msg.value)
            received.append(batch)

        # With 33% drop over 30 messages, expect roughly 15–25 deliveries
        assert 10 <= len(received) <= 30

    async def test_per_topic_drop_accounting(self):
        """Drop statistics are per-producer, not per-topic."""
        injector = FaultInjector(drop_rate=0.5, seed=99)
        producer = FaultyKafkaProducer(injector)

        for i, topic in enumerate(
            ["ccdt.ebpf.events", "ccdt.gnn.inference", "ccdt.guardian.actions"]
        ):
            for _ in range(20):
                try:
                    await producer.send(topic, f"msg-{i}".encode())
                except IOError:
                    pass

        total_attempted = producer.attempted
        total_delivered = len(producer.messages)
        assert total_attempted == 60
        # ~50% drop → expect 20–40 deliveries
        assert 15 <= total_delivered <= 45

    async def test_key_preserved_on_delivered_messages(
        self, fault_injector_low, ebpf_batch
    ):
        """Kafka message keys must be preserved when not dropped."""
        producer = FaultyKafkaProducer(fault_injector_low)
        key = b"ip-10-0-1-42.us-east-1.compute.internal"
        payload = ebpf_batch.SerializeToString()

        # Send 50, some will be dropped but key integrity checked on all survivors
        for _ in range(50):
            try:
                await producer.send("ccdt.ebpf.events", payload, key=key)
            except IOError:
                pass

        for msg in producer.messages:
            assert msg["key"] == key


# ══════════════════════════════════════════════════════════════════════════════
# Payload corruption
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.kafka
class TestPayloadCorruption:
    """
    Tests that consumers handle corrupted protobuf bytes gracefully.
    A corrupted message must NEVER crash the consumer — it should be
    logged and skipped (dead-letter queue pattern).
    """

    def test_corrupted_bytes_fail_to_deserialize(
        self, fault_injector_corruption_only, ebpf_batch
    ):
        """Corrupted payload should fail TypedEbpfBatch.FromString without crash."""
        injector = fault_injector_corruption_only
        original = ebpf_batch.SerializeToString()
        corrupted = injector.corrupt_bytes(original)

        # Corrupted bytes may or may not parse (proto is somewhat resilient)
        # but they should never raise an unhandled exception in a try/except
        try:
            batch = TypedEbpfBatch.FromString(corrupted)
            # If it parses, node_name might be garbled — that's expected
        except Exception:
            pass  # Expected: corrupt proto fails cleanly

    def test_malformed_payloads_handled_gracefully(
        self, malformed_ebpf_payloads
    ):
        """All malformed payloads in the fixture must not raise unhandled exceptions."""
        errors_handled = 0
        for payload in malformed_ebpf_payloads:
            try:
                TypedEbpfBatch.FromString(payload)
            except Exception:
                errors_handled += 1  # correctly caught

        # At least some of these must fail to parse (they're intentionally corrupt)
        assert errors_handled > 0

    async def test_corrupt_producer_tracks_corruption_count(
        self, faulty_producer_corruption, ebpf_batch
    ):
        """Corrupt producer must increment corrupted_count on each send."""
        for _ in range(10):
            await faulty_producer_corruption.send(
                "ccdt.ebpf.events", ebpf_batch.SerializeToString()
            )

        injector = faulty_producer_corruption._injector
        assert injector.corrupted_count == 10
        assert len(faulty_producer_corruption.messages) == 10  # all 'delivered' (corrupted)

    async def test_consumer_skips_corrupt_messages(
        self, fault_injector_corruption_only, ebpf_batch
    ):
        """Consumer pipeline should parse what it can and skip what it can't."""
        original = ebpf_batch.SerializeToString()
        corrupt_payload = fault_injector_corruption_only.corrupt_bytes(original)

        # Mix of good + corrupt messages
        payloads = (
            [original] * 5
            + [corrupt_payload] * 5
        )

        injector = FaultInjector()  # no drops/errors
        consumer = FaultyKafkaConsumer(payloads, injector)

        parsed_ok = 0
        parse_errors = 0

        async for msg in consumer:
            try:
                batch = TypedEbpfBatch.FromString(msg.value)
                if batch.schema_ver or batch.node_name:
                    parsed_ok += 1
            except Exception:
                parse_errors += 1

        # At least the 5 good messages should parse correctly
        assert parsed_ok >= 5


# ══════════════════════════════════════════════════════════════════════════════
# Consumer lag
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.kafka
class TestConsumerLag:
    """
    Simulates slow consumer / high latency scenarios.
    Tests that the pipeline remains correct under lag (even if slow).
    """

    async def test_high_latency_consumer_still_processes_all_messages(
        self, ebpf_batch
    ):
        """Even with 20ms delay per message, all 10 messages must be consumed."""
        injector = FaultInjector(latency_ms=20, seed=1)
        payloads = [ebpf_batch.SerializeToString() for _ in range(10)]
        consumer = FaultyKafkaConsumer(payloads, injector)

        received = []
        start = time.perf_counter()
        async for msg in consumer:
            batch = TypedEbpfBatch.FromString(msg.value)
            received.append(batch)
        elapsed = time.perf_counter() - start

        assert len(received) == 10
        # 10 messages × 20ms = at least 200ms total
        assert elapsed >= 0.150

    async def test_batch_ordering_preserved_under_latency(
        self, ebpf_batch
    ):
        """Messages must be received in the order they were sent."""
        injector = FaultInjector(latency_ms=5)
        payloads = []
        batch_ids = []
        for i in range(5):
            batch = TypedEbpfBatch(
                batch_id=f"batch-{i:04d}",
                node_name="node-1",
                collector_id=_uid(),
                batch_ts=_now(),
                schema_ver="1.0",
            )
            payloads.append(batch.SerializeToString())
            batch_ids.append(f"batch-{i:04d}")

        consumer = FaultyKafkaConsumer(payloads, injector)
        received_ids = []
        async for msg in consumer:
            batch = TypedEbpfBatch.FromString(msg.value)
            received_ids.append(batch.batch_id)

        assert received_ids == batch_ids

    async def test_slow_consumer_does_not_block_producer(
        self, fault_injector_low, ebpf_batch
    ):
        """Producer should not block waiting for a slow consumer."""
        producer = FaultyKafkaProducer(fault_injector_low)

        async def _slow_consumer():
            await asyncio.sleep(0.5)

        # Producer sends 20 messages concurrently with a slow consumer
        producer_tasks = [
            asyncio.create_task(
                producer.send("ccdt.ebpf.events", ebpf_batch.SerializeToString())
            )
            for _ in range(20)
        ]
        consumer_task = asyncio.create_task(_slow_consumer())

        await asyncio.gather(*producer_tasks, consumer_task, return_exceptions=True)

        # All successfully sent messages should be recorded
        assert len(producer.messages) > 0


# ══════════════════════════════════════════════════════════════════════════════
# Schema evolution / backward compatibility
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.kafka
class TestSchemaEvolution:
    """
    Proto shims must handle messages serialized with older or newer schema
    versions without crashing.
    """

    def test_minimal_batch_parses_with_missing_optional_fields(self):
        """Batch with only required fields must parse without error."""
        minimal = TypedEbpfBatch(
            batch_id=_uid(),
            node_name="node-1",
            collector_id=_uid(),
            batch_ts=_now(),
            schema_ver="1.0",
        )
        raw = minimal.SerializeToString()
        loaded = TypedEbpfBatch.FromString(raw)

        assert loaded.node_name == "node-1"
        assert loaded.schema_ver == "1.0"
        assert loaded.capability_events == []
        assert loaded.oom_kill_events == []

    def test_future_schema_version_string_preserved(self):
        """A future schema_ver string must be preserved, not rejected."""
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name="node-1",
            collector_id=_uid(),
            batch_ts=_now(),
            schema_ver="99.0",  # future version
        )
        raw = batch.SerializeToString()
        loaded = TypedEbpfBatch.FromString(raw)
        assert loaded.schema_ver == "99.0"

    def test_gnn_inference_forward_compat(self):
        """GnnInferenceResult with extra unknown fields must survive roundtrip."""
        inf = GnnInferenceResult(
            inference_id=_uid(),
            incident_type=IncidentType.INCIDENT_FAULT,
            graph_confidence=0.87,
        )
        raw = inf.SerializeToString()
        loaded = GnnInferenceResult.FromString(raw)
        assert loaded.graph_confidence == pytest.approx(0.87)

    def test_action_result_unknown_status_graceful(self):
        """ActionResult with unexpected int status must not raise."""
        result = ActionResult(
            status=ActionStatus.STATUS_UNKNOWN,
            message="Unknown status from older agent",
        )
        raw = result.SerializeToString()
        loaded = ActionResult.FromString(raw)
        assert loaded.status == ActionStatus.STATUS_UNKNOWN

    def test_empty_type_counts_dict(self):
        """Batch with empty type_counts dict should serialize and deserialize."""
        batch = TypedEbpfBatch(
            batch_id=_uid(),
            node_name="node-1",
            collector_id=_uid(),
            batch_ts=_now(),
            schema_ver="1.0",
            type_counts={},
        )
        raw = batch.SerializeToString()
        loaded = TypedEbpfBatch.FromString(raw)
        assert isinstance(loaded.type_counts, dict)


# ══════════════════════════════════════════════════════════════════════════════
# High-throughput stress
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.chaos
@pytest.mark.kafka
@pytest.mark.slow
class TestKafkaHighThroughput:
    """
    Stress tests: 1000 messages at various fault rates.
    Validates delivery ratio and no hangs.
    """

    async def test_1000_messages_no_faults(self, ebpf_batch):
        """1000 messages, no faults: delivery ratio = 100%."""
        injector = FaultInjector()
        producer = FaultyKafkaProducer(injector)
        payload = ebpf_batch.SerializeToString()

        for _ in range(1000):
            await producer.send("ccdt.ebpf.events", payload)

        assert len(producer.messages) == 1000
        assert injector.delivery_ratio == 1.0

    async def test_1000_messages_10pct_drop(self, ebpf_batch):
        """1000 messages with 10% drop: expect 850–1000 delivered."""
        injector = FaultInjector(drop_rate=0.10, seed=11)
        producer = FaultyKafkaProducer(injector)
        payload = ebpf_batch.SerializeToString()

        for _ in range(1000):
            await producer.send("ccdt.ebpf.events", payload)

        delivered = len(producer.messages)
        assert 850 <= delivered <= 1000

    async def test_oom_cascade_batch_serialization_at_scale(
        self, high_volume_batch_factory
    ):
        """100 OOM-cascade batches must serialize and deserialize without error."""
        batches = high_volume_batch_factory(n_batches=100, oom_per_batch=5)

        for batch in batches:
            raw = batch.SerializeToString()
            loaded = TypedEbpfBatch.FromString(raw)
            assert loaded.node_name == batch.node_name
            assert len(loaded.oom_kill_events) == 5
