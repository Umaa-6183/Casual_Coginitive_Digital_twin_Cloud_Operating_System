# CCDT WebSocket Protocol

The CCDT Dashboard uses a WebSocket connection for real-time updates. The WebSocket endpoint is exposed by the API Gateway.

## Connection

```
ws://ccdt.internal.yourcompany.com/ws/stream?token=<jwt_token>
```

Or with TLS:
```
wss://ccdt.internal.yourcompany.com/ws/stream?token=<jwt_token>
```

Authentication is via a short-lived JWT token passed as a query parameter. The token must be renewed before expiry — the server sends a `token_expiry_warning` event 60 seconds before expiry.

---

## Message Format

All WebSocket messages are JSON objects with an `event` field that determines the payload shape.

### Server → Client Events

#### `topology_update`
Sent every 5 seconds with the current cluster topology.
```json
{
  "event": "topology_update",
  "ts": "2024-12-20T14:23:45Z",
  "data": {
    "snapshot_id": "...",
    "node_count": 47,
    "incident_type": "FAULT",
    "severity": "high",
    "root_cause_node": "payment-svc",
    "nodes": [
      {"name": "payment-svc", "node_class": "fault", "class_confidence": 0.91},
      {"name": "api-gateway", "node_class": "healthy", "class_confidence": 0.97}
    ]
  }
}
```

#### `incident_detected`
Sent when a new incident is detected (GNN confidence ≥ 0.70).
```json
{
  "event": "incident_detected",
  "ts": "2024-12-20T14:23:46Z",
  "data": {
    "incident_id": "...",
    "severity": "high",
    "incident_type": "FAULT",
    "root_cause_service": "payment-svc",
    "graph_confidence": 0.88,
    "nl_summary": "OOM kills in payment-svc causing cascading latency.",
    "blast_radius_count": 3
  }
}
```

#### `action_proposed`
Sent when Guardian proposes a remediation action (before execution).
```json
{
  "event": "action_proposed",
  "ts": "2024-12-20T14:23:47Z",
  "data": {
    "audit_id": "...",
    "action_name": "restart_pod",
    "target": "production/payment-svc-pod-abc123",
    "risk_score": 0.12,
    "risk_category": "VERY_LOW",
    "opa_approved": true,
    "requires_approval": false,
    "autonomy_mode": "supervised"
  }
}
```

#### `action_completed`
Sent when a Guardian action completes (success or failure).
```json
{
  "event": "action_completed",
  "ts": "2024-12-20T14:23:50Z",
  "data": {
    "audit_id": "...",
    "status": "SUCCEEDED",
    "action_name": "restart_pod",
    "execution_duration_ms": 1250,
    "verified_effect": true,
    "post_action_health": 0.94
  }
}
```

#### `approval_required`
Sent when an action requires human approval before execution.
```json
{
  "event": "approval_required",
  "ts": "2024-12-20T14:23:47Z",
  "data": {
    "audit_id": "...",
    "action_name": "drain_node",
    "target": "production/ip-10-0-1-42",
    "risk_score": 0.65,
    "risk_category": "HIGH",
    "expires_at": "2024-12-20T14:28:47Z"
  }
}
```

#### `incident_resolved`
Sent when an incident transitions to RESOLVED state.
```json
{
  "event": "incident_resolved",
  "ts": "2024-12-20T14:25:00Z",
  "data": {
    "incident_id": "...",
    "resolved_at": "2024-12-20T14:25:00Z",
    "mttr_seconds": 75,
    "resolution_action": "restart_pod"
  }
}
```

#### `copilot_stream_token`
Sent during a streaming Co-Pilot response (token-by-token).
```json
{"event": "copilot_stream_token", "session_id": "...", "delta_text": "Based on", "seq_num": 1}
{"event": "copilot_stream_token", "session_id": "...", "delta_text": " the GNN", "seq_num": 2}
{"event": "copilot_stream_complete", "session_id": "...", "input_tokens": 450, "output_tokens": 120}
```

#### `token_expiry_warning`
Sent 60 seconds before JWT token expiry.
```json
{"event": "token_expiry_warning", "expires_at": "2024-12-20T14:30:00Z", "remaining_seconds": 60}
```

### Client → Server Messages

#### `ping`
```json
{"event": "ping", "ts": "2024-12-20T14:23:45Z"}
```

#### `approve_action`
Approve or deny a pending action.
```json
{"event": "approve_action", "audit_id": "...", "decision": "approve", "reason": "Looks safe"}
```

#### `subscribe`
Subscribe to specific event types (default: all).
```json
{"event": "subscribe", "event_types": ["incident_detected", "action_completed"]}
```

---

## Connection Management

- **Heartbeat**: Client should send `ping` every 30s; server responds with `pong`
- **Reconnection**: Client should reconnect with exponential backoff (1s, 2s, 4s, ... max 60s)
- **Message ordering**: Events within the same `incident_id` are guaranteed to arrive in order
- **Missed events**: On reconnect, client should call `GET /api/v1/incidents?since=<last_seen_ts>` to catch up
