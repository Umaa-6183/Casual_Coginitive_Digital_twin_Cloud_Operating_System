"""
CCDT Layer-4 Co-Pilot — Real-Time Sync with UI & Mock API
═══════════════════════════════════════════════════════════

Synchronizes:
1. Layer 1-4 incident state → Dashboard UI (WebSocket)
2. Critical/Warning status → Mock UI facade (HTTP/WS)
3. Recovery actions → Live visual feedback
4. Auto-refresh triggers on state changes

Target: <200ms propagation delay from incident → UI update
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger("ccdt.copilot.realtime_sync")


@dataclass
class IncidentUpdate:
    """Real-time incident state update."""
    incident_id: str
    status: str  # "active", "resolving", "resolved"
    severity: str  # "critical", "warning", "healthy"
    root_cause: str
    confidence: float
    blast_radius: list[str]
    timestamp: str
    layer_source: int  # 1, 2, 3, or 4
    auto_recovered: bool = False
    recovery_action: Optional[str] = None
    mttr_seconds: Optional[float] = None


@dataclass
class NodeHealthUpdate:
    """Individual node health status."""
    node_id: str
    status: str  # "healthy", "warning", "critical"
    cpu_percent: float
    memory_percent: float
    error_rate: float
    last_restart: Optional[str] = None
    incident_count: int = 0


class RealTimeSyncManager:
    """
    Manages real-time synchronization between CCDT layers and UIs.

    Responsibilities:
    - WebSocket broadcast to Dashboard UI (port 3000)
    - HTTP updates to Mock UI facade (port 8088)
    - State change detection and notification
    - Auto-refresh trigger on critical events
    """

    def __init__(self):
        self._ws_connections: list[Any] = []  # WebSocket connections
        self._last_broadcast: dict[str, float] = {}  # Throttle broadcasts
        self._incident_history: dict[str, IncidentUpdate] = {}
        self._node_health_cache: dict[str, NodeHealthUpdate] = {}

        # External service URLs
        self._mock_ui_url = os.getenv("MOCK_UI_URL", "http://demo-nginx:8088")
        self._api_gateway_url = os.getenv("API_GATEWAY_URL", "http://api-gateway:8000")
        self._dashboard_url = os.getenv("DASHBOARD_URL", "http://dashboard:3000")

        # Metrics
        self._broadcasts_sent = 0
        self._ws_errors = 0
        self._http_updates_sent = 0

        logger.info("Real-time sync manager initialized")

    def add_websocket(self, ws: Any) -> None:
        """Register a new WebSocket connection."""
        self._ws_connections.append(ws)
        logger.info("WebSocket connected (total: %d)", len(self._ws_connections))

    def remove_websocket(self, ws: Any) -> None:
        """Unregister a WebSocket connection."""
        if ws in self._ws_connections:
            self._ws_connections.remove(ws)
            logger.info("WebSocket disconnected (total: %d)", len(self._ws_connections))

    async def broadcast_incident_update(self, incident: IncidentUpdate) -> None:
        """
        Broadcast incident update to all connected clients.

        Targets:
        - WebSocket clients (Dashboard UI)
        - Mock UI facade (HTTP POST)
        - API Gateway (for persistence)
        """
        # Throttle: max 1 broadcast per 500ms per incident
        throttle_key = f"incident:{incident.incident_id}"
        now = time.time()
        if now - self._last_broadcast.get(throttle_key, 0) < 0.5:
            return

        self._last_broadcast[throttle_key] = now

        # Store in history
        self._incident_history[incident.incident_id] = incident

        # Build broadcast message
        message = {
            "type": "incident_update",
            "incident": {
                "id": incident.incident_id,
                "status": incident.status,
                "severity": incident.severity,
                "root_cause": incident.root_cause,
                "confidence": round(incident.confidence, 3),
                "blast_radius": incident.blast_radius,
                "auto_recovered": incident.auto_recovered,
                "recovery_action": incident.recovery_action,
                "mttr_seconds": incident.mttr_seconds,
                "timestamp": incident.timestamp,
                "layer_source": incident.layer_source,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 1. WebSocket broadcast to Dashboard
        await self._ws_broadcast(message)

        # 2. HTTP update to Mock UI facade
        await self._update_mock_ui(incident)

        # 3. Trigger auto-refresh on critical events
        if incident.severity == "critical" and incident.status == "active":
            await self._trigger_ui_refresh()

        logger.info(
            "📡 Broadcast: %s (%s) to %d WS clients",
            incident.incident_id, incident.status, len(self._ws_connections)
        )

    async def broadcast_node_health(self, node: NodeHealthUpdate) -> None:
        """Broadcast individual node health update."""
        # Throttle: max 1 broadcast per 2s per node
        throttle_key = f"node:{node.node_id}"
        now = time.time()
        if now - self._last_broadcast.get(throttle_key, 0) < 2.0:
            return

        self._last_broadcast[throttle_key] = now
        self._node_health_cache[node.node_id] = node

        message = {
            "type": "node_health",
            "node": {
                "id": node.node_id,
                "status": node.status,
                "cpu": round(node.cpu_percent, 1),
                "memory": round(node.memory_percent, 1),
                "error_rate": round(node.error_rate, 3),
                "last_restart": node.last_restart,
                "incident_count": node.incident_count,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await self._ws_broadcast(message)

    async def broadcast_recovery_action(self, action: dict) -> None:
        """Broadcast recovery action to UI."""
        message = {
            "type": "recovery_action",
            "action": action,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await self._ws_broadcast(message)
        await self._update_mock_ui_action(action)

    async def _ws_broadcast(self, message: dict) -> None:
        """Send message to all connected WebSocket clients."""
        if not self._ws_connections:
            return

        message_json = json.dumps(message)
        dead_connections = []

        for ws in self._ws_connections:
            try:
                await ws.send_text(message_json)
                self._broadcasts_sent += 1
            except Exception as exc:
                logger.debug("WebSocket send failed: %s", exc)
                dead_connections.append(ws)
                self._ws_errors += 1

        # Remove dead connections
        for ws in dead_connections:
            self.remove_websocket(ws)

    async def _update_mock_ui(self, incident: IncidentUpdate) -> None:
        """
        Send incident update to Mock UI facade.

        The Mock UI should display:
        - Visual alert when critical incident starts
        - Progress indicator during recovery
        - Success message when resolved
        """
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                # POST to Mock UI status endpoint
                response = await client.post(
                    f"{self._mock_ui_url}/api/incident-status",
                    json={
                        "incident_id": incident.incident_id,
                        "status": incident.status,
                        "severity": incident.severity,
                        "root_cause": incident.root_cause,
                        "message": self._build_ui_message(incident),
                        "auto_recovered": incident.auto_recovered,
                        "timestamp": incident.timestamp,
                    },
                    headers={"X-CCDT-Source": "layer4-copilot"},
                )

                if response.status_code == 200:
                    self._http_updates_sent += 1
                    logger.debug("Mock UI updated: %s", incident.incident_id)
                else:
                    logger.debug(
                        "Mock UI update failed: HTTP %d", response.status_code
                    )

        except Exception as exc:
            logger.debug("Mock UI unreachable: %s", exc)

    async def _update_mock_ui_action(self, action: dict) -> None:
        """Send recovery action to Mock UI."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(
                    f"{self._mock_ui_url}/api/recovery-action",
                    json=action,
                    headers={"X-CCDT-Source": "layer4-copilot"},
                )
        except Exception as exc:
            logger.debug("Mock UI action update failed: %s", exc)

    async def _trigger_ui_refresh(self) -> None:
        """Trigger UI auto-refresh on critical events."""
        message = {
            "type": "ui_refresh",
            "reason": "critical_incident",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._ws_broadcast(message)

    def _build_ui_message(self, incident: IncidentUpdate) -> str:
        """Build user-friendly message for Mock UI."""
        if incident.status == "active":
            if incident.severity == "critical":
                return f"🔴 CRITICAL: {incident.root_cause} is experiencing issues"
            elif incident.severity == "warning":
                return f"⚠️ WARNING: {incident.root_cause} needs attention"
            else:
                return f"ℹ️ {incident.root_cause} status update"

        elif incident.status == "resolving":
            action = incident.recovery_action or "recovery"
            return f"🔄 Fixing {incident.root_cause} via {action}..."

        elif incident.status == "resolved":
            mttr = incident.mttr_seconds or 0
            auto = "automatically " if incident.auto_recovered else ""
            return f"✅ {incident.root_cause} {auto}recovered in {mttr:.1f}s"

        return f"Status: {incident.status}"

    async def sync_topology_update(self, topology: dict) -> None:
        """
        Sync topology update from Layer-2 GNN to all UIs.

        Called when GNN inference detects topology changes.
        """
        nodes = topology.get("nodes", [])

        # Extract node health
        for node_data in nodes:
            node = NodeHealthUpdate(
                node_id=node_data.get("id", "unknown"),
                status=node_data.get("status", "healthy"),
                cpu_percent=node_data.get("cpu", 0),
                memory_percent=node_data.get("mem", 0),
                error_rate=node_data.get("error_rate", 0),
                last_restart=node_data.get("last_restart"),
                incident_count=node_data.get("incident_count", 0),
            )
            await self.broadcast_node_health(node)

        # Full topology broadcast (throttled)
        throttle_key = "topology:full"
        now = time.time()
        if now - self._last_broadcast.get(throttle_key, 0) < 5.0:
            return

        self._last_broadcast[throttle_key] = now

        message = {
            "type": "topology_update",
            "topology": topology,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._ws_broadcast(message)

    async def sync_guardian_action(self, action_result: dict) -> None:
        """Sync Guardian action result to UIs."""
        await self.broadcast_recovery_action({
            "action_type": action_result.get("action_type", "unknown"),
            "target_node": action_result.get("target_node", "unknown"),
            "status": action_result.get("status", "unknown"),
            "risk_score": action_result.get("risk_score", 0),
            "mttr_delta": action_result.get("mttr_delta_seconds", 0),
            "opa_approved": action_result.get("opa_approved", False),
            "timestamp": action_result.get("timestamp", datetime.now(timezone.utc).isoformat()),
        })

    def get_stats(self) -> dict:
        """Get sync statistics."""
        return {
            "ws_connections": len(self._ws_connections),
            "broadcasts_sent": self._broadcasts_sent,
            "ws_errors": self._ws_errors,
            "http_updates_sent": self._http_updates_sent,
            "incidents_tracked": len(self._incident_history),
            "nodes_tracked": len(self._node_health_cache),
        }

    async def health_check(self) -> dict:
        """Check connectivity to all external services."""
        results = {}

        async def check_service(name: str, url: str) -> tuple[str, bool]:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"{url}/health", follow_redirects=True)
                    return (name, response.status_code == 200)
            except:
                return (name, False)

        services = [
            ("mock_ui", self._mock_ui_url),
            ("api_gateway", self._api_gateway_url),
            ("dashboard", self._dashboard_url),
        ]

        check_results = await asyncio.gather(
            *[check_service(name, url) for name, url in services],
            return_exceptions=True
        )

        for result in check_results:
            if isinstance(result, tuple):
                name, healthy = result
                results[name] = healthy
            else:
                logger.debug("Health check error: %s", result)

        return {
            "services": results,
            "ws_connections": len(self._ws_connections),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# Global singleton instance
_sync_manager: Optional[RealTimeSyncManager] = None


def get_sync_manager() -> RealTimeSyncManager:
    """Get or create the global sync manager instance."""
    global _sync_manager
    if _sync_manager is None:
        _sync_manager = RealTimeSyncManager()
    return _sync_manager
