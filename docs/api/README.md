# CCDT API Reference

This directory documents all CCDT external and internal APIs.

| Document | Description |
|---|---|
| [openapi.yaml](openapi.yaml) | OpenAPI 3.1 specification for the REST API Gateway |
| [grpc-services.md](grpc-services.md) | gRPC service definitions for Layer-2 and Layer-3 |
| [kafka-topics.md](kafka-topics.md) | Kafka topic schemas, partitioning, and consumer groups |
| [websocket-protocol.md](websocket-protocol.md) | WebSocket protocol for real-time dashboard updates |

## Base URL

| Environment | URL |
|---|---|
| Production | `https://ccdt.internal.yourcompany.com/api/v1` |
| Staging | `https://ccdt-staging.internal.yourcompany.com/api/v1` |
| Local dev | `http://localhost:8000/api/v1` |

## Authentication

All API requests require a JWT Bearer token in the `Authorization` header:

```
Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...
```

Tokens are issued by your IdP. Obtain one with:
```bash
# Using OIDC device flow (for CLI)
ccdt auth login

# Returns a short-lived access token (15 minutes) and refresh token
```

## Rate Limits

| Endpoint group | Rate limit |
|---|---|
| `/api/v1/chat` | 60 requests/minute per operator |
| `/api/v1/actions/*` | 20 requests/minute per operator |
| `/api/v1/topology` | 30 requests/minute |
| All others | 120 requests/minute |

Rate limit responses return `HTTP 429` with:
```json
{"detail": "Rate limit exceeded", "retry_after_seconds": 12}
```
