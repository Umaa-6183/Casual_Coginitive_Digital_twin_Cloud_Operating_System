"""
CCDT Layer-4 Co-Pilot — Cluster Context Builder
════════════════════════════════════════════════════════════════════════════════
Assembles a rich, LLM-ready context string from all CCDT data sources:

  Source           Endpoint / method              Cache TTL
  ────────────────────────────────────────────────────────
  GNN inference    POST /infer (Layer-2)           3 s
  GNN topology     GET  /topology (Layer-2)        10 s
  Guardian state   GET  /actions/history (L3)      5 s
  eBPF live        GET  /events?limit=50 (L1)      2 s
  Active incidents GET  /api/v1/incidents (GW)     5 s
  Counterfactual   (injected per request)          —

build_context() returns a dict that includes BOTH structured data fields
AND a pre-formatted "context_text" string ready for LLM prompt injection.
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

CLASS_EMOJI = {"healthy": "✅", "fault": "⚠️",  "attack": "🚨"}
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
        builder      = ClusterContextBuilder()
        ctx          = await builder.build_context()
        context_text = ctx["context_text"]   # inject into Gemini prompt
    """

    def __init__(self) -> None:
        self._cache_gnn = _Cache(ttl_s=3.0)
        self._cache_topo = _Cache(ttl_s=10.0)
        self._cache_guard = _Cache(ttl_s=5.0)
        self._cache_ebpf = _Cache(ttl_s=2.0)
        self._cache_incs = _Cache(ttl_s=5.0)

    async def build_context(
        self,
        include_topology:  bool = True,
        include_ebpf:      bool = True,
        include_guardian:  bool = True,
        include_incidents: bool = True,
        extra_context:     Optional[str] = None,
    ) -> dict:
        """
        Fetch all data sources in parallel and assemble a context dict.

        Returns a dict with:
          - "context_text"  : pre-formatted string ready for LLM injection  ← KEY FIX
          - "timestamp"     : UTC timestamp string
          - "incident"      : structured incident summary
          - "impact"        : blast radius info
          - "metrics"       : causal chain depth etc.
          - "topology"      : list of node dicts
          - "incidents"     : list of active incident dicts
          - "ebpf_summary"  : list of eBPF event dicts

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

        # ── Unpack safely ─────────────────────────────────────────────────────
        gnn = results["gnn"] if isinstance(results["gnn"],   dict) else {}
        topo = results["topo"] if isinstance(results["topo"],  dict) else {}
        guard = results["guard"] if isinstance(results["guard"], dict) else {}
        ebpf = results["ebpf"] if isinstance(
            results["ebpf"],  (dict, list)) else {}
        incs = results["incs"] if isinstance(results["incs"],  list) else []

        # ── Build formatted context text ──────────────────────────────────────
        lines: list[str] = [f"=== CCDT REAL-TIME CLUSTER CONTEXT  [{now}] ==="]

        lines += self._fmt_incident_overview(gnn)
        lines += self._fmt_gnn(gnn)

        if topo and include_topology:
            lines += self._fmt_topology(topo)

        if ebpf and include_ebpf:
            lines += self._fmt_ebpf(ebpf)

        if guard and include_guardian:
            lines += self._fmt_guardian(guard)

        if incs and include_incidents:
            lines += self._fmt_incidents(incs)

        if extra_context:
            lines.append("\n[ADDITIONAL CONTEXT]")
            lines.append(extra_context)

        lines.append("=== END CONTEXT ===")

        # FIX: join lines into context_text and include it in the return dict.
        # Previously lines was built but then silently discarded — copilot.py
        # called raw_ctx.get("context_text") and got None every time, causing
        # Gemini to receive a raw JSON dump instead of the nicely formatted text.
        context_text = "\n".join(lines)

        return {
            # ← THE CRITICAL KEY that copilot.py reads
            "context_text": context_text,

            "timestamp": now,

            "incident": {
                "type":            gnn.get("incidentType", "healthy"),
                "confidence":      gnn.get("graphClassification", {}).get(
                    gnn.get("incidentType", "healthy"), 0
                ),
                "root_cause":      gnn.get("rootCauseNode", "none"),
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

            "incidents": incs,

            "ebpf_summary": (
                ebpf if isinstance(ebpf, list) else ebpf.get("events", [])
            ),
        }

    # ── Formatters ────────────────────────────────────────────────────────────

    def _fmt_incident_overview(self, gnn: dict) -> list[str]:
        inc_type = gnn.get("incidentType", "healthy")
        emoji = CLASS_EMOJI.get(inc_type, "❓")
        conf = gnn.get("graphClassification", {}).get(inc_type, 0)
        root = gnn.get("rootCauseNode",        "none identified")
        r_conf = gnn.get("rootCauseConfidence",  0)
        blast = gnn.get("blastRadius",           [])

        if inc_type == "healthy":
            return ["\n[INCIDENT OVERVIEW]", "  ✅ Cluster is HEALTHY — no active incidents"]

        return [
            "\n[INCIDENT OVERVIEW]",
            f"  {emoji} Status:      {inc_type.upper()}  (confidence: {conf:.0%})",
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
                    f"  {emoji} {node:20s}  {dominant:8s}  [{bar}] {conf:.0%}"
                )

        chain = gnn.get("causalChain", [])
        if chain:
            lines.append(
                "  Causal chain: " + " → ".join(
                    f"{c['node']}({c['causalScore']:.2f})" for c in chain[:5]
                )
            )
        return lines

    def _fmt_topology(self, topo: dict) -> list[str]:
        nodes = topo.get("nodes", [])
        if not nodes:
            return []
        lines = [
            "\n[TOPOLOGY SNAPSHOT]",
            f"  {'Node':20s}  {'Status':9s}  {'Layer':8s}  {'CPU':>5s}  {'MEM':>5s}  {'Restarts':>8s}",
            "  " + "─" * 60,
        ]
        for n in nodes:
            s = n.get("status", "healthy")
            em = STATUS_EMOJI.get(s, "❓")
            lines.append(
                f"  {em} {n.get('id','?'):18s}  {s:9s}  {n.get('layer','?'):8s}"
                f"  {n.get('cpu',0):4.0f}%  {n.get('mem',0):4.0f}%"
                f"  {n.get('restarts',0):>8d}"
            )
        return lines

    def _fmt_ebpf(self, ebpf) -> list[str]:
        events = ebpf if isinstance(ebpf, list) else ebpf.get("events", [])
        if not events:
            return []

        by_type: dict[str, list] = {}
        for ev in events:
            t = ev.get("type", "unknown")
            by_type.setdefault(t, []).append(ev)

        type_labels = {
            "capability": "🔑 Capability escalation",
            "oom":        "💀 OOM kill",
            "tcp":        "📡 TCP retransmit",
            "sched":      "⏱️  Scheduler latency",
            "file":       "📁 Sensitive file access",
            "syscall":    "⚙️  Suspicious syscall",
        }
        lines = ["\n[eBPF TELEMETRY — Recent Anomalies]"]
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
                    f"target={h.get('target','?'):15s}  "
                    f"status={h.get('status','?')}"
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

                # FIX: parse each response exactly once into a local variable.
                # The original code called hist_r.json() twice — the second call
                # raises an error because the response body stream is already consumed.
                hist_json = hist_r.json() if hist_r.status_code == 200 else {}
                pend_json = pend_r.json() if pend_r.status_code == 200 else {}

                data = {
                    "history":       hist_json.get("history", []),
                    "autonomy_mode": hist_json.get("autonomy_mode", "unknown"),
                    "pending_count": len(pend_json.get("pending", [])),
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
                r = await c.get(
                    f"{API_GW_URL}/api/v1/incidents?status=active&limit=10"
                )
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
