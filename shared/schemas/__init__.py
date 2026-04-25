"""
CCDT Shared Schemas — JSON Schema definitions and validator helpers.

    from ccdt.shared.schemas import validate_ebpf_event, validate_gnn_inference
    from ccdt.shared.schemas import load_schema, SchemaValidator

    validator = SchemaValidator("ebpf_event")
    validator.validate(event_dict)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Schema directory (same directory as this file)
_SCHEMA_DIR = Path(__file__).parent

_SCHEMA_FILES = {
    "ebpf_event":       "ebpf_event.schema.json",
    "gnn_inference":    "gnn_inference.schema.json",
    "guardian_action":  "guardian_action.schema.json",
    "copilot_session":  "copilot_session.schema.json",
    "incident":         "incident.schema.json",
}

_schema_cache: dict[str, dict] = {}


def load_schema(name: str) -> dict:
    """
    Load and cache a JSON Schema by name.

        schema = load_schema("ebpf_event")
    """
    if name not in _schema_cache:
        fname = _SCHEMA_FILES.get(name)
        if not fname:
            raise ValueError(
                f"Unknown schema '{name}'. "
                f"Available: {list(_SCHEMA_FILES.keys())}"
            )
        path = _SCHEMA_DIR / fname
        with open(path, encoding="utf-8") as f:
            _schema_cache[name] = json.load(f)
    return _schema_cache[name]


class SchemaValidator:
    """
    Validate Python dicts against a named CCDT JSON Schema.

    Requires: jsonschema >= 4.0
        pip install jsonschema

    Usage:
        from ccdt.shared.schemas import SchemaValidator

        v = SchemaValidator("ebpf_event")
        try:
            v.validate(event_dict)
        except SchemaValidationError as e:
            log.error("invalid event", errors=str(e))

    For high-throughput paths use validate_fast() which returns bool
    instead of raising.
    """

    def __init__(self, schema_name: str) -> None:
        self.schema_name = schema_name
        self._schema = load_schema(schema_name)
        self._validator = None

    def _get_validator(self):
        if self._validator is None:
            try:
                import jsonschema
                self._validator = jsonschema.Draft202012Validator(self._schema)
            except ImportError as e:
                raise ImportError(
                    "jsonschema is required for schema validation. "
                    "Install with: pip install jsonschema"
                ) from e
        return self._validator

    def validate(self, instance: Any) -> None:
        """Validate instance against schema. Raises jsonschema.ValidationError on failure."""
        self._get_validator().validate(instance)

    def validate_fast(self, instance: Any) -> bool:
        """Return True if valid, False if invalid. Never raises."""
        return self._get_validator().is_valid(instance)

    def iter_errors(self, instance: Any):
        """Iterate over all validation errors."""
        yield from self._get_validator().iter_errors(instance)

    def validate_batch(self, instances: list[Any]) -> list[str]:
        """
        Validate a list of instances. Returns list of error messages
        for invalid items (empty list = all valid).
        """
        errors = []
        for i, inst in enumerate(instances):
            for err in self.iter_errors(inst):
                errors.append(f"[{i}] {err.json_path}: {err.message}")
        return errors


def validate_ebpf_event(event: dict) -> bool:
    """Quick boolean validation of an eBPF event dict."""
    return SchemaValidator("ebpf_event").validate_fast(event)


def validate_gnn_inference(result: dict) -> bool:
    """Quick boolean validation of a GNN inference result dict."""
    return SchemaValidator("gnn_inference").validate_fast(result)


def validate_guardian_action(action: dict) -> bool:
    """Quick boolean validation of a Guardian action result dict."""
    return SchemaValidator("guardian_action").validate_fast(action)


def validate_copilot_session(session: dict) -> bool:
    """Quick boolean validation of a Co-Pilot session dict."""
    return SchemaValidator("copilot_session").validate_fast(session)


def validate_incident(incident: dict) -> bool:
    """Quick boolean validation of an incident record dict."""
    return SchemaValidator("incident").validate_fast(incident)


__all__ = [
    "load_schema",
    "SchemaValidator",
    "validate_ebpf_event",
    "validate_gnn_inference",
    "validate_guardian_action",
    "validate_copilot_session",
    "validate_incident",
    "_SCHEMA_FILES",
]
