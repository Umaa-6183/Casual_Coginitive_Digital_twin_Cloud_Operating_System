"""Tests for CCDT JSON Schema validation helpers."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from ccdt.shared.schemas import (
    load_schema,
    SchemaValidator,
    validate_ebpf_event,
    validate_gnn_inference,
    validate_guardian_action,
    validate_copilot_session,
    validate_incident,
)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


class TestLoadSchema:
    def test_loads_all_schemas(self):
        for name in ("ebpf_event", "gnn_inference", "guardian_action",
                     "copilot_session", "incident"):
            schema = load_schema(name)
            assert "$schema" in schema
            assert "title" in schema

    def test_schema_cached(self):
        s1 = load_schema("ebpf_event")
        s2 = load_schema("ebpf_event")
        assert s1 is s2  # same object (cached)

    def test_unknown_schema_raises(self):
        with pytest.raises(ValueError, match="Unknown schema"):
            load_schema("nonexistent_schema")


class TestEbpfEventSchema:
    def _valid_batch(self) -> dict:
        return {
            "batch_id":     _uuid(),
            "node_name":    "ip-10-0-1-42.us-east-1.compute.internal",
            "collector_id": _uuid(),
            "batch_ts":     _ts(),
            "schema_ver":   "1.0",
        }

    def test_valid_minimal_batch(self):
        assert validate_ebpf_event(self._valid_batch()) is True

    def test_invalid_missing_required(self):
        batch = self._valid_batch()
        del batch["batch_id"]
        assert validate_ebpf_event(batch) is False

    def test_invalid_schema_ver(self):
        batch = self._valid_batch()
        batch["schema_ver"] = "2.0"
        assert validate_ebpf_event(batch) is False

    def test_valid_with_oom_events(self):
        batch = self._valid_batch()
        batch["oom_kill_events"] = [{
            "meta": {
                "kernel_ts_ns": 1234567890,
                "node_name": "node-1",
                "pid": 1234,
                "comm": "oom_test",
            },
            "victim_pid":   999,
            "victim_comm":  "victim",
        }]
        assert validate_ebpf_event(batch) is True

    def test_iter_errors_gives_detail(self):
        v = SchemaValidator("ebpf_event")
        bad = {"batch_id": _uuid()}   # missing required fields
        errors = list(v.iter_errors(bad))
        assert len(errors) > 0


class TestGnnInferenceSchema:
    def _valid_result(self) -> dict:
        return {
            "inference_id":    _uuid(),
            "timestamp":       _ts(),
            "incident_type":   "FAULT",
            "graph_confidence": 0.87,
            "schema_ver":      "1.0",
        }

    def test_valid_minimal(self):
        assert validate_gnn_inference(self._valid_result()) is True

    def test_invalid_incident_type(self):
        r = self._valid_result()
        r["incident_type"] = "EXPLOSION"
        assert validate_gnn_inference(r) is False

    def test_confidence_out_of_range(self):
        r = self._valid_result()
        r["graph_confidence"] = 1.5
        assert validate_gnn_inference(r) is False

    def test_valid_with_causal_chain(self):
        r = self._valid_result()
        r["causal_chain"] = [{
            "node_id":      "svc-1",
            "node_name":    "auth-svc",
            "causal_score": 0.85,
            "depth":        0,
        }]
        assert validate_gnn_inference(r) is True

    def test_valid_heartbeat(self):
        r = self._valid_result()
        r["incident_type"] = "NONE"
        r["is_heartbeat"] = True
        r["graph_confidence"] = 0.0
        assert validate_gnn_inference(r) is True


class TestGuardianActionSchema:
    def _valid_action(self) -> dict:
        return {
            "audit_id":    _uuid(),
            "status":      "SUCCEEDED",
            "requested_at": _ts(),
            "schema_ver":  "1.0",
            "request": {
                "request_id":       _uuid(),
                "action_name":      "RESTART_POD",
                "target_namespace": "default",
            },
        }

    def test_valid_minimal(self):
        assert validate_guardian_action(self._valid_action()) is True

    def test_invalid_status(self):
        a = self._valid_action()
        a["status"] = "FLYING"
        assert validate_guardian_action(a) is False

    def test_valid_with_ghost_result(self):
        a = self._valid_action()
        a["request"]["ghost_result"] = {
            "risk_score":   0.15,
            "risk_category": "LOW",
            "opa_approved": True,
            "confidence":   0.9,
        }
        assert validate_guardian_action(a) is True


class TestCopilotSessionSchema:
    def _valid_session(self) -> dict:
        return {
            "session_id":  _uuid(),
            "operator_id": "alice",
            "state":       "ACTIVE",
            "created_at":  _ts(),
            "schema_ver":  "1.0",
        }

    def test_valid_minimal(self):
        assert validate_copilot_session(self._valid_session()) is True

    def test_invalid_state(self):
        s = self._valid_session()
        s["state"] = "DREAMING"
        assert validate_copilot_session(s) is False

    def test_valid_with_history(self):
        s = self._valid_session()
        s["history"] = [{
            "message_id": _uuid(),
            "role":       "user",
            "content":    "What is wrong?",
            "created_at": _ts(),
        }]
        assert validate_copilot_session(s) is True

    def test_history_max_items_enforced(self):
        s = self._valid_session()
        # 41 messages should fail (maxItems: 40)
        s["history"] = [
            {"message_id": _uuid(), "role": "user"} for _ in range(41)
        ]
        assert validate_copilot_session(s) is False


class TestIncidentSchema:
    def _valid_incident(self) -> dict:
        return {
            "incident_id":   _uuid(),
            "detected_at":   _ts(),
            "state":         "ACTIVE",
            "severity":      "HIGH",
            "incident_type": "FAULT",
            "schema_ver":    "1.0",
        }

    def test_valid_minimal(self):
        assert validate_incident(self._valid_incident()) is True

    def test_invalid_severity(self):
        i = self._valid_incident()
        i["severity"] = "EXTREME"
        assert validate_incident(i) is False

    def test_invalid_state(self):
        i = self._valid_incident()
        i["state"] = "EXPLODING"
        assert validate_incident(i) is False

    def test_valid_with_timeline(self):
        i = self._valid_incident()
        i["timeline"] = [{
            "ts":          _ts(),
            "event_type":  "DETECTED",
            "description": "GNN detected FAULT at 87% confidence",
            "actor":       "gnn-inference",
        }]
        assert validate_incident(i) is True

    def test_valid_with_actions(self):
        i = self._valid_incident()
        i["actions_taken"] = [{
            "audit_id":    _uuid(),
            "action_name": "restart_pod",
            "status":      "SUCCEEDED",
        }]
        assert validate_incident(i) is True


class TestSchemaValidator:
    def test_validate_batch_returns_errors(self):
        v = SchemaValidator("gnn_inference")
        results = v.validate_batch([
            # valid
            {"inference_id": _uuid(), "timestamp": _ts(),
             "incident_type": "NONE", "graph_confidence": 0.0, "schema_ver": "1.0"},
            # invalid — missing timestamp
            {"inference_id": _uuid(), "incident_type": "FAULT", "graph_confidence": 0.5,
             "schema_ver": "1.0"},
        ])
        assert len(results) > 0
        assert any("[1]" in e for e in results)
