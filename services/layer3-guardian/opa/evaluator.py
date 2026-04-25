"""
CCDT Layer-3 Guardian — OPA Policy Evaluator
═══════════════════════════════════════════════════════════════════════════════
Evaluates all 5 Rego policies against a proposed remediation action.

Two evaluation modes:

  1. Remote OPA server (production)
     Sends input to http://opa:8181/v1/data/ccdt/guardian/policies/<policy>
     OPA must be deployed as a sidecar or service with the .rego files loaded.

  2. Local fallback (dev / OPA unreachable)
     A pure-Python rule engine that replicates the core logic of each policy.
     Covers the most common allow/deny paths. Less expressive than Rego but
     sufficient for local development without OPA installed.

Usage:
    evaluator = OPAEvaluator(opa_url="http://opa:8181")
    result = await evaluator.evaluate(action_input)
    if result.allowed:
        await executor.run(...)
    else:
        log.warning("OPA blocked action: %s", result.violations)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

logger = logging.getLogger("ccdt.guardian.opa_evaluator")

# ─── Configuration ────────────────────────────────────────────────────────────
OPA_URL             = os.getenv("OPA_URL",              "http://opa:8181")
OPA_TIMEOUT_S       = float(os.getenv("OPA_TIMEOUT_S",  "1.5"))
OPA_FALLBACK_LOCAL  = os.getenv("OPA_FALLBACK_LOCAL",   "true").lower() == "true"

# All 5 policies — evaluated in this order
POLICY_NAMES = [
    "no_privilege_escalation",
    "lateral_movement",
    "egress_control",
    "cpu_threshold",
    "oom_notification",
]


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class PolicyDecision:
    """Result from a single OPA policy evaluation."""
    policy:     str
    allowed:    bool
    violations: list[str] = field(default_factory=list)
    audit:      dict      = field(default_factory=dict)
    source:     str       = "opa"   # "opa" | "local_fallback"
    latency_ms: float     = 0.0


@dataclass
class OPAResult:
    """
    Aggregated result from evaluating all policies against a single action.
    An action is only allowed when ALL policies return allow=True.
    """
    allowed:      bool
    decisions:    list[PolicyDecision] = field(default_factory=list)
    violations:   list[str]            = field(default_factory=list)
    notify_flags: list[str]            = field(default_factory=list)  # e.g. ["oom_notify"]
    total_ms:     float                = 0.0
    source:       str                  = "opa"

    def to_dict(self) -> dict:
        return {
            "allowed":    self.allowed,
            "violations": self.violations,
            "decisions": [
                {"policy": d.policy, "allowed": d.allowed, "violations": d.violations}
                for d in self.decisions
            ],
            "notifyFlags": self.notify_flags,
            "totalMs":    round(self.total_ms, 2),
            "source":     self.source,
        }


# ─── Action input builder ─────────────────────────────────────────────────────

def build_action_input(
    action_name:    str,
    target_node:    str,
    node_state:     dict,
    cluster_state:  dict,
    parameters:     Optional[dict]    = None,
    context:        Optional[dict]    = None,
    action_history: Optional[list]   = None,
) -> dict:
    """
    Build the OPA input document for a remediation action evaluation.

    Args:
        action_name     Action name string (e.g. "isolate_container")
        target_node     Node ID being acted on
        node_state      Dict with keys: cpu, mem, status, class, layer,
                        oom_kills, cap_event, file_event, is_isolated
        cluster_state   Dict with keys: namespace, nodes, node_mem_total_gb
        parameters      Action-specific parameters
        context         Dict with: autonomy_mode, user, rbac_subject,
                        can_write_ns, human_approved
        action_history  List of recent action dicts for the target node

    Returns:
        OPA input document dict
    """
    return {
        "action": {
            "name":        action_name,
            "target_node": target_node,
            "parameters":  parameters or {},
            "history":     action_history or [],
        },
        "node":    node_state,
        "cluster": cluster_state,
        "context": {
            "autonomy_mode":  "supervised",
            "user":           "ccdt-guardian",
            "rbac_subject":   "ccdt-guardian",
            "can_write_ns":   ["default", "production", "ccdt"],
            "human_approved": False,
            **(context or {}),
        },
    }


# ─── OPA Evaluator ────────────────────────────────────────────────────────────

class OPAEvaluator:
    """
    Evaluates remediation actions against all 5 OPA policies.

    Falls back to LocalFallbackEvaluator when OPA is unreachable.
    """

    def __init__(
        self,
        opa_url:        str  = OPA_URL,
        timeout_s:      float = OPA_TIMEOUT_S,
        use_fallback:   bool  = OPA_FALLBACK_LOCAL,
    ) -> None:
        self._opa_url      = opa_url.rstrip("/")
        self._timeout      = timeout_s
        self._use_fallback = use_fallback
        self._fallback     = LocalFallbackEvaluator()
        self._opa_healthy  = False
        self._last_health_check = 0.0

    async def evaluate(self, action_input: dict) -> OPAResult:
        """
        Evaluate all policies for the given action input.
        Returns an OPAResult with allow/deny decision + violations.
        """
        t0 = time.perf_counter()

        # Check OPA reachability (cached 30 s)
        opa_available = await self._check_opa_health()

        if opa_available:
            try:
                result = await self._eval_remote(action_input)
                result.total_ms = (time.perf_counter() - t0) * 1000
                return result
            except Exception as exc:
                logger.warning("OPA remote eval failed: %s — using local fallback", exc)

        if self._use_fallback:
            result = self._fallback.evaluate(action_input)
            result.total_ms = (time.perf_counter() - t0) * 1000
            result.source   = "local_fallback"
            return result

        # If no fallback: default deny
        logger.error("OPA unreachable and local fallback disabled — denying action")
        return OPAResult(
            allowed    = False,
            violations = ["OPA server unreachable and local fallback disabled"],
            source     = "error",
            total_ms   = (time.perf_counter() - t0) * 1000,
        )

    async def _eval_remote(self, action_input: dict) -> OPAResult:
        """
        Query OPA REST API for each policy in parallel.
        Endpoint: POST /v1/data/ccdt/guardian/policies/<policy_name>
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            tasks = [
                self._query_policy(client, policy, action_input)
                for policy in POLICY_NAMES
            ]
            decisions = await asyncio.gather(*tasks, return_exceptions=True)

        valid_decisions: list[PolicyDecision] = []
        all_violations:  list[str]            = []
        notify_flags:    list[str]            = []

        for i, dec in enumerate(decisions):
            if isinstance(dec, Exception):
                logger.warning("Policy %s error: %s", POLICY_NAMES[i], dec)
                # On error: fail open with a warning (policy misconfiguration ≠ deny)
                valid_decisions.append(PolicyDecision(
                    policy   = POLICY_NAMES[i],
                    allowed  = True,
                    violations = [f"Policy eval error: {dec}"],
                    source   = "error",
                ))
            else:
                valid_decisions.append(dec)
                if not dec.allowed:
                    all_violations.extend(dec.violations)
                # Collect notify flags from OOM policy
                if dec.policy == "oom_notification":
                    if dec.audit.get("notify_required"):
                        notify_flags.append("oom_notify")

        allowed = all(d.allowed for d in valid_decisions)
        return OPAResult(
            allowed      = allowed,
            decisions    = valid_decisions,
            violations   = all_violations,
            notify_flags = notify_flags,
            source       = "opa",
        )

    async def _query_policy(
        self,
        client:       httpx.AsyncClient,
        policy_name:  str,
        action_input: dict,
    ) -> PolicyDecision:
        """Query one OPA policy endpoint."""
        url = f"{self._opa_url}/v1/data/ccdt/guardian/policies/{policy_name}"
        t0  = time.perf_counter()

        resp = await client.post(url, json={"input": action_input})
        resp.raise_for_status()

        data    = resp.json().get("result", {})
        allowed = bool(data.get("allow", False))
        viols   = list(data.get("violations", []))
        audit   = dict(data.get("audit", {}))

        return PolicyDecision(
            policy     = policy_name,
            allowed    = allowed,
            violations = viols,
            audit      = audit,
            source     = "opa",
            latency_ms = (time.perf_counter() - t0) * 1000,
        )

    async def _check_opa_health(self) -> bool:
        """Check OPA health with a 30 s cache."""
        now = time.time()
        if now - self._last_health_check < 30:
            return self._opa_healthy

        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(f"{self._opa_url}/health")
                self._opa_healthy = resp.status_code == 200
        except Exception:
            self._opa_healthy = False

        self._last_health_check = now
        return self._opa_healthy

    async def load_policies(self, policies_dir: str) -> bool:
        """
        Upload all .rego files in policies_dir to the OPA server via PUT /v1/policies/<id>.
        Returns True if all policies were uploaded successfully.
        """
        import pathlib
        rego_files = list(pathlib.Path(policies_dir).glob("*.rego"))
        if not rego_files:
            logger.warning("No .rego files found in %s", policies_dir)
            return False

        success = True
        async with httpx.AsyncClient(timeout=5.0) as client:
            for rego in rego_files:
                policy_id = rego.stem
                try:
                    resp = await client.put(
                        f"{self._opa_url}/v1/policies/{policy_id}",
                        content=rego.read_bytes(),
                        headers={"Content-Type": "text/plain"},
                    )
                    if resp.status_code in (200, 201):
                        logger.info("Loaded policy: %s", policy_id)
                    else:
                        logger.error("Failed to load %s: HTTP %d — %s",
                                     policy_id, resp.status_code, resp.text)
                        success = False
                except Exception as exc:
                    logger.error("Error loading policy %s: %s", policy_id, exc)
                    success = False

        return success


# ─── Local Fallback Evaluator ─────────────────────────────────────────────────

class LocalFallbackEvaluator:
    """
    Pure-Python implementation of the core allow/deny logic for all 5 policies.
    Used when the OPA server is unreachable.

    Covers the most critical deny paths. Not a complete Rego reimplementation.
    """

    def evaluate(self, inp: dict) -> OPAResult:
        """Evaluate all 5 policies locally. Returns OPAResult."""
        decisions    = []
        all_viols    = []
        notify_flags = []

        checks = [
            ("no_privilege_escalation", self._check_priv_esc),
            ("lateral_movement",        self._check_lateral),
            ("egress_control",          self._check_egress),
            ("cpu_threshold",           self._check_cpu),
            ("oom_notification",        self._check_oom),
        ]

        for name, fn in checks:
            allowed, viols, flags = fn(inp)
            decisions.append(PolicyDecision(
                policy     = name,
                allowed    = allowed,
                violations = viols,
                source     = "local_fallback",
            ))
            all_viols.extend(viols)
            notify_flags.extend(flags)

        return OPAResult(
            allowed      = all(d.allowed for d in decisions),
            decisions    = decisions,
            violations   = all_viols,
            notify_flags = notify_flags,
            source       = "local_fallback",
        )

    # ── Policy implementations ────────────────────────────────────────────────

    def _check_priv_esc(self, inp: dict) -> tuple[bool, list[str], list[str]]:
        action = inp.get("action", {})
        params = action.get("parameters", {})
        name   = action.get("name", "")
        viols  = []

        always_blocked = {
            "grant_privileged_mode", "add_capability",
            "modify_security_context", "run_as_root",
        }
        dangerous_caps = {
            "CAP_SYS_ADMIN", "CAP_SYS_PTRACE", "CAP_SYS_MODULE",
            "CAP_SYS_RAWIO", "CAP_NET_ADMIN", "CAP_SETUID", "CAP_SETGID",
        }

        if name in always_blocked:
            viols.append(f"Action '{name}' is unconditionally blocked by no_privilege_escalation")

        cap = params.get("capability")
        if cap and cap in dangerous_caps:
            viols.append(f"Dangerous capability '{cap}' is not permitted")

        if params.get("run_as_user") == 0 or params.get("privileged"):
            viols.append("Running as root (uid=0 or privileged=true) is not permitted")

        return (len(viols) == 0), viols, []

    def _check_lateral(self, inp: dict) -> tuple[bool, list[str], list[str]]:
        action     = inp.get("action", {})
        node       = inp.get("node", {})
        name       = action.get("name", "")
        target     = action.get("target_node", "")
        viols      = []

        # Rollback on attack node
        if name == "rollback_deployment" and node.get("class") == "attack":
            viols.append(
                f"rollback_deployment on attack-classified node '{target}' blocked — isolate first"
            )

        # Restart unisolated attack node
        requires_isolation = {
            "restart_pod", "rollback_deployment",
            "scale_up_replicas", "enable_debug_logging",
        }
        if (name in requires_isolation
                and node.get("class") == "attack"
                and not node.get("is_isolated")):
            if not any("rollback" in v for v in viols):  # deduplicate
                viols.append(
                    f"'{name}' on attack node '{target}' requires network isolation first"
                )

        # Retry loop check
        history = action.get("history", [])
        recent_same = [
            h for h in history
            if h.get("action_name") == name
            and h.get("target_node") == target
            and h.get("age_minutes", 999) <= 10
        ]
        if len(recent_same) >= 3:
            viols.append(
                f"Action '{name}' on '{target}' attempted {len(recent_same)} times in 10 min — possible loop"
            )

        # Always allow isolate on attack node
        if name == "isolate_container" and node.get("class") == "attack":
            return True, [], []

        return (len(viols) == 0), viols, []

    def _check_egress(self, inp: dict) -> tuple[bool, list[str], list[str]]:
        action  = inp.get("action", {})
        cluster = inp.get("cluster", {})
        ctx     = inp.get("context", {})
        name    = action.get("name", "")
        params  = action.get("parameters", {})
        viols   = []

        approved_ns = {
            "production", "prod", "staging", "default",
            "kube-system", "monitoring", "ccdt",
        }
        blocked_cidrs = {"0.0.0.0/0", "::/0"}

        egress_actions = {"apply_network_policy", "isolate_container"}

        if name in egress_actions:
            ns = cluster.get("namespace", "default")
            if ns not in approved_ns:
                viols.append(f"Namespace '{ns}' not in approved list for network policy changes")

            for rule in params.get("egress_rules", []):
                if rule.get("cidr") in blocked_cidrs:
                    viols.append(
                        f"Egress CIDR '{rule.get('cidr')}' opens unrestricted internet access"
                    )

            if ctx.get("autonomy_mode") == "full-auto":
                viols.append("Network policy changes not permitted in full-auto mode")

        return (len(viols) == 0), viols, []

    def _check_cpu(self, inp: dict) -> tuple[bool, list[str], list[str]]:
        action = inp.get("action", {})
        node   = inp.get("node", {})
        name   = action.get("name", "")
        params = action.get("parameters", {})
        viols  = []

        cpu       = float(node.get("cpu", 50))
        layer     = node.get("layer", "service")
        protected = {"data", "system"}

        if name == "scale_down_replicas" and cpu < 20:
            viols.append(
                f"scale_down_replicas blocked: CPU {cpu:.0f}% is already below 20% threshold"
            )

        if name == "throttle_cpu":
            if cpu < 30:
                viols.append(
                    f"throttle_cpu blocked: CPU {cpu:.0f}% already below 30% minimum"
                )
            if layer in protected:
                viols.append(
                    f"throttle_cpu blocked: '{layer}' layer nodes must not be CPU-throttled"
                )
            limit = params.get("cpu_limit_cores", 1.0)
            if limit < 0.1:
                viols.append(
                    f"throttle_cpu blocked: requested limit {limit} cores is below 0.1 core floor"
                )

        if name in {"drain_node", "cordon_node"}:
            nodes = inp.get("cluster", {}).get("nodes", [])
            system_nodes = [
                n for n in nodes
                if n.get("layer") == "system" and n.get("status") != "drained"
            ]
            if layer == "system" and len(system_nodes) == 1:
                viols.append(
                    f"drain_node/cordon_node blocked: only one system-layer node running"
                )

        return (len(viols) == 0), viols, []

    def _check_oom(self, inp: dict) -> tuple[bool, list[str], list[str]]:
        action  = inp.get("action", {})
        node    = inp.get("node", {})
        cluster = inp.get("cluster", {})
        ctx     = inp.get("context", {})
        name    = action.get("name", "")
        params  = action.get("parameters", {})
        viols   = []
        flags   = []

        oom_kills = int(node.get("oom_kills", 0))
        layer     = node.get("layer", "service")
        target    = action.get("target_node", "")

        stateful_prefixes = (
            "postgres", "mysql", "redis", "kafka",
            "elasticsearch", "cassandra", "mongodb",
        )
        is_stateful = any(target.startswith(p) for p in stateful_prefixes)

        # Non-memory actions pass through
        if name not in {"increase_oom_threshold", "restart_pod", "drain_node"}:
            return True, [], []

        if name == "increase_oom_threshold":
            if oom_kills < 1:
                viols.append(
                    f"increase_oom_threshold requires >= 1 OOM kill; node has {oom_kills}"
                )
            if is_stateful and not ctx.get("human_approved"):
                viols.append(
                    f"Stateful workload '{target}' memory changes require human approval"
                )
            limit = params.get("new_mem_limit_gb", 0)
            total = cluster.get("node_mem_total_gb", 0)
            if limit and total and (limit / total) * 100 > 90:
                viols.append(
                    f"Requested memory limit {limit:.1f} GB exceeds 90% of node capacity"
                )

        # OOM storm
        if oom_kills >= 5:
            viols.append(
                f"OOM storm: {oom_kills} kills detected — requires human review"
            )

        # Notify flags
        if layer == "data" and oom_kills >= 1:
            flags.append("oom_notify")
        if oom_kills >= 5:
            flags.append("oom_escalate")

        return (len(viols) == 0), viols, flags
