"""
CCDT Shared Library
═══════════════════════════════════════════════════════════════════════════════
Shared Protocol Buffers, JSON Schemas, and utilities used across all
four CCDT layers.

Package structure
-----------------
ccdt/shared/
├── proto/              Protocol Buffer definitions (.proto)
│   ├── events.proto    Layer-1 eBPF telemetry events
│   ├── graph.proto     Layer-2 GNN topology + inference results
│   ├── actions.proto   Layer-3 Guardian remediation actions
│   └── copilot.proto   Layer-4 Co-Pilot conversation interface
│   └── generated/      Auto-generated Python pb2 stubs (run `make proto`)
├── schemas/            JSON Schema definitions for Kafka message validation
│   ├── ebpf_event.schema.json
│   ├── gnn_inference.schema.json
│   ├── guardian_action.schema.json
│   ├── copilot_session.schema.json
│   └── incident.schema.json
└── utils/
    ├── logging.py      Structured JSON logging with context propagation
    └── metrics.py      Centralized Prometheus metrics registry

Quick start
-----------
    # Install
    pip install -e ccdt/shared

    # Logging
    from ccdt.shared.utils.logging import get_logger, configure_logging
    configure_logging(level="INFO", service="layer2-cognitive")
    log = get_logger(__name__)
    log.info("hello", key="value")

    # Metrics
    from ccdt.shared.utils.metrics import LAYER2_GNN_INFERENCES, start_metrics_server
    LAYER2_GNN_INFERENCES.labels(incident_type="FAULT", is_heartbeat="false").inc()
    start_metrics_server(port=9090)

    # Protobuf (after running `make proto`)
    from ccdt.shared.proto.generated import events_pb2
    batch = events_pb2.TypedEbpfBatch()
    batch.batch_id = "abc-123"
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
