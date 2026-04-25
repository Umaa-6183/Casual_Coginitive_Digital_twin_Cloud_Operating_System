# CCDT gRPC Services

CCDT uses gRPC for its highest-performance paths. Both Layer-2 and Layer-3 expose gRPC endpoints in addition to REST.

## Layer-2: GnnInferenceService (port 8001)

```protobuf
service GnnInferenceService {
  // Run inference on the current cluster topology
  rpc GetInference(GetInferenceRequest) returns (GnnInferenceResult);
  
  // Stream inference results as they are produced
  rpc StreamInference(StreamInferenceRequest) returns (stream GnnInferenceResult);
  
  // Get the current topology snapshot
  rpc GetTopology(GetTopologyRequest) returns (TopologySnapshot);
  
  // Run counterfactual analysis: "What if node X were healthy?"
  rpc ComputeCounterfactual(CounterfactualQuery) returns (CounterfactualResponse);
}
```

### Example: GetInference

```python
import grpc
from ccdt.shared.proto.generated import graph_pb2, graph_pb2_grpc

channel = grpc.insecure_channel("layer2-cognitive:8001")
stub = graph_pb2_grpc.GnnInferenceServiceStub(channel)

result = stub.GetInference(graph_pb2.GetInferenceRequest(
    request_id="req-001",
    include_counterfactuals=True,
    max_causal_chain_depth=5,
))
print(f"Incident: {result.incident_type}, Confidence: {result.graph_confidence:.1%}")
```

### Example: StreamInference

```python
for inference in stub.StreamInference(graph_pb2.StreamInferenceRequest(
    min_confidence=0.70,
    skip_heartbeats=True,
)):
    print(f"[{inference.timestamp}] {inference.incident_type}: {inference.nl_summary()}")
```

---

## Layer-3: GuardianService (port 8002)

```protobuf
service GuardianService {
  // Run Ghost Preview simulation (no K8s API calls)
  rpc PreviewAction(ActionRequest) returns (GhostSimulationResult);
  
  // Execute action (full pipeline: Ghost → OPA → K8s)
  rpc ExecuteAction(ActionRequest) returns (ActionResult);
  
  // Submit human approval for a pending action
  rpc ApproveAction(ApprovalRequest) returns (ApprovalResponse);
  
  // Get recent action history
  rpc GetActionHistory(ActionHistoryRequest) returns (ActionHistoryResponse);
  
  // Stream action results as they complete
  rpc StreamActions(StreamActionsRequest) returns (stream ActionResult);
}
```

### Example: PreviewAction

```python
import grpc
from ccdt.shared.proto.generated import actions_pb2, actions_pb2_grpc

channel = grpc.insecure_channel("layer3-guardian:8002")
stub = actions_pb2_grpc.GuardianServiceStub(channel)

ghost = stub.PreviewAction(actions_pb2.ActionRequest(
    action_name=actions_pb2.ActionName.ACTION_RESTART_POD,
    target_node_name="payment-svc-pod-abc123",
    target_namespace="production",
))
print(f"Risk: {ghost.risk_score:.2f} ({ghost.risk_category.name})")
print(f"OPA approved: {ghost.opa_approved}")
if ghost.opa_violations:
    print(f"Violations: {ghost.opa_violations}")
```

---

## Connection Configuration

### TLS (Production)
```python
# Using mTLS with SPIFFE certificates
creds = grpc.ssl_channel_credentials(
    root_certificates=open("/var/run/secrets/spiffe/ca.crt", "rb").read(),
    private_key=open("/var/run/secrets/spiffe/tls.key", "rb").read(),
    certificate_chain=open("/var/run/secrets/spiffe/tls.crt", "rb").read(),
)
channel = grpc.secure_channel("layer2-cognitive:8001", creds)
```

### Development (insecure)
```python
channel = grpc.insecure_channel("localhost:8001")
```

### Deadlines
Always set a deadline to prevent hung connections:
```python
result = stub.GetInference(request, timeout=5.0)  # 5-second deadline
```

---

## Error Codes

| gRPC Status | Meaning | CCDT Context |
|---|---|---|
| `OK` | Success | Normal response |
| `UNAVAILABLE` | Service down | Layer-2/3 pod not running |
| `DEADLINE_EXCEEDED` | Timeout | GNN inference > 5s (overloaded) |
| `FAILED_PRECONDITION` | Invalid state | OPA blocked the action |
| `RESOURCE_EXHAUSTED` | Rate limited | Too many concurrent requests |
| `INTERNAL` | Server error | Unexpected exception in service |
| `NOT_FOUND` | Resource missing | Inference ID not in cache |
