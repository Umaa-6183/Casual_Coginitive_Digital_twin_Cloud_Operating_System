"""
CCDT Layer-4 Co-Pilot — Cluster Context Builder
════════════════════════════════════════════════════════════════════════════════
Assembles a rich, LLM-ready context string from all CCDT data sources:

  Source          Endpoint / method              Cache TTL
  ────────────────────────────────────────────────────────
  GNN inference   POST /infer (Layer-2)          3 s
  GNN topology    GET  /topology (Layer-2)        10 s
  Guardian state  GET  /actions/history (L3)      5 s
  eBPF live       GET  /events?limit=20 (L1)      2 s
  Active incidents GET  /api/v1/incidents (GW)    5 s
  Counterfactual  (injected per request)          —

The resulting context is injected into every Claude API call as a system
prompt addendum, keeping Claude grounded in real-time cluster reality.

Context sections:
  [INCIDENT OVERVIEW]   severity, type, elapsed, blast radius
  [CAUSAL GNN]          root cause, confidence, per-node classification
  [eBPF TELEMETRY]      top anomalous signals (cap, OOM, TCP, sched)
  [GUARDIAN STATUS]     recent actions, OPA decisions, pending approvals
  [TOPOLOGY SNAPSHOT]   node statuses in tabular form
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("ccdt.copilot.context_builder")

# ─── Service URLs ─────────────────────────────────────────────────────────────
GNN_URL = os.getenv("GNN_SERVICE_URL",      "http://layer2-cognitive:8001")
GUARDIAN_URL = os.getenv("GUARDIAN_SERVICE_URL", "http://layer3-guardian:8002")
EBPF_URL = os.getenv("EBPF_SERVICE_URL",     "http://layer1-nervous:9100")
API_GW_URL = os.getenv("API_GATEWAY_URL",      "http://api-gateway:8000")

HTTP_TIMEOUT = float(os.getenv("CONTEXT_FETCH_TIMEOUT_S", "3.0"))

CLASS_EMOJI = {"healthy": "✅", "fault": "⚠️", "attack": "🚨"}
STATUS_EMOJI = {"healthy": "✅", "warning": "⚠️", "critical": "🔴"}


class _Cache:
    """Simple TTL cache for a single async fetch."""

    def __init__(self, ttl_s: float) -> None:
        self._ttl = ttl_s
        self._value = None
        self._ts = 0.0

    def get(self) -> Optional[Any]:
        if time.monotonic() - self._ts < self._ttl:
            return self._value
        return None

    def set(self, value: Any) -> None:
        self._value = value
        self._ts = time.monotonic()


class ClusterContextBuilder:
    """
    Fetches and formats real-time cluster state for LLM context injection.

    Usage:
        builder = ClusterContextBuilder()
        context_str = await builder.build_context()
        # → inject into Claude system prompt
    """

    def __init__(self) -> None:
        self._cache_gnn = _Cache(ttl_s=3.0)
        self._cache_topo = _Cache(ttl_s=10.0)
        self._cache_guard = _Cache(ttl_s=5.0)
        self._cache_ebpf = _Cache(ttl_s=2.0)
        self._cache_incs = _Cache(ttl_s=5.0)

    async def build_context(
        self,
        include_topology:    bool = True,
        include_ebpf:        bool = True,
        include_guardian:    bool = True,
        include_incidents:   bool = True,
        extra_context:       Optional[str] = None,
    ) -> dict:
        """
        Fetch all data sources in parallel and assemble a context string.
        Any fetch that fails returns a graceful placeholder.
        """
        tasks = {
            "gnn":   self._fetch_gnn_inference(),
            "topo":  self._fetch_topology() if include_topology else self._noop({}),
            "guard": self._fetch_guardian() if include_guardian else self._noop({}),
            "ebpf":  self._fetch_ebpf() if include_ebpf else self._noop({}),
            "incs":  self._fetch_incidents() if include_incidents else self._noop([]),
        }
        names = list(tasks.keys())
        results_raw = await asyncio.gather(*tasks.values(), return_exceptions=True)
        results = dict(zip(names, results_raw))

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines: list[str] = [f"=== CCDT REAL-TIME CLUSTER CONTEXT  [{now}] ==="]

        # ── 1. Incident Overview ──────────────────────────────────────────────
        gnn = results["gnn"] if isinstance(results["gnn"], dict) else {}
        lines += self._fmt_incident_overview(gnn)

        # ── 2. Causal GNN ─────────────────────────────────────────────────────
        lines += self._fmt_gnn(gnn)

        # ── 3. Topology snapshot ──────────────────────────────────────────────
        topo = results["topo"] if isinstance(results["topo"], dict) else {}
        if topo and include_topology:
            lines += self._fmt_topology(topo)

        # ── 4. eBPF telemetry ─────────────────────────────────────────────────
        ebpf = results["ebpf"] if isinstance(
            results["ebpf"], (dict, list)) else {}
        if ebpf and include_ebpf:
            lines += self._fmt_ebpf(ebpf)

        # ── 5. Guardian status ────────────────────────────────────────────────
        guard = results["guard"] if isinstance(results["guard"], dict) else {}
        if guard and include_guardian:
            lines += self._fmt_guardian(guard)

        # ── 6. Active incidents ───────────────────────────────────────────────
        incs = results["incs"] if isinstance(results["incs"], list) else []
        if incs and include_incidents:
            lines += self._fmt_incidents(incs)

        # ── 7. Extra context (counterfactual, user-injected) ──────────────────
        if extra_context:
            lines.append("\n[ADDITIONAL CONTEXT]")
            lines.append(extra_context)

        lines.append("=== END CONTEXT ===")
        return {
            "timestamp": now,

            "incident": {
                "type": gnn.get("incidentType", "healthy"),
                "confidence": gnn.get("graphClassification", {}).get(
                    gnn.get("incidentType", "healthy"), 0
                ),
                "root_cause": gnn.get("rootCauseNode", "none"),
                "root_confidence": gnn.get("rootCauseConfidence", 0),
            },

            "impact": {
                "blast_radius_nodes": gnn.get("blastRadius", []),
                "blast_radius_count": len(gnn.get("blastRadius", [])),
            },

            "metrics": {
                "causal_chain_depth": len(gnn.get("causalChain", [])),
            },

            "topology": topo.get("nodes", []) if isinstance(topo, dict) else [],

            "incidents": incs if isinstance(incs, list) else [],

            "ebpf_summary": ebpf if isinstance(ebpf, list) else ebpf.get("events", []),

            # 🔥 Optional: keep your nice text for fallback
        }

    # ── Formatters ────────────────────────────────────────────────────────────

    def _fmt_incident_overview(self, gnn: dict) -> list[str]:
        inc_type = gnn.get("incidentType", "healthy")
        emoji = CLASS_EMOJI.get(inc_type, "❓")
        conf = gnn.get("graphClassification", {}).get(inc_type, 0)
        root = gnn.get("rootCauseNode",       "none identified")
        r_conf = gnn.get("rootCauseConfidence", 0)
        blast = gnn.get("blastRadius", [])

        if inc_type == "healthy":
            return ["\n[INCIDENT OVERVIEW]", "  ✅ Cluster is HEALTHY — no active incidents"]

        return [
            "\n[INCIDENT OVERVIEW]",
            f"  {emoji} Status:     {inc_type.upper()}  (confidence: {conf:.0%})",
            f"  🎯 Root Cause: {root}  (confidence: {r_conf:.0%})",
            f"  💥 Blast Radius: {', '.join(blast) if blast else 'none'}  ({len(blast)} nodes)",
        ]

    def _fmt_gnn(self, gnn: dict) -> list[str]:
        if not gnn:
            return ["\n[CAUSAL GNN]", "  ⚠️  GNN service unreachable"]

        lines = ["\n[CAUSAL GNN — Node Classifications]"]
        nc = gnn.get("nodeClassifications", {})
        for node, probs in nc.items():
            dominant = max(probs, key=probs.get)
            conf = probs[dominant]
            if dominant != "healthy" or conf > 0.65:
                emoji = CLASS_EMOJI.get(dominant, "❓")
                bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
                lines.append(
                    f"  {emoji} {node:20s}  {dominant:8s}  [{bar}] {conf:.0%}")

        chain = gnn.get("causalChain", [])
        if chain:
            lines.append("  Causal chain: " + " → ".join(
                f"{c['node']}({c['causalScore']:.2f})" for c in chain[:5]
            ))

        return lines

    def _fmt_topology(self, topo: dict) -> list[str]:
        nodes = topo.get("nodes", [])
        if not nodes:
            return []
        lines = ["\n[TOPOLOGY SNAPSHOT]",
                 f"  {'Node':20s}  {'Status':9s}  {'Layer':8s}  {'CPU':>5s}  {'MEM':>5s}  {'Restarts':>8s}"]
        lines.append("  " + "─" * 60)
        for n in nodes:
            s = n.get("status", "healthy")
            em = STATUS_EMOJI.get(s, "❓")
            lines.append(
                f"  {em} {n.get('id','?'):18s}  {s:9s}  {n.get('layer','?'):8s}"
                f"  {n.get('cpu',0):4.0f}%  {n.get('mem',0):4.0f}%  {n.get('restarts',0):>8d}"
            )
        return lines

    def _fmt_ebpf(self, ebpf: dict) -> list[str]:
        events = ebpf if isinstance(ebpf, list) else ebpf.get("events", [])
        if not events:
            return []
        # Deduplicate by type and show top anomalies
        by_type: dict[str, list] = {}
        for ev in events:
            t = ev.get("type", "unknown")
            by_type.setdefault(t, []).append(ev)

        lines = ["\n[eBPF TELEMETRY — Recent Anomalies]"]
        type_labels = {
            "capability": "🔑 Capability escalation",
            "oom":        "💀 OOM kill",
            "tcp":        "📡 TCP retransmit",
            "sched":      "⏱️  Scheduler latency",
            "file":       "📁 Sensitive file access",
            "syscall":    "⚙️  Suspicious syscall",
        }
        for t, evs in sorted(by_type.items()):
            label = type_labels.get(t, t)
            sample = evs[0]
            comm = sample.get("comm", sample.get("process", "?"))
            lines.append(f"  {label}: {len(evs)} event(s) — process: {comm}")
        return lines

    def _fmt_guardian(self, guard: dict) -> list[str]:
        history = guard.get("history", [])[-5:]
        pending_cnt = guard.get("pending_count", 0)
        autonomy = guard.get("autonomy_mode", "unknown")

        lines = [f"\n[GUARDIAN STATUS — autonomy: {autonomy}]"]
        if pending_cnt > 0:
            lines.append(f"  ⏳ {pending_cnt} action(s) pending human approval")

        if history:
            lines.append("  Recent actions:")
            for h in reversed(history):
                status_icon = "✅" if "execut" in h.get("status", "") else "🚫"
                lines.append(
                    f"    {status_icon} {h.get('name','?'):25s}  "
                    f"target={h.get('target','?'):15s}  status={h.get('status','?')}"
                )
        else:
            lines.append("  No recent guardian actions")
        return lines

    def _fmt_incidents(self, incs: list) -> list[str]:
        lines = [f"\n[ACTIVE INCIDENTS — {len(incs)} total]"]
        for inc in incs[:5]:
            sev = inc.get("severity", "?").upper()
            icon = {"CRITICAL": "🔴", "HIGH": "🟠",
                    "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "⚪")
            lines.append(
                f"  {icon} [{sev}] {inc.get('id','?'):12s}  "
                f"{inc.get('title','?')[:60]}  "
                f"elapsed={inc.get('elapsed','?')}"
            )
        return lines

    # ── Data fetchers ─────────────────────────────────────────────────────────

    async def _fetch_gnn_inference(self) -> dict:
        cached = self._cache_gnn.get()
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                r = await c.post(f"{GNN_URL}/infer", json={})
                if r.status_code == 200:
                    data = r.json()
                    self._cache_gnn.set(data)
                    return data
        except Exception as exc:
            logger.debug("GNN inference fetch failed: %s", exc)
        return {}

    async def _fetch_topology(self) -> dict:
        cached = self._cache_topo.get()
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                r = await c.get(f"{GNN_URL}/topology")
                if r.status_code == 200:
                    data = r.json()
                    self._cache_topo.set(data)
                    return data
        except Exception as exc:
            logger.debug("Topology fetch failed: %s", exc)
        return {}

    async def _fetch_guardian(self) -> dict:
        cached = self._cache_guard.get()
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                hist_r = await c.get(f"{GUARDIAN_URL}/actions/history?limit=10")
                pend_r = await c.get(f"{GUARDIAN_URL}/actions/pending")
                data = {
                    "history":       hist_r.json().get("history", []) if hist_r.status_code == 200 else [],
                    "autonomy_mode": hist_r.json().get("autonomy_mode", "unknown") if hist_r.status_code == 200 else "unknown",
                    "pending_count": len(pend_r.json().get("pending", [])) if pend_r.status_code == 200 else 0,
                }
                self._cache_guard.set(data)
                return data
        except Exception as exc:
            logger.debug("Guardian fetch failed: %s", exc)
        return {}

    async def _fetch_ebpf(self) -> dict:
        cached = self._cache_ebpf.get()
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                r = await c.get(f"{EBPF_URL}/events?limit=50")
                if r.status_code == 200:
                    data = r.json()
                    self._cache_ebpf.set(data)
                    return data
        except Exception as exc:
            logger.debug("eBPF events fetch failed: %s", exc)
        return {}

    async def _fetch_incidents(self) -> list:
        cached = self._cache_incs.get()
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                r = await c.get(f"{API_GW_URL}/api/v1/incidents?status=active&limit=10")
                if r.status_code == 200:
                    data = r.json().get("incidents", [])
                    self._cache_incs.set(data)
                    return data
        except Exception as exc:
            logger.debug("Incidents fetch failed: %s", exc)
        return []

    @staticmethod
    async def _noop(default: Any) -> Any:
        return default
