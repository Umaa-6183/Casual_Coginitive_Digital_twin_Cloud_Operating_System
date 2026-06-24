"""
CCDT Layer-4 Co-Pilot — Fast Response Cache & Autonomous Recovery
═══════════════════════════════════════════════════════════════════

Intelligent caching layer that enables:
1. Sub-100ms responses for repeated queries
2. Autonomous recovery trigger on critical incidents
3. Pre-computed responses for common scenarios
4. Direct WebSocket broadcast to UI
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("ccdt.copilot.fast_cache")


@dataclass
class CachedResponse:
    """Cached AI response with metadata."""
    response_text: str
    tool_calls: list[dict]
    model_used: str
    timestamp: float
    hit_count: int = 0
    last_hit: float = field(default_factory=time.time)
    ttl_seconds: float = 30.0  # Default 30s cache

    def is_valid(self) -> bool:
        """Check if cache entry is still fresh."""
        return (time.time() - self.timestamp) < self.ttl_seconds

    def to_dict(self) -> dict:
        """Convert to response dict."""
        return {
            "reply": self.response_text,
            "tool_calls": self.tool_calls,
            "model_used": self.model_used,
            "cached": True,
            "cache_age_ms": round((time.time() - self.timestamp) * 1000, 1),
            "hit_count": self.hit_count,
        }


@dataclass
class AutonomousRecoveryTrigger:
    """Configuration for autonomous fix triggers."""
    incident_type: str  # "critical", "attack", "fault"
    confidence_threshold: float = 0.70
    auto_fix_enabled: bool = True
    max_auto_fixes_per_hour: int = 10
    cooldown_seconds: float = 300.0  # 5 min between auto-fixes

    recent_fixes: list[float] = field(default_factory=list)
    last_fix_time: float = 0.0

    def should_trigger(self, incident: dict) -> bool:
        """Check if we should auto-fix this incident."""
        if not self.auto_fix_enabled:
            return False

        # Check confidence
        confidence = incident.get("rootCauseConfidence", 0)
        if confidence < self.confidence_threshold:
            return False

        # Check cooldown
        now = time.time()
        if now - self.last_fix_time < self.cooldown_seconds:
            logger.debug("Auto-fix in cooldown period")
            return False

        # Check rate limit
        recent = [t for t in self.recent_fixes if now - t < 3600]  # Last hour
        if len(recent) >= self.max_auto_fixes_per_hour:
            logger.warning("Auto-fix rate limit reached: %d/hour", len(recent))
            return False

        return True

    def record_fix(self) -> None:
        """Record that we executed a fix."""
        now = time.time()
        self.last_fix_time = now
        self.recent_fixes.append(now)
        # Keep only last hour
        self.recent_fixes = [t for t in self.recent_fixes if now - t < 3600]


class FastResponseCache:
    """
    Intelligent cache for Co-Pilot responses.

    Features:
    - Query fingerprinting for instant cache hits
    - Scenario-based pre-computed responses
    - Autonomous recovery triggers
    - Metrics tracking
    """

    def __init__(self):
        self._cache: dict[str, CachedResponse] = {}
        self._scenario_templates: dict[str, str] = self._init_templates()
        self._recovery_triggers: dict[str, AutonomousRecoveryTrigger] = {
            "critical": AutonomousRecoveryTrigger(
                incident_type="critical",
                confidence_threshold=0.75,
                auto_fix_enabled=True,
            ),
            "attack": AutonomousRecoveryTrigger(
                incident_type="attack",
                confidence_threshold=0.80,
                auto_fix_enabled=True,
                cooldown_seconds=180.0,  # 3 min for attacks
            ),
        }

        # Metrics
        self._hits = 0
        self._misses = 0
        self._auto_fixes = 0
        self._last_cleanup = time.time()

    def _init_templates(self) -> dict[str, str]:
        """Pre-computed response templates for common scenarios."""
        return {
            "healthy": """
✅ **Cluster Status: HEALTHY**

All services are operating normally with no detected incidents.

**System Health:**
- GNN Classification: Healthy (>90% confidence)
- Active Incidents: 0
- Recent Guardian Actions: None required
- eBPF Anomalies: None detected

**Performance Metrics:**
- Average Response Time: <50ms
- Resource Utilization: Normal
- No memory pressure or CPU saturation

The autonomous AIOps system is actively monitoring all nodes.
            """.strip(),

            "oom_cascade": """
🔴 **CRITICAL: PostgreSQL OOM Cascade Detected**

**Executive Summary:**
Memory pressure cascade detected on postgres node with 94% confidence. The GNN identified this as a FAULT (not an attack). Immediate action required.

**Root Cause Analysis:**
- **Root Node**: postgres
- **Confidence**: 94%
- **Incident Type**: FAULT (memory exhaustion)
- **Causal Chain**: postgres → order-svc → payment-svc

**Blast Radius:**
- Primary: postgres (512MB limit exceeded)
- Secondary: order-svc (connection pool exhausted)
- Tertiary: payment-svc (transaction queue backing up)

**Autonomous Remediation:**
The Guardian is executing: `restart_pod` on postgres
- **Ghost Preview**: Risk Score 12/100, MTTR improvement 78%
- **OPA Status**: All 5 policies passed
- **ETA**: <60 seconds to recovery

**eBPF Evidence:**
- OOM killer events: 3 in last 30s
- WAL write buffer full
- TCP retransmit rate elevated (conn storm)

**Recommendation:**
Monitor for recurrence. If pattern repeats, increase memory limit to 1GB.
            """.strip(),

            "cryptominer": """
🚨 **CRITICAL ATTACK: Cryptominer Process Detected**

**Executive Summary:**
The GNN detected an ATTACK pattern on auth-svc with 87% confidence. eBPF telemetry confirms suspicious syscall sequences consistent with cryptocurrency mining.

**Attack Classification:**
- **Attack Type**: Cryptominer
- **Affected Node**: auth-svc
- **Confidence**: 87%
- **First Detected**: 23 seconds ago

**eBPF Security Events:**
- Syscall: execve("xmrig") detected
- Network: stratum+tcp connections to mining pool
- CPU: Sudden spike to 99% on single container
- Capability: CAP_SYS_ADMIN escalation attempt

**Autonomous Response:**
1. ✅ Container isolated from network (Ghost Risk: 8/100)
2. ✅ CPU throttled to 5% to limit damage
3. ⏳ Forensic snapshot captured
4. ⏳ OPA policy auto-generated: `block_xmrig_exec`

**Self-Authoring Immune System:**
A new Rego policy has been generated and is pending approval:
```rego
package ccdt.guardian.policies
default block_cryptominer = false
block_cryptominer {
    input.ebpf_events[_].type == "syscall"
    contains(input.ebpf_events[_].detail, "xmrig")
}
```

**Next Steps:**
- Approve OPA policy in Dashboard → Policies tab
- Review container image for supply chain compromise
- Audit recent deployments and access logs
            """.strip(),

            "status_query": """
**Current Cluster Status**

{context_summary}

**Active Monitoring:**
- Layer-1 (eBPF): ✅ Capturing kernel events
- Layer-2 (GNN): ✅ Inference running every 3s
- Layer-3 (Guardian): ✅ {autonomy_mode} mode active
- Layer-4 (Co-Pilot): ✅ {provider_chain}

Use the tools to deep-dive into specific nodes or recent incidents.
            """.strip(),
        }

    def get_cache_key(self, user_message: str, context_summary: str) -> str:
        """Generate cache key from message + context fingerprint."""
        # Normalize message
        msg_norm = user_message.lower().strip()

        # Create context fingerprint (stable for similar states)
        ctx_fingerprint = self._fingerprint_context(context_summary)

        # Combine
        key_str = f"{msg_norm}::{ctx_fingerprint}"
        return hashlib.sha256(key_str.encode()).hexdigest()[:16]

    def _fingerprint_context(self, context_summary: str) -> str:
        """Create stable fingerprint of cluster state."""
        # Extract key state markers
        try:
            if "healthy" in context_summary.lower() and "critical" not in context_summary.lower():
                return "state:healthy"

            # Look for incident types
            if "oom" in context_summary.lower() or "memory" in context_summary.lower():
                return "state:oom_critical"

            if "cryptominer" in context_summary.lower() or "xmrig" in context_summary.lower():
                return "state:attack_cryptominer"

            if "cpu" in context_summary.lower() and "saturation" in context_summary.lower():
                return "state:cpu_critical"

            if "attack" in context_summary.lower():
                return "state:attack_general"

            if "fault" in context_summary.lower() or "critical" in context_summary.lower():
                return "state:fault_critical"

            return "state:unknown"
        except:
            return "state:unknown"

    def get(self, cache_key: str) -> Optional[CachedResponse]:
        """Retrieve cached response if valid."""
        entry = self._cache.get(cache_key)
        if entry and entry.is_valid():
            entry.hit_count += 1
            entry.last_hit = time.time()
            self._hits += 1
            logger.info("Cache HIT: %s (age: %.1fs, hits: %d)",
                       cache_key, time.time() - entry.timestamp, entry.hit_count)
            return entry

        self._misses += 1
        return None

    def set(self, cache_key: str, response: str, tool_calls: list[dict],
            model_used: str, ttl_seconds: float = 30.0) -> None:
        """Store response in cache."""
        self._cache[cache_key] = CachedResponse(
            response_text=response,
            tool_calls=tool_calls,
            model_used=model_used,
            timestamp=time.time(),
            ttl_seconds=ttl_seconds,
        )
        logger.debug("Cache SET: %s (TTL: %.0fs)", cache_key, ttl_seconds)

    def get_template(self, scenario: str, **kwargs) -> Optional[str]:
        """Get pre-computed template response."""
        template = self._scenario_templates.get(scenario)
        if template and kwargs:
            try:
                return template.format(**kwargs)
            except KeyError:
                return template
        return template

    async def check_autonomous_recovery(self, incident_data: dict) -> Optional[dict]:
        """
        Check if we should trigger autonomous recovery.

        Returns action dict if trigger fires, None otherwise.
        """
        severity = incident_data.get("severity", "").lower()
        incident_type = incident_data.get("incidentType", "fault")
        root_node = incident_data.get("rootCauseNode", "unknown")
        confidence = incident_data.get("rootCauseConfidence", 0)

        # Check triggers
        trigger = self._recovery_triggers.get(severity) or self._recovery_triggers.get(incident_type)
        if not trigger:
            return None

        if not trigger.should_trigger(incident_data):
            return None

        # Determine action based on incident type
        if incident_type == "attack":
            action_id = 1  # isolate_container
            action_name = "isolate_container"
        elif "oom" in str(incident_data).lower():
            action_id = 5  # restart_pod
            action_name = "restart_pod"
        elif "cpu" in str(incident_data).lower():
            action_id = 12  # throttle_cpu
            action_name = "throttle_cpu"
        else:
            action_id = 5  # restart_pod (default)
            action_name = "restart_pod"

        # Record fix
        trigger.record_fix()
        self._auto_fixes += 1

        logger.warning(
            "🤖 AUTONOMOUS RECOVERY TRIGGERED: %s on %s (confidence: %.0f%%)",
            action_name, root_node, confidence * 100
        )

        return {
            "action_id": action_id,
            "action_name": action_name,
            "target_node": root_node,
            "incident_type": incident_type,
            "confidence": confidence,
            "trigger": "autonomous",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns number removed."""
        now = time.time()

        # Only cleanup every 60s
        if now - self._last_cleanup < 60:
            return 0

        self._last_cleanup = now
        before = len(self._cache)
        self._cache = {k: v for k, v in self._cache.items() if v.is_valid()}
        removed = before - len(self._cache)

        if removed > 0:
            logger.info("Cache cleanup: removed %d expired entries", removed)

        return removed

    def get_stats(self) -> dict:
        """Get cache statistics."""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0

        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": round(hit_rate, 1),
            "cache_size": len(self._cache),
            "auto_fixes_total": self._auto_fixes,
            "auto_fix_enabled": {
                k: v.auto_fix_enabled for k, v in self._recovery_triggers.items()
            },
        }

    def clear(self) -> int:
        """Clear all cache entries."""
        count = len(self._cache)
        self._cache.clear()
        return count
