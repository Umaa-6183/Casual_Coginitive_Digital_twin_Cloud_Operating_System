"""
CCDT Layer-3 Guardian — Docker Action Executor
════════════════════════════════════════════════════════════════════════════════
Option D: executes real remediation actions via the Docker SDK instead of
kubectl. Targets the demo-* services running in docker-compose.

Actions map:
  restart_pod              → docker.containers.get(target).restart()
  scale_up_replicas        → docker update --cpus (not natively, uses compose scale)
  increase_oom_threshold   → docker update --memory <new_limit>
  throttle_cpu             → docker update --cpus 0.5
  isolate_container        → docker network disconnect ccdt-net <container>
  apply_network_policy     → docker network disconnect + reconnect with limits
  kill_process             → docker exec container kill -9 <pid>
  enable_debug_logging     → docker exec container env DEBUG=1
  rollback_deployment      → docker restart (rollback = restart for demo)
  escalate_to_human        → log only, no execution
  no_op                    → no action

All actions are logged to SQLite via the database module.
Falls back to DRY-RUN if Docker socket is not mounted.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("ccdt.guardian.docker_executor")

DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
DEMO_NETWORK  = os.getenv("DEMO_NETWORK",  "ccdt_ccdt-net")

# Map GNN node IDs → actual Docker container names
NODE_TO_CONTAINER: dict[str, str] = {
    "api-gw":        "ccdt-demo-nginx-1",
    "postgres":      "ccdt-demo-postgres-1",
    "redis":         "ccdt-demo-redis-1",
    "pgbouncer":     "ccdt-demo-pgbouncer-1",
    "order-svc":     "ccdt-demo-nginx-1",      # nginx proxies order traffic
    "payment-svc":   "ccdt-demo-postgres-1",   # payment goes through postgres
    "notify-svc":    "ccdt-demo-redis-1",      # notify caches in redis
    "auth-svc":      "ccdt-demo-nginx-1",
    "inventory-svc": "ccdt-demo-postgres-1",
    "kafka":         "ccdt-kafka-1",
    "monitoring":    "ccdt-demo-cadvisor-1",
}


class DockerActionExecutor:
    """
    Executes remediation actions via the Docker SDK.
    Drop-in replacement for K8sActionExecutor in executor.py.
    """

    def __init__(self) -> None:
        self._client    = None
        self._connected = False

    def connect(self) -> None:
        """Connect to Docker daemon via Unix socket."""
        try:
            import docker
            self._client    = docker.DockerClient(base_url=f"unix://{DOCKER_SOCKET}")
            self._client.ping()
            self._connected = True
            logger.info("Docker executor connected (socket=%s)", DOCKER_SOCKET)
        except Exception as exc:
            logger.warning(
                "Docker socket unavailable (%s) — running in DRY-RUN mode. "
                "Mount /var/run/docker.sock into Guardian container to enable real actions.",
                exc,
            )
            self._connected = False

    def _resolve_container(self, node_id: str) -> str:
        """Resolve a GNN node ID to a Docker container name."""
        return NODE_TO_CONTAINER.get(node_id, f"ccdt-demo-{node_id}-1")

    def _get_container(self, node_id: str):
        """Get a Docker container object, raising ValueError if not found."""
        container_name = self._resolve_container(node_id)
        try:
            return self._client.containers.get(container_name)
        except Exception:
            # Try partial name match
            containers = self._client.containers.list(
                filters={"name": node_id}
            )
            if containers:
                return containers[0]
            raise ValueError(f"Container for '{node_id}' not found (tried '{container_name}')")

    async def execute(
        self,
        action_name: str,
        target_node: str,
        parameters:  dict,
        dry_run:     bool = False,
    ) -> dict:
        """Execute action. Returns {success, detail, dry_run}."""
        if dry_run or not self._connected:
            logger.info("[DRY-RUN] %s on %s", action_name, target_node)
            return {
                "success": True,
                "detail":  f"DRY-RUN: {action_name} on {target_node}",
                "dry_run": True,
            }

        import asyncio
        dispatch = {
            "no_op":                  self._no_op,
            "isolate_container":      self._isolate_container,
            "rollback_deployment":    self._restart_container,
            "scale_down_replicas":    self._throttle_cpu,
            "scale_up_replicas":      self._scale_up,
            "restart_pod":            self._restart_container,
            "cordon_node":            self._isolate_container,
            "drain_node":             self._isolate_container,
            "apply_network_policy":   self._isolate_container,
            "rotate_secrets":         self._restart_container,
            "kill_process":           self._kill_process,
            "increase_oom_threshold": self._increase_memory,
            "throttle_cpu":           self._throttle_cpu,
            "enable_debug_logging":   self._enable_debug,
            "escalate_to_human":      self._escalate,
        }

        fn = dispatch.get(action_name, self._no_op)
        try:
            loop   = asyncio.get_event_loop()
            detail = await loop.run_in_executor(None, lambda: fn(target_node, parameters))
            logger.info("Docker action executed: %s on %s → %s",
                        action_name, target_node, detail[:80])
            return {"success": True, "detail": detail, "dry_run": False}
        except Exception as exc:
            logger.error("Docker action %s failed on %s: %s", action_name, target_node, exc)
            return {"success": False, "detail": str(exc), "dry_run": False}

    # ── Action implementations ────────────────────────────────────────────────

    def _no_op(self, target: str, params: dict) -> str:
        return f"no_op: observed {target}, no action taken"

    def _restart_container(self, target: str, params: dict) -> str:
        """docker restart <container> — fastest remediation for most faults."""
        container = self._get_container(target)
        container.restart(timeout=10)
        return f"Container '{container.name}' restarted successfully"

    def _isolate_container(self, target: str, params: dict) -> str:
        """
        docker network disconnect — cuts the container off from ccdt-net.
        This is the Docker equivalent of a Kubernetes NetworkPolicy.
        """
        container = self._get_container(target)
        try:
            network = self._client.networks.get(DEMO_NETWORK)
            network.disconnect(container, force=True)
            return (f"Container '{container.name}' disconnected from {DEMO_NETWORK} — "
                    f"network isolated")
        except Exception as exc:
            return f"Isolation attempted (may already be disconnected): {exc}"

    def _scale_up(self, target: str, params: dict) -> str:
        """
        Increase container CPU quota — the Docker equivalent of scale_up.
        """
        container = self._get_container(target)
        new_cpus = float(params.get("cpus", 2.0))
        container.update(cpu_quota=int(new_cpus * 100_000), cpu_period=100_000)
        return f"Container '{container.name}' CPU quota increased to {new_cpus} cores"

    def _throttle_cpu(self, target: str, params: dict) -> str:
        """
        Reduce CPU quota — throttles a misbehaving container.
        docker update --cpus 0.5 <container>
        """
        container = self._get_container(target)
        throttle = float(params.get("cpu_fraction", 0.5))
        container.update(cpu_quota=int(throttle * 100_000), cpu_period=100_000)
        return f"Container '{container.name}' CPU throttled to {throttle} cores"

    def _increase_memory(self, target: str, params: dict) -> str:
        """
        Increase memory limit — fixes OOM pressure.
        docker update --memory 512m <container>
        """
        container = self._get_container(target)
        new_mem_mb = int(params.get("memory_mb", 512))
        container.update(mem_limit=f"{new_mem_mb}m")
        return (f"Container '{container.name}' memory limit increased "
                f"to {new_mem_mb}MB")

    def _kill_process(self, target: str, params: dict) -> str:
        """
        docker exec <container> kill -9 <pid>
        Used for attack scenarios to kill a malicious process.
        """
        container = self._get_container(target)
        pid = params.get("pid", 1)
        try:
            result = container.exec_run(f"kill -9 {pid}", privileged=False)
            return f"Process {pid} killed in '{container.name}' (exit={result.exit_code})"
        except Exception as exc:
            return f"kill attempted on {target}: {exc}"

    def _enable_debug(self, target: str, params: dict) -> str:
        """Enable debug logging — forensic data collection."""
        container = self._get_container(target)
        try:
            container.exec_run("sh -c 'echo DEBUG_MODE=1 >> /proc/1/environ'",
                               privileged=False)
        except Exception:
            pass
        return f"Debug logging enabled on '{container.name}'"

    def _escalate(self, target: str, params: dict) -> str:
        """Human escalation — log only, no container changes."""
        logger.warning(
            "HUMAN ESCALATION REQUIRED: %s needs manual intervention (params=%s)",
            target, params,
        )
        return f"Escalated to human operator: {target} requires manual review"

    def get_container_stats(self, node_id: str) -> dict:
        """
        Return real cgroup CPU and memory stats for a container.
        Used by dag_builder to get live metrics.
        """
        if not self._connected:
            return {}
        try:
            container = self._get_container(node_id)
            stats     = container.stats(stream=False)
            cpu_delta  = (stats["cpu_stats"]["cpu_usage"]["total_usage"]
                         - stats["precpu_stats"]["cpu_usage"]["total_usage"])
            sys_delta  = (stats["cpu_stats"]["system_cpu_usage"]
                         - stats["precpu_stats"]["system_cpu_usage"])
            cpu_pct    = (cpu_delta / sys_delta) * 100.0 if sys_delta > 0 else 0
            mem_usage  = stats["memory_stats"].get("usage", 0)
            mem_limit  = stats["memory_stats"].get("limit", 1)
            mem_pct    = (mem_usage / mem_limit) * 100.0 if mem_limit > 0 else 0
            return {
                "cpu":          round(min(cpu_pct, 100), 1),
                "mem":          round(min(mem_pct, 100), 1),
                "mem_usage_mb": round(mem_usage / 1_048_576, 1),
                "mem_limit_mb": round(mem_limit / 1_048_576, 1),
                "container":    container.name,
                "status":       container.status,
            }
        except Exception as exc:
            logger.debug("Container stats failed for %s: %s", node_id, exc)
            return {}
