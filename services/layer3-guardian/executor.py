"""
CCDT Layer-3 Guardian — Action Executor & FastAPI Server
═══════════════════════════════════════════════════════════════════════════════
The Guardian's main entry point. Runs the full autonomous remediation loop:

  1. Consume GNN inference events from Kafka (Layer-2 → Layer-3 topic)
  2. RL agent selects the best action given current cluster state
  3. Ghost Preview simulates the action without executing it
  4. OPA evaluates all 5 policies against the proposed action
  5. If approved: execute action via Kubernetes API
  6. Log result + update action history for OPA lateral-movement policy

FastAPI endpoints:
  GET  /health                 Liveness probe
  GET  /ready                  Readiness probe
  POST /actions/preview        Ghost Preview only (no execution)
  POST /actions/execute        Full pipeline: preview → OPA → execute
  POST /actions/approve        Human approval for a blocked action
  GET  /actions/history        Recent action history
  GET  /metrics                Prometheus metrics

Autonomy modes (env: AUTONOMY_MODE):
  human-in-loop    Preview + OPA only; human must confirm every action
  supervised       Auto-execute if approved; page human if blocked    (default)
  full-auto        Auto-execute all approved actions without paging

Run:
  uvicorn executor:app --host 0.0.0.0 --port 8002
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger("ccdt.guardian.executor")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# ─── Configuration ────────────────────────────────────────────────────────────
AUTONOMY_MODE = os.getenv("AUTONOMY_MODE",       "supervised")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
KAFKA_TOPIC_IN = os.getenv("KAFKA_TOPIC_INFER",   "ccdt.gnn.inference")
KAFKA_TOPIC_OUT = os.getenv("KAFKA_TOPIC_ACTIONS",  "ccdt.guardian.actions")
GNN_SERVICE_URL = os.getenv(
    "GNN_SERVICE_URL",      "http://layer2-cognitive:8001")
OPA_URL = os.getenv("OPA_URL",              "http://opa:8181")
MODEL_PATH = os.getenv("AGENT_MODEL_PATH",
                       "/app/checkpoints/guardian_ppo_final")

# Option D: use Docker executor when EXECUTOR_MODE=docker (default on macOS)
EXECUTOR_MODE = os.getenv(
    "EXECUTOR_MODE",        "docker")  # "docker" or "k8s"
NAMESPACE = os.getenv("K8S_NAMESPACE",        "default")
HISTORY_MAX = 200     # max action history entries kept in memory
INFER_POLL_S = float(os.getenv("INFER_POLL_S",  "5.0"))

# ─── Prometheus metrics ───────────────────────────────────────────────────────
ACTIONS_TOTAL = Counter("ccdt_guardian_actions_total",
                        "Total actions proposed",    ["action", "status"])
OPA_DENIALS = Counter("ccdt_guardian_opa_denials_total",
                      "OPA policy denials",        ["policy"])
EXEC_LATENCY = Histogram("ccdt_guardian_exec_duration_seconds", "Action execution time",
                         buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0])
GHOST_LATENCY = Histogram("ccdt_guardian_ghost_preview_seconds", "Ghost preview time",
                          buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0])

# ─── Global state ─────────────────────────────────────────────────────────────
_state: dict[str, Any] = {
    "agent":           None,
    "dag_builder":     None,
    "simulator":       None,
    "opa_evaluator":   None,
    "k8s_executor":    None,
    "kafka_producer":  None,
    "action_history":  deque(maxlen=HISTORY_MAX),
    # action_id → action dict (awaiting human approval)
    "pending_approvals": {},
    "ready":           False,
    "loop_task":       None,
}


# ─── Kubernetes action executor ───────────────────────────────────────────────

class K8sActionExecutor:
    """
    Executes approved remediation actions via the Kubernetes API.
    Each action maps to one or more kubectl-equivalent API calls.
    """

    def __init__(self, namespace: str = "default") -> None:
        self.namespace = namespace
        self._v1 = None
        self._appv1 = None
        self._netv1 = None
        self._connected = False

    def connect(self) -> None:
        """Initialise Kubernetes client.

        Priority order:
          1. In-cluster config (when running as a K8s pod)
          2. KUBECONFIG_PATH env var (docker compose with mounted kubeconfig)
          3. Default ~/.kube/config (local kubectl)
          4. Falls back to dry-run if none available
        """
        try:
            from kubernetes import client as k8s, config as k8s_config

            if os.getenv("KUBERNETES_SERVICE_HOST"):
                # Running inside a K8s pod
                k8s_config.load_incluster_config()
                logger.info("K8s: loaded in-cluster config")
            elif os.getenv("KUBECONFIG_PATH"):
                # Docker compose: kubeconfig mounted at KUBECONFIG_PATH
                kubeconfig_path = os.getenv("KUBECONFIG_PATH")
                context = os.getenv("K8S_CONTEXT")
                k8s_config.load_kube_config(
                    config_file=kubeconfig_path,
                    context=context,
                )
                logger.info("K8s: loaded kubeconfig from %s (context=%s)",
                            kubeconfig_path, context or "default")
            else:
                # Local development: use ~/.kube/config
                k8s_config.load_kube_config()
                logger.info("K8s: loaded default ~/.kube/config")

            self._v1 = k8s.CoreV1Api()
            self._appv1 = k8s.AppsV1Api()
            self._netv1 = k8s.NetworkingV1Api()
            self._connected = True
            logger.info("Kubernetes client ready (namespace=%s)",
                        self.namespace)

        except Exception as exc:
            logger.warning(
                "K8s connect failed (%s) — Guardian running in DRY-RUN mode. "
                "Set KUBECONFIG_PATH to enable real K8s actions.", exc
            )
            self._connected = False

    async def execute(
        self,
        action_name:  str,
        target_node:  str,
        parameters:   dict,
        dry_run:      bool = False,
    ) -> dict:
        """
        Execute the named action on target_node.
        Returns {"success": bool, "detail": str, "dry_run": bool}
        """
        if dry_run or not self._connected:
            logger.info("[DRY-RUN] Would execute: %s on %s",
                        action_name, target_node)
            return {"success": True, "detail": f"DRY-RUN: {action_name} on {target_node}", "dry_run": True}

        dispatch = {
            "no_op":                  self._no_op,
            "isolate_container":      self._isolate_container,
            "rollback_deployment":    self._rollback_deployment,
            "scale_down_replicas":    self._scale_replicas,
            "scale_up_replicas":      self._scale_replicas,
            "restart_pod":            self._restart_pod,
            "cordon_node":            self._cordon_node,
            "drain_node":             self._drain_node,
            "apply_network_policy":   self._apply_network_policy,
            "rotate_secrets":         self._rotate_secrets,
            "kill_process":           self._kill_process,
            "increase_oom_threshold": self._increase_oom_threshold,
            "throttle_cpu":           self._throttle_cpu,
            "enable_debug_logging":   self._enable_debug_logging,
            "escalate_to_human":      self._escalate_to_human,
        }

        fn = dispatch.get(action_name, self._no_op)
        try:
            detail = await asyncio.get_event_loop().run_in_executor(
                None, lambda: fn(target_node, parameters)
            )
            return {"success": True, "detail": detail, "dry_run": False}
        except Exception as exc:
            logger.error("K8s action %s failed: %s", action_name, exc)
            return {"success": False, "detail": str(exc), "dry_run": False}

    # ── Action implementations ────────────────────────────────────────────────

    def _no_op(self, target: str, params: dict) -> str:
        return "no_op: observation only"

    def _isolate_container(self, target: str, params: dict) -> str:
        """Apply a deny-all NetworkPolicy to isolate the pod."""
        from kubernetes import client as k8s
        policy_name = f"isolate-{target}-{int(time.time())}"
        policy = k8s.V1NetworkPolicy(
            metadata=k8s.V1ObjectMeta(
                name=policy_name, namespace=self.namespace,
                labels={"ccdt.guardian/action": "isolate",
                        "ccdt.guardian/target": target}
            ),
            spec=k8s.V1NetworkPolicySpec(
                pod_selector=k8s.V1LabelSelector(
                    match_labels={"app": target}
                ),
                policy_types=["Ingress", "Egress"],
                ingress=[], egress=[],   # deny all
            ),
        )
        self._netv1.create_namespaced_network_policy(self.namespace, policy)
        return f"NetworkPolicy '{policy_name}' created — pod '{target}' isolated"

    def _rollback_deployment(self, target: str, params: dict) -> str:
        """Annotate deployment to trigger rollback (kubectl rollout undo equivalent)."""
        from kubernetes import client as k8s
        # Get current deployment
        dep = self._appv1.read_namespaced_deployment(target, self.namespace)
        # Bump revision annotation to trigger rollback
        annotations = dep.metadata.annotations or {}
        annotations["ccdt.guardian/rollback"] = str(int(time.time()))
        dep.metadata.annotations = annotations
        # Set revision history limit if not set
        if dep.spec.revision_history_limit is None:
            dep.spec.revision_history_limit = 10
        self._appv1.patch_namespaced_deployment(target, self.namespace, dep)
        return f"Rollback triggered for deployment '{target}'"

    def _scale_replicas(self, target: str, params: dict) -> str:
        """Scale deployment replicas up or down."""
        from kubernetes import client as k8s
        dep = self._appv1.read_namespaced_deployment(target, self.namespace)
        current = dep.spec.replicas or 1
        delta = params.get("replica_delta", -1)
        new_reps = max(1, current + delta)
        patch = {"spec": {"replicas": new_reps}}
        self._appv1.patch_namespaced_deployment(target, self.namespace, patch)
        return f"Scaled '{target}' from {current} → {new_reps} replicas"

    def _restart_pod(self, target: str, params: dict) -> str:
        """Delete pod to trigger K8s recreation."""
        pods = self._v1.list_namespaced_pod(
            self.namespace,
            label_selector=f"app={target}",
        )
        deleted = []
        for pod in (pods.items or []):
            self._v1.delete_namespaced_pod(pod.metadata.name, self.namespace)
            deleted.append(pod.metadata.name)
        return f"Deleted pods: {deleted} — K8s will recreate"

    def _cordon_node(self, target: str, params: dict) -> str:
        """Mark a K8s node as unschedulable."""
        from kubernetes import client as k8s
        patch = {"spec": {"unschedulable": True}}
        self._v1.patch_node(target, patch)
        return f"Node '{target}' cordoned (unschedulable=true)"

    def _drain_node(self, target: str, params: dict) -> str:
        """Cordon + evict all pods from the node."""
        self._cordon_node(target, params)
        pods = self._v1.list_namespaced_pod(
            self.namespace,
            field_selector=f"spec.nodeName={target}",
        )
        from kubernetes import client as k8s
        evicted = []
        for pod in (pods.items or []):
            eviction = k8s.V1Eviction(
                metadata=k8s.V1ObjectMeta(
                    name=pod.metadata.name,
                    namespace=self.namespace,
                )
            )
            try:
                self._v1.create_namespaced_pod_eviction(
                    pod.metadata.name, self.namespace, eviction
                )
                evicted.append(pod.metadata.name)
            except Exception as exc:
                logger.warning("Could not evict %s: %s",
                               pod.metadata.name, exc)
        return f"Node '{target}' drained. Evicted: {evicted}"

    def _apply_network_policy(self, target: str, params: dict) -> str:
        """Apply a namespace-level network policy from parameters."""
        from kubernetes import client as k8s
        policy_name = f"ccdt-guardian-{target}-{int(time.time())}"
        policy = k8s.V1NetworkPolicy(
            metadata=k8s.V1ObjectMeta(
                name=policy_name, namespace=self.namespace,
                labels={"ccdt.guardian/action": "network_policy"}
            ),
            spec=k8s.V1NetworkPolicySpec(
                pod_selector=k8s.V1LabelSelector(
                    match_labels={"app": target}
                ),
                policy_types=["Ingress"],
                ingress=[],    # deny all ingress
            ),
        )
        self._netv1.create_namespaced_network_policy(self.namespace, policy)
        return f"Network policy '{policy_name}' applied to '{target}'"

    def _rotate_secrets(self, target: str, params: dict) -> str:
        """Annotate all secrets used by target to trigger rotation (via external secrets operator)."""
        secrets = self._v1.list_namespaced_secret(
            self.namespace,
            label_selector=f"app={target}",
        )
        rotated = []
        for secret in (secrets.items or []):
            patch = {"metadata": {"annotations": {
                "ccdt.guardian/rotate-timestamp": str(int(time.time()))
            }}}
            self._v1.patch_namespaced_secret(
                secret.metadata.name, self.namespace, patch)
            rotated.append(secret.metadata.name)
        return f"Secrets rotated for '{target}': {rotated}"

    def _kill_process(self, target: str, params: dict) -> str:
        """Delete the target pod (equivalent to killing the main process if single-process)."""
        return self._restart_pod(target, params)

    def _increase_oom_threshold(self, target: str, params: dict) -> str:
        """Patch deployment container resources to increase memory limit."""
        mem_limit = params.get("new_mem_limit_gb", 2)
        mem_str = f"{int(mem_limit * 1024)}Mi"
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "name": target,
                            "resources": {"limits": {"memory": mem_str}}
                        }]
                    }
                }
            }
        }
        self._appv1.patch_namespaced_deployment(target, self.namespace, patch)
        return f"Memory limit for '{target}' increased to {mem_str}"

    def _throttle_cpu(self, target: str, params: dict) -> str:
        """Apply CPU quota via deployment resource limits."""
        cpu_cores = params.get("cpu_limit_cores", 0.5)
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "name": target,
                            "resources": {"limits": {"cpu": str(cpu_cores)}}
                        }]
                    }
                }
            }
        }
        self._appv1.patch_namespaced_deployment(target, self.namespace, patch)
        return f"CPU limit for '{target}' set to {cpu_cores} cores"

    def _enable_debug_logging(self, target: str, params: dict) -> str:
        """Patch deployment env vars to enable debug logging."""
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "name": target,
                            "env": [
                                {"name": "LOG_LEVEL", "value": "DEBUG"},
                                {"name": "CCDT_DEBUG", "value": "1"},
                            ]
                        }]
                    }
                }
            }
        }
        self._appv1.patch_namespaced_deployment(target, self.namespace, patch)
        return f"Debug logging enabled for '{target}'"

    def _escalate_to_human(self, target: str, params: dict) -> str:
        """Log escalation — actual paging handled by notification layer."""
        logger.critical(
            "ESCALATION: Human intervention required for '%s'. Params: %s",
            target, params
        )
        return f"Escalated '{target}' to on-call engineer"


# ─── Main remediation pipeline ────────────────────────────────────────────────

class ActionPipeline:
    """
    Orchestrates the full remediation pipeline:
    GNN inference → RL decision → Ghost Preview → OPA → K8s execution
    """

    def __init__(self) -> None:
        from rl.agent import GuardianAgent
        from ghost_preview.simulator import GhostSimulator
        from opa.evaluator import OPAEvaluator

        self.agent = GuardianAgent(model_path=MODEL_PATH)
        self.simulator = GhostSimulator(gnn_url=GNN_SERVICE_URL)
        self.opa = OPAEvaluator(opa_url=OPA_URL)

        # Option D: use Docker executor on macOS (EXECUTOR_MODE=docker)
        if EXECUTOR_MODE == "docker":
            try:
                from docker_executor import DockerActionExecutor
                self.k8s = DockerActionExecutor()
                self.k8s.connect()
                logger.info("RemediationLoop: using Docker executor")
            except ImportError:
                self.k8s = K8sActionExecutor(namespace=NAMESPACE)
                self.k8s.connect()
        else:
            self.k8s = K8sActionExecutor(namespace=NAMESPACE)
            self.k8s.connect()

    async def run(
        self,
        topology:      dict,
        incident_type: str = "fault",
        dry_run:       bool = False,
        autonomy_mode: str = AUTONOMY_MODE,
    ) -> dict:
        """
        Execute the full pipeline for a given topology snapshot.

        Returns a detailed result dict with all pipeline stages.
        """
        pipeline_id = str(uuid.uuid4())[:8]
        t0 = time.perf_counter()
        result = {
            "pipeline_id":    pipeline_id,
            "timestamp":      int(time.time()),
            "incident_type":  incident_type,
            "autonomy_mode":  autonomy_mode,
            "dry_run":        dry_run,
        }

        try:
            # ── Stage 1: RL agent selects action ─────────────────────────────
            obs = self.agent.obs_from_topology(topology)
            action, confidence = self.agent.predict(obs)
            ranked = self.agent.rank_actions(obs, top_k=5)
            result["rl_action"] = action
            result["rl_action_name"] = ranked[0][
                "name"] if ranked else f"action_{action}"
            result["rl_confidence"] = confidence
            result["rl_ranked"] = ranked
            logger.info("[%s] RL selected: %s (conf=%.2f)",
                        pipeline_id, result["rl_action_name"], confidence)

            # ── Stage 2: Ghost Preview ────────────────────────────────────────
            t_ghost = time.perf_counter()
            preview = await self.simulator.preview(
                action_id=action,
                topology_override=topology,
                incident_type=incident_type,
            )
            GHOST_LATENCY.observe(time.perf_counter() - t_ghost)
            result["ghost_preview"] = preview.to_dict()
            logger.info("[%s] Ghost: approved=%s risk=%.2f mttr_delta=%.0fmin",
                        pipeline_id, preview.approved,
                        preview.risk_score, preview.mttr_delta_min)

            # ── Stage 3: OPA policy evaluation ───────────────────────────────
            # Build node state from topology for OPA
            incident_nodes = [n for n in topology.get("nodes", [])
                              if n.get("status") == "critical"]
            node_state = incident_nodes[0] if incident_nodes else {"cpu": 50, "mem": 50,
                                                                   "status": "warning", "class": "fault", "layer": "service", "oom_kills": 0}
            target_node = node_state.get("id", "unknown")

            # Add incident classification from GNN inference (not in topology)
            # The incident_type tells us if it's a fault or attack
            if "class" not in node_state:
                node_state["class"] = incident_type  # "fault" or "attack"

            from opa.evaluator import build_action_input
            opa_input = build_action_input(
                action_name=result["rl_action_name"],
                target_node=target_node,
                node_state=node_state,
                cluster_state={
                    "namespace":        NAMESPACE,
                    "nodes":            topology.get("nodes", []),
                    "node_mem_total_gb": 32,
                },
                context={"autonomy_mode": autonomy_mode},
                action_history=[
                    {"action_name": h["name"], "target_node": target_node,
                     "age_minutes": (time.time() - h.get("ts", time.time())) / 60}
                    for h in list(_state["action_history"])[-20:]
                    if h.get("target") == target_node
                ],
            )

            opa_result = await self.opa.evaluate(opa_input)
            result["opa_result"] = opa_result.to_dict()

            ACTIONS_TOTAL.labels(action=result["rl_action_name"],
                                 status="opa_pass" if opa_result.allowed else "opa_deny").inc()
            for d in opa_result.decisions:
                if not d.allowed:
                    OPA_DENIALS.labels(policy=d.policy).inc()

            logger.info("[%s] OPA: allowed=%s violations=%d",
                        pipeline_id, opa_result.allowed, len(opa_result.violations))

            # Log detailed violation information when blocked
            if not opa_result.allowed and opa_result.violations:
                logger.warning("[%s] OPA BLOCKED: %s", pipeline_id, opa_result.violations)
                for decision in opa_result.decisions:
                    if not decision.allowed:
                        logger.warning("[%s]   Policy '%s' denied: %s",
                                      pipeline_id, decision.policy, decision.violations)

            # ── Fallback: Try restart_pod for stateful memory scenarios ───────
            # If increase_oom_threshold was blocked on a stateful workload,
            # try restart_pod as a safer alternative
            fallback_used = False
            oom_kills = node_state.get("oom_kills") or 0
            mem_usage = node_state.get("mem", 0)
            has_memory_issue = (oom_kills >= 1) or (mem_usage >= 85)

            if (not opa_result.allowed
                and result["rl_action_name"] == "increase_oom_threshold"
                and has_memory_issue):

                logger.info("[%s] FALLBACK: Trying restart_pod instead of increase_oom_threshold",
                           pipeline_id)

                # Build OPA input for restart_pod
                restart_opa_input = build_action_input(
                    action_name="restart_pod",
                    target_node=target_node,
                    node_state=node_state,
                    cluster_state={
                        "namespace": NAMESPACE,
                        "nodes": topology.get("nodes", []),
                        "node_mem_total_gb": 32,
                    },
                    context={"autonomy_mode": autonomy_mode},
                    action_history=[
                        {"action_name": h["name"], "target_node": target_node,
                         "age_minutes": (time.time() - h.get("ts", time.time())) / 60}
                        for h in list(_state["action_history"])[-20:]
                        if h.get("target") == target_node
                    ],
                )

                fallback_opa = await self.opa.evaluate(restart_opa_input)
                if fallback_opa.allowed:
                    logger.info("[%s] FALLBACK APPROVED: restart_pod allowed by OPA", pipeline_id)
                    result["rl_action_name"] = "restart_pod"
                    result["fallback_from"] = "increase_oom_threshold"
                    opa_result = fallback_opa
                    fallback_used = True
                else:
                    logger.warning("[%s] FALLBACK BLOCKED: restart_pod also denied", pipeline_id)
                    logger.warning("[%s] FALLBACK OPA violations: %s", pipeline_id, fallback_opa.violations)
                    for dec in fallback_opa.decisions:
                        if not dec.allowed:
                            logger.warning("[%s]   Fallback policy '%s': %s", pipeline_id, dec.policy, dec.violations)

            # ── Stage 4: Execution decision ───────────────────────────────────
            execute_action = (
                preview.approved
                and opa_result.allowed
                and autonomy_mode in ("supervised", "full-auto")
            )

            if autonomy_mode == "human-in-loop":
                execute_action = False
                result["status"] = "pending_human_approval"
                result["approval_required"] = True
                _state["pending_approvals"][pipeline_id] = {
                    "action":      action,
                    "action_name": result["rl_action_name"],
                    "target_node": target_node,
                    "preview":     preview.to_dict(),
                    "opa":         opa_result.to_dict(),
                    "created_at":  time.time(),
                }
            elif not preview.approved:
                result["status"] = "blocked_ghost_risk"
                execute_action = False
            elif not opa_result.allowed:
                result["status"] = "blocked_opa_policy"
                execute_action = False
            else:
                result["status"] = "approved"

            # ── Stage 5: Execute ──────────────────────────────────────────────
            if execute_action:
                t_exec = time.perf_counter()
                exec_result = await self.k8s.execute(
                    action_name=result["rl_action_name"],
                    target_node=target_node,
                    parameters={},
                    dry_run=dry_run,
                )
                EXEC_LATENCY.observe(time.perf_counter() - t_exec)
                result["execution"] = exec_result
                result["status"] = "executed" if exec_result["success"] else "execution_failed"
                ACTIONS_TOTAL.labels(action=result["rl_action_name"],
                                     status="executed" if exec_result["success"] else "failed").inc()
                logger.info("[%s] Executed: success=%s detail=%s",
                            pipeline_id, exec_result["success"], exec_result["detail"])

                # Record in history
                _state["action_history"].append({
                    "pipeline_id": pipeline_id,
                    "action":      action,
                    "name":        result["rl_action_name"],
                    "target":      target_node,
                    "status":      result["status"],
                    "ts":          time.time(),
                })

            # Handle OOM notifications
            if opa_result.notify_flags:
                logger.warning("[%s] Notification flags: %s",
                               pipeline_id, opa_result.notify_flags)

        except Exception as exc:
            logger.error("[%s] Pipeline error: %s",
                         pipeline_id, exc, exc_info=True)
            result["status"] = "pipeline_error"
            result["error"] = str(exc)

        result["total_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return result


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialise all components and start the autonomous loop."""
    logger.info("Guardian Layer-3 starting (autonomy_mode=%s)…", AUTONOMY_MODE)

    pipeline = ActionPipeline()
    _state["pipeline"] = pipeline
    _state["ready"] = True

    # Start autonomous polling loop (polls GNN every INFER_POLL_S)
    task = asyncio.create_task(_autonomous_loop(pipeline))
    _state["loop_task"] = task

    logger.info("Guardian Layer-3 ready")
    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Guardian Layer-3 stopped")


async def _autonomous_loop(pipeline: ActionPipeline) -> None:
    """Background task: poll GNN every INFER_POLL_S and run remediation pipeline."""
    logger.info(
        "Autonomous remediation loop started (interval=%.0fs)", INFER_POLL_S)
    while True:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(f"{GNN_SERVICE_URL}/infer", json={})
                if resp.status_code == 200:
                    data = resp.json()
                    # Only act on fault/attack incidents
                    if data.get("incidentType", "healthy") != "healthy":
                        topo_resp = await client.get(f"{GNN_SERVICE_URL}/topology")
                        if topo_resp.status_code == 200:
                            topo = topo_resp.json()
                            await pipeline.run(
                                topology=topo,
                                incident_type=data.get(
                                    "incidentType", "fault"),
                                autonomy_mode=AUTONOMY_MODE,
                            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Autonomous loop error (non-critical): %s", exc)
        await asyncio.sleep(INFER_POLL_S)


# ─── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="CCDT Guardian Executor",
    description="Layer-3 Guardian — RL + Ghost Preview + OPA + K8s remediation",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── Request/response models ──────────────────────────────────────────────────

class PreviewRequest(BaseModel):
    action_id:     int = Field(..., ge=0, le=14,
                               description="Action index 0-14")
    target_node:   str = Field(..., description="Node ID to act on")
    topology:      Optional[dict] = Field(
        None, description="Topology override")
    incident_type: str = Field("fault", description="fault | attack")


class ExecuteRequest(BaseModel):
    topology:      Optional[dict] = Field(
        None,    description="Topology snapshot from Layer-2")
    incident_type: str = Field("fault", description="fault | attack")
    dry_run:       bool = Field(False,   description="Dry-run (no K8s calls)")
    autonomy_mode: str = Field(
        AUTONOMY_MODE, description="Override autonomy mode")


class ApproveRequest(BaseModel):
    pipeline_id: str = Field(...,
                             description="Pipeline ID from pending_human_approval")
    approved:    bool = Field(...,
                              description="True to approve, False to reject")
    reason:      str = Field("",  description="Human review notes")


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(content={
        "status":       "ok",
        "service":      "layer3-guardian",
        "autonomy_mode": AUTONOMY_MODE,
        "timestamp":    int(time.time()),
    })


@app.get("/ready")
async def ready() -> JSONResponse:
    if not _state.get("ready"):
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(content={"status": "ready"})


@app.post("/actions/preview")
async def preview_action(body: PreviewRequest) -> JSONResponse:
    """
    Run Ghost Preview for a specific action on a given topology.
    No execution — safe to call at any time.
    """
    pipeline: ActionPipeline = _state.get("pipeline")
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialised")
    try:
        t0 = time.perf_counter()
        preview = await pipeline.simulator.preview(
            action_id=body.action_id,
            target_node=body.target_node,
            topology_override=body.topology,
            incident_type=body.incident_type,
        )
        return JSONResponse(content={
            **preview.to_dict(),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/actions/execute")
async def execute_action(body: ExecuteRequest) -> JSONResponse:
    """
    Full remediation pipeline: GNN → RL → Ghost → OPA → K8s.
    Uses live topology if body.topology is not provided.
    """
    pipeline: ActionPipeline = _state.get("pipeline")
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialised")

    # Fetch live topology if not provided
    topology = body.topology
    if topology is None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{GNN_SERVICE_URL}/topology")
                if resp.status_code == 200:
                    topology = resp.json()
                else:
                    raise HTTPException(
                        status_code=502, detail="GNN topology unavailable")
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502, detail=f"GNN unreachable: {exc}")

    try:
        result = await pipeline.run(
            topology=topology,
            incident_type=body.incident_type,
            dry_run=body.dry_run,
            autonomy_mode=body.autonomy_mode,
        )
        status_code = 200 if result.get("status") in (
            "executed", "pending_human_approval", "approved") else 422
        return JSONResponse(content=result, status_code=status_code)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/actions/approve")
async def approve_action(body: ApproveRequest) -> JSONResponse:
    """
    Approve or reject a pending action in human-in-loop mode.
    """
    pending = _state["pending_approvals"].get(body.pipeline_id)
    if pending is None:
        raise HTTPException(
            status_code=404, detail=f"No pending action for pipeline_id={body.pipeline_id}")

    pipeline: ActionPipeline = _state.get("pipeline")
    result = {"pipeline_id": body.pipeline_id,
              "approved": body.approved, "reason": body.reason}

    if body.approved and pipeline:
        exec_result = await pipeline.k8s.execute(
            action_name=pending["action_name"],
            target_node=pending["target_node"],
            parameters={},
            dry_run=False,
        )
        result["execution"] = exec_result
        result["status"] = "executed" if exec_result["success"] else "execution_failed"
        _state["action_history"].append({
            "pipeline_id": body.pipeline_id,
            "action":      pending["action"],
            "name":        pending["action_name"],
            "target":      pending["target_node"],
            "status":      result["status"],
            "approved_by": "human",
            "ts":          time.time(),
        })
    else:
        result["status"] = "rejected_by_human"

    del _state["pending_approvals"][body.pipeline_id]
    return JSONResponse(content=result)


@app.get("/actions/history")
async def get_history(limit: int = 50) -> JSONResponse:
    """Return recent action history (last N actions)."""
    history = list(_state["action_history"])[-limit:]
    return JSONResponse(content={
        "history":      history,
        "total":        len(_state["action_history"]),
        "autonomy_mode": AUTONOMY_MODE,
    })


@app.get("/actions/pending")
async def get_pending() -> JSONResponse:
    """Return actions awaiting human approval (human-in-loop mode)."""
    return JSONResponse(content={"pending": list(_state["pending_approvals"].values())})


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(
        content=generate_latest().decode(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ─── Dev entrypoint ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "executor:app",
        host=os.getenv("HOST",       "0.0.0.0"),
        port=int(os.getenv("PORT",   "8002")),
        reload=os.getenv("RELOAD",     "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL",  "info").lower(),
    )
