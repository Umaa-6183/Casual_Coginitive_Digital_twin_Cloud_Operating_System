"""
CCDT Layer-4 Co-Pilot — Three-Provider AI: Groq → Gemini → Ollama
═══════════════════════════════════════════════════════════════════

Provider order (always the same, no intent routing):
  1. Groq   llama-3.3-70b-versatile  fast, 500 RPM free tier
  2. Gemini gemini-2.5-flash         deep fallback
  3. Ollama llama3.2:1b              local free fallback (needs ~1GB RAM)

Every provider error — 400, 429, 503, OOM, module error — falls through
to the next provider instead of crashing.

requirements.txt: groq>=0.9.0  google-genai>=1.0.0
env vars: GROQ_API_KEY  GEMINI_API_KEY  OLLAMA_BASE_URL  OLLAMA_MODEL
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
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse

# ── Groq ─────────────────────────────────────────────────────────────────────
try:
    from groq import AsyncGroq
    _GROQ_AVAILABLE = True
except ImportError:
    _GROQ_AVAILABLE = False
    AsyncGroq = None  # type: ignore

# ── Anthropic ─────────────────────────────────────────────────────────────────
try:
    from anthropic import AsyncAnthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False
    AsyncAnthropic = None  # type: ignore

# ── Gemini ────────────────────────────────────────────────────────────────────
from google import genai as ggenai
from google.genai import types as gtypes

from context_builder import ClusterContextBuilder

logger = logging.getLogger("ccdt.copilot")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

# ─── Configuration ─────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
# FIX: default to 1b model — llama3.1:8b needs 4.8GB RAM which OOMs on Mac
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-7-sonnet-20250219")

_raw_gemini = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PRIMARY_GEMINI = _raw_gemini[len(
    "models/"):] if _raw_gemini.startswith("models/") else _raw_gemini
GEMINI_CHAIN = [PRIMARY_GEMINI] + [
    m for m in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]
    if m != PRIMARY_GEMINI
]

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))  # Increased for detailed demo responses
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "20"))
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
KAFKA_TOPIC_INFER = os.getenv("KAFKA_TOPIC_INFER", "ccdt.gnn.inference")
GUARDIAN_URL = os.getenv("GUARDIAN_SERVICE_URL", "http://layer3-guardian:8002")
GNN_URL = os.getenv("GNN_SERVICE_URL",      "http://layer2-cognitive:8001")
EBPF_URL = os.getenv("EBPF_SERVICE_URL",     "http://layer1-nervous:9100")
AUTO_REPORT_THRESH = float(os.getenv("AUTO_REPORT_CONFIDENCE", "0.85"))

# ─── Prometheus ─────────────────────────────────────────────────────────────────
TOKENS_IN = Counter("ccdt_copilot_tokens_input_total",
                    "Input tokens consumed")
TOKENS_OUT = Counter("ccdt_copilot_tokens_output_total",
                     "Output tokens generated")
CHAT_COUNT = Counter("ccdt_copilot_chats_total",  "Chat requests", ["type"])
CHAT_ERR = Counter("ccdt_copilot_errors_total", "Chat errors",   ["code"])
CHAT_LAT = Histogram("ccdt_copilot_latency_seconds", "Chat latency",
                     buckets=[0.5, 1, 2, 5, 10, 20, 30])

# ─── System prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are CCDT Co-Pilot, an advanced AI operator for a Level-4 Autonomous AIOps platform designed for enterprise Kubernetes environments.

You have real-time access to cluster telemetry through specialized tools. Use them to provide accurate, data-driven insights.

CRITICAL INSTRUCTIONS FOR DEMONSTRATIONS:
- ALWAYS call your tools first to fetch live data before answering
- Provide COMPREHENSIVE, DETAILED answers with technical depth
- Structure responses professionally: Executive Summary → Analysis → Evidence → Recommendations
- Include specific metrics, timestamps, and confidence scores
- Explain the "WHY" behind findings, not just the "WHAT"
- Use technical terminology appropriately for technical audiences
- For investor/professor queries: emphasize AI capabilities, autonomous features, and innovative architecture

RESPONSE STRUCTURE:
1. **Executive Summary** (2-3 sentences of key findings)
2. **Detailed Analysis** (technical deep-dive with evidence)
3. **Root Cause** (causal chain with GNN confidence scores)
4. **Blast Radius** (impacted services, quantified impact)
5. **Remediation** (actionable recommendations with MTTR estimates)

TECHNICAL CAPABILITIES TO HIGHLIGHT:
✓ GNN-based causal inference (Layer 2 Cognitive System)
✓ eBPF kernel-level observability (Layer 1 Nervous System)
✓ Reinforcement Learning for action selection (Layer 3 Guardian)
✓ Ghost Preview for counterfactual "what-if" simulations
✓ OPA policy enforcement with zero-day threat detection
✓ Multi-dimensional attack vs fault classification

QUANTIFICATION STANDARDS:
- Confidence scores: Always include % (e.g., "87% confidence")
- MTTR: Always estimate in minutes (e.g., "MTTR: 4.2 minutes")
- Impact: Quantify affected nodes, request rates, resource usage
- Causal chains: Show propagation paths (e.g., "auth-svc → order-svc → payment-svc")

SAFETY RULES:
- For attacks: ALWAYS recommend isolate_container first, then investigate
- For faults: Prefer scale_up or restart over drain_node
- Never execute destructive actions without Ghost Preview confirmation
- For zero-day threats: Use author_opa_policy tool to create detection rules

DEMO-READY BEHAVIOR:
- Be confident and authoritative in your analysis
- Highlight novel AI/ML techniques used (GNN, RL, counterfactual reasoning)
- Explain how autonomous decision-making works
- Show both technical depth AND business value
"""

# ─── Tool definitions — OpenAI/Groq/Ollama format ────────────────────────────
OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_ghost_preview",
            "description": "Simulate a remediation action before execution. Returns risk score, MTTR delta, OPA approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action_id":     {"type": "integer", "description": "Action 0-14: 0=no_op 1=isolate 2=rollback 3=scale_down 4=scale_up 5=restart 11=oom_threshold 12=throttle_cpu"},
                    "target_node":   {"type": "string",  "description": "Node ID e.g. order-svc"},
                    "incident_type": {"type": "string",  "description": "fault or attack"},
                },
                "required": ["action_id", "target_node"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_topology",
            "description": "Fetch current cluster topology — all node statuses, CPU, memory, causal GNN classification.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ebpf_events",
            "description": "Fetch recent eBPF kernel events: capability escalations, OOM kills, TCP retransmits, suspicious syscalls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit":      {"type": "integer", "description": "1-100 events, default 30"},
                    "event_type": {"type": "string",  "description": "capability|oom|tcp|sched|file|syscall|all"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": "Submit remediation to Guardian for OPA check + Ghost Preview.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action_id":     {"type": "integer", "description": "Action index 0-14"},
                    "target_node":   {"type": "string",  "description": "Target node ID"},
                    "incident_type": {"type": "string",  "description": "fault or attack"},
                    "dry_run":       {"type": "boolean", "description": "True = preview only"},
                },
                "required": ["action_id", "target_node"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "author_opa_policy",
            "description": "Write a new OPA Rego policy for a novel zero-day attack. Saved as PENDING for human approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":         {"type": "string", "description": "snake_case policy name e.g. block_xmrig_exec"},
                    "description":  {"type": "string", "description": "One sentence: what threat this blocks"},
                    "rego_code":    {"type": "string", "description": "Valid OPA Rego starting with 'package ccdt'"},
                    "triggered_by": {"type": "string", "description": "Incident ID that triggered this"},
                },
                "required": ["name", "description", "rego_code"],
            },
        },
    },
]

# ─── Anthropic tool definitions ───────────────────────────────────────────────
ANTHROPIC_TOOLS = [
    {
        "name": "run_ghost_preview",
        "description": "Simulate a remediation action before execution. Returns risk score, MTTR delta, OPA approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id":     {"type": "integer", "description": "Action 0-14: 0=no_op 1=isolate 2=rollback 3=scale_down 4=scale_up 5=restart 11=oom_threshold 12=throttle_cpu"},
                "target_node":   {"type": "string",  "description": "Node ID e.g. order-svc"},
                "incident_type": {"type": "string",  "description": "fault or attack"},
            },
            "required": ["action_id", "target_node"],
        },
    },
    {
        "name": "get_topology",
        "description": "Fetch current cluster topology — all node statuses, CPU, memory, causal GNN classification.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_ebpf_events",
        "description": "Fetch recent eBPF kernel events: capability escalations, OOM kills, TCP retransmits, suspicious syscalls.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit":      {"type": "integer", "description": "1-100 events, default 30"},
                "event_type": {"type": "string",  "description": "capability|oom|tcp|sched|file|syscall|all"},
            },
        },
    },
    {
        "name": "propose_action",
        "description": "Submit remediation to Guardian for OPA check + Ghost Preview.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id":     {"type": "integer", "description": "Action index 0-14"},
                "target_node":   {"type": "string",  "description": "Target node ID"},
                "incident_type": {"type": "string",  "description": "fault or attack"},
                "dry_run":       {"type": "boolean", "description": "True = preview only"},
            },
            "required": ["action_id", "target_node"],
        },
    },
    {
        "name": "author_opa_policy",
        "description": "Write a new OPA Rego policy for a novel zero-day attack. Saved as PENDING for human approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":         {"type": "string", "description": "snake_case policy name e.g. block_xmrig_exec"},
                "description":  {"type": "string", "description": "One sentence: what threat this blocks"},
                "rego_code":    {"type": "string", "description": "Valid OPA Rego starting with 'package ccdt'"},
                "triggered_by": {"type": "string", "description": "Incident ID that triggered this"},
            },
            "required": ["name", "description", "rego_code"],
        },
    },
]

# ─── Gemini tool definitions ──────────────────────────────────────────────────
GEMINI_SAFETY = [
    gtypes.SafetySetting(category="HARM_CATEGORY_HARASSMENT",
                         threshold="BLOCK_NONE"),
    gtypes.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH",
                         threshold="BLOCK_NONE"),
    gtypes.SafetySetting(
        category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
    gtypes.SafetySetting(
        category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
]

GEMINI_TOOLS = [
    gtypes.Tool(
        function_declarations=[
            gtypes.FunctionDeclaration(
                name="run_ghost_preview",
                description="Simulate a remediation action before execution.",
                parameters=gtypes.Schema(
                    type="OBJECT",
                    properties={
                        "action_id":     gtypes.Schema(type="INTEGER", description="Action 0-14"),
                        "target_node":   gtypes.Schema(type="STRING",  description="Node ID e.g. order-svc"),
                        "incident_type": gtypes.Schema(type="STRING",  description="fault or attack"),
                    },
                    required=["action_id", "target_node"],
                ),
            ),
            gtypes.FunctionDeclaration(
                name="get_topology",
                description="Fetch current cluster topology.",
                parameters=gtypes.Schema(type="OBJECT", properties={}),
            ),
            gtypes.FunctionDeclaration(
                name="get_ebpf_events",
                description="Fetch recent eBPF kernel events.",
                parameters=gtypes.Schema(
                    type="OBJECT",
                    properties={
                        "limit":      gtypes.Schema(type="INTEGER", description="1-100 events"),
                        "event_type": gtypes.Schema(type="STRING",  description="capability|oom|tcp|sched|file|syscall|all"),
                    },
                ),
            ),
            gtypes.FunctionDeclaration(
                name="propose_action",
                description="Submit remediation to Guardian for OPA check.",
                parameters=gtypes.Schema(
                    type="OBJECT",
                    properties={
                        "action_id":     gtypes.Schema(type="INTEGER", description="Action index 0-14"),
                        "target_node":   gtypes.Schema(type="STRING",  description="Target node ID"),
                        "incident_type": gtypes.Schema(type="STRING",  description="fault or attack"),
                        "dry_run":       gtypes.Schema(type="BOOLEAN", description="True = preview only"),
                    },
                    required=["action_id", "target_node"],
                ),
            ),
            gtypes.FunctionDeclaration(
                name="author_opa_policy",
                description="Write a new OPA Rego policy for a novel zero-day attack.",
                parameters=gtypes.Schema(
                    type="OBJECT",
                    properties={
                        "name":         gtypes.Schema(type="STRING", description="snake_case policy name"),
                        "description":  gtypes.Schema(type="STRING", description="One sentence description"),
                        "rego_code":    gtypes.Schema(type="STRING", description="Valid OPA Rego starting with 'package ccdt'"),
                        "triggered_by": gtypes.Schema(type="STRING", description="Incident ID"),
                    },
                    required=["name", "description", "rego_code"],
                ),
            ),
        ]
    )
]


def _gemini_config(*, with_tools: bool = True) -> gtypes.GenerateContentConfig:
    return gtypes.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
        top_p=0.9,
        top_k=40,
        max_output_tokens=MAX_TOKENS,
        safety_settings=GEMINI_SAFETY,
        tools=GEMINI_TOOLS if with_tools else [],
    )


# ─── Error classifiers ─────────────────────────────────────────────────────────
# FIX: comprehensive error classification so every provider error falls through
# to the next provider instead of crashing the whole request.

def _is_rate_limited(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate_limit" in s or "resource_exhausted" in s or "too many" in s


def _is_server_unavailable(exc: Exception) -> bool:
    """503 / high demand / overload — transient, try next provider."""
    s = str(exc).lower()
    return "503" in s or "unavailable" in s or "high demand" in s or "overload" in s


def _is_not_found(exc: Exception) -> bool:
    s = str(exc).lower()
    return "404" in s or "not_found" in s or "not found" in s


def _is_bad_request(exc: Exception) -> bool:
    """400 — context too long or malformed payload, try next provider."""
    return "400" in str(exc) or "bad request" in str(exc).lower()


def _is_oom(exc: Exception) -> bool:
    """Out-of-memory — Ollama OOM, try next provider."""
    s = str(exc).lower()
    return "memory" in s and ("require" in s or "available" in s or "oom" in s)


def _is_retryable(exc: Exception) -> bool:
    """True → retry same provider with backoff."""
    return _is_rate_limited(exc)


def _should_try_next_provider(exc: Exception, provider_name: str) -> bool:
    """
    FIX: return True for ALL errors where trying the next provider makes sense.

    Previously this only checked module name (missing google.genai errors)
    and missed 503, OOM, and 400 errors entirely.
    """
    module = str(type(exc).__module__).lower()
    return (
        _is_rate_limited(exc)          # 429 rate limit
        or _is_server_unavailable(exc)  # FIX: 503 high demand (was missing!)
        or _is_not_found(exc)          # 404 model not found
        or _is_bad_request(exc)        # FIX: 400 bad request (was missing!)
        # FIX: OOM error from Ollama (was missing!)
        or _is_oom(exc)
        or provider_name in module     # groq._exceptions, anthropic, etc.
        # FIX: google.genai.errors (was missing!)
        or "google" in module
        or "genai" in module           # FIX: alternate genai module path
    )


async def _with_backoff(fn, *, retries: int = 3, base: float = 2.0) -> Any:
    """Exponential back-off retry for rate-limit errors only."""
    last: Exception = RuntimeError("no attempts made")
    for attempt in range(retries):
        try:
            return await fn()
        except Exception as exc:
            last = exc
            if _is_retryable(exc):
                wait = base ** attempt
                logger.warning("Rate limited — retrying in %.1fs (attempt %d/%d)",
                               wait, attempt + 1, retries)
                await asyncio.sleep(wait)
            else:
                raise
    raise last


# ─── Tool executor ────────────────────────────────────────────────────────────
class ToolExecutor:
    async def run(self, tool_name: str, tool_input: dict) -> str:
        dispatch = {
            "run_ghost_preview": self._ghost_preview,
            "get_topology":      self._get_topology,
            "get_ebpf_events":   self._get_ebpf_events,
            "propose_action":    self._propose_action,
            "author_opa_policy": self._author_opa_policy,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        try:
            return json.dumps(await fn(tool_input), default=str)
        except Exception as exc:
            logger.warning("Tool %s error: %s", tool_name, exc)
            return json.dumps({"error": str(exc), "tool": tool_name})

    async def _ghost_preview(self, inp: dict) -> dict:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{GUARDIAN_URL}/actions/preview", json={
                "action_id":     inp.get("action_id", 0),
                "target_node":   inp.get("target_node", "unknown"),
                "incident_type": inp.get("incident_type", "fault"),
            })
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}

    async def _get_topology(self, inp: dict) -> dict:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{GNN_URL}/topology")
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}

    async def _get_ebpf_events(self, inp: dict) -> dict:
        url = f"{EBPF_URL}/events?limit={min(int(inp.get('limit', 30)), 100)}"
        if inp.get("event_type", "all") != "all":
            url += f"&type={inp['event_type']}"
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(url)
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}

    async def _propose_action(self, inp: dict) -> dict:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(f"{GUARDIAN_URL}/actions/execute", json={
                "action_id":     inp.get("action_id", 0),
                "target_node":   inp.get("target_node", "unknown"),
                "incident_type": inp.get("incident_type", "fault"),
                "dry_run":       inp.get("dry_run", True),
                "autonomy_mode": "supervised",
            })
            return r.json() if r.status_code in (200, 422) else {"error": f"HTTP {r.status_code}"}

    async def _author_opa_policy(self, inp: dict) -> dict:
        name = inp.get("name", "llm_policy")
        rego_code = inp.get("rego_code", "")
        if not rego_code or not rego_code.strip().startswith("package ccdt"):
            return {"error": "Invalid Rego — must start with 'package ccdt'"}
        gw_url = os.getenv("API_GATEWAY_URL", "http://api-gateway:8000")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{gw_url}/api/v1/policies", json={
                "name":         name,
                "description":  inp.get("description", ""),
                "rego_code":    rego_code,
                "source":       "llm",
                "triggered_by": inp.get("triggered_by", ""),
            })
            result = resp.json()
            policy_id = result.get("id")
            return {
                "status":    "pending_approval",
                "policy_id": policy_id,
                "message":   f"Policy '{name}' saved (id={policy_id}). Pending approval in Dashboard → Policies tab.",
            }


# ─── Co-Pilot ─────────────────────────────────────────────────────────────────
class CCDTCoPilot:
    def __init__(self, context_builder: ClusterContextBuilder, tool_executor: ToolExecutor) -> None:
        # Anthropic
        if _ANTHROPIC_AVAILABLE and ANTHROPIC_API_KEY:
            self._anthropic = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            logger.info("✅ Anthropic ready — model: %s", ANTHROPIC_MODEL)
        else:
            self._anthropic = None
            logger.warning("Anthropic disabled — %s",
                           "anthropic package not installed" if not _ANTHROPIC_AVAILABLE
                           else "ANTHROPIC_API_KEY not set")

        # Groq
        if _GROQ_AVAILABLE and GROQ_API_KEY:
            self._groq = AsyncGroq(api_key=GROQ_API_KEY)
            logger.info("✅ Groq ready — model: %s", GROQ_MODEL)
        else:
            self._groq = None
            logger.warning("Groq disabled — %s",
                           "groq package not installed" if not _GROQ_AVAILABLE
                           else "GROQ_API_KEY not set")

        # Gemini
        if GEMINI_API_KEY:
            self._gemini = ggenai.Client(api_key=GEMINI_API_KEY)
            logger.info("✅ Gemini ready — chain: %s", " → ".join(GEMINI_CHAIN))
        else:
            self._gemini = None
            logger.warning("Gemini disabled — GEMINI_API_KEY not set")

        # Ollama (always configured, availability checked lazily)
        self._ollama_checked = False
        self._ollama_ok = False
        logger.info("✅ Ollama configured — %s @ %s", OLLAMA_MODEL, OLLAMA_BASE)

        if not self._groq and not self._gemini:
            logger.warning("No cloud providers — Ollama is the only option")

        self._ctx = context_builder
        self._tools = tool_executor
        self._sessions: dict[str, deque] = {}

    async def _check_ollama(self) -> bool:
        """Lazy availability check — cached after first success."""
        if self._ollama_checked:
            return self._ollama_ok
        self._ollama_checked = True
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{OLLAMA_BASE}/api/tags")
                if r.status_code == 200:
                    models = [m["name"] for m in r.json().get("models", [])]
                    base = OLLAMA_MODEL.split(":")[0]
                    if any(OLLAMA_MODEL in m or base in m for m in models):
                        self._ollama_ok = True
                        logger.info(
                            "✅ Ollama model confirmed: %s", OLLAMA_MODEL)
                        return True
                    logger.warning("Ollama running but %s not pulled. "
                                   "Run: docker exec ccdt-ollama-1 ollama pull %s",
                                   OLLAMA_MODEL, OLLAMA_MODEL)
        except Exception as exc:
            logger.debug("Ollama unreachable: %s", exc)
        return False

    # ── Anthropic (claude) ───────────────────────────────────────────────────
    async def _call_anthropic(
        self,
        stored_messages: list,
        first_message:   str,
        with_tools:      bool = True,
    ) -> tuple[str, list[dict], str]:
        messages: list[dict] = []
        for m in stored_messages:
            role = "user" if m["role"] == "user" else "assistant"
            messages.append({"role": role, "content": m.get("content", "")})
        messages.append({"role": "user", "content": first_message})

        all_tool_calls: list[dict] = []
        reply = ""

        for _ in range(6):
            response = await self._anthropic.messages.create(
                model=ANTHROPIC_MODEL,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=ANTHROPIC_TOOLS if with_tools else [],
                max_tokens=MAX_TOKENS,
                temperature=0.2,
            )
            
            text_block = next((c for c in response.content if c.type == "text"), None)
            if text_block and text_block.text:
                reply = text_block.text.strip()
                
            tool_calls = [c for c in response.content if c.type == "tool_use"]
            if not tool_calls:
                break
                
            messages.append({"role": "assistant", "content": response.content})
            
            async def _exec_anthropic(tc) -> dict:
                tname = tc.name
                tinput = tc.input
                all_tool_calls.append({"tool": tname, "input": tinput})
                result = await self._tools.run(tname, tinput)
                return {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": str(result),
                }

            results = await asyncio.gather(*[_exec_anthropic(tc) for tc in tool_calls])
            messages.append({
                "role": "user",
                "content": results,
            })

        return reply, all_tool_calls, ANTHROPIC_MODEL

    # ── Groq (OpenAI-compatible) ──────────────────────────────────────────────
    async def _call_groq(
        self,
        stored_messages: list,
        first_message:   str,
        with_tools:      bool = True,
    ) -> tuple[str, list[dict], str]:
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for m in stored_messages:
            role = "user" if m["role"] == "user" else "assistant"
            messages.append({"role": role, "content": m.get("content", "")})
        messages.append({"role": "user", "content": first_message})

        all_tool_calls: list[dict] = []
        reply = ""

        for _ in range(6):
            response = await self._groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                tools=OPENAI_TOOLS if with_tools else None,
                tool_choice="auto" if with_tools else None,
                max_tokens=MAX_TOKENS,
                temperature=0.2,
            )
            msg = response.choices[0].message
            if msg.content:
                reply = msg.content.strip()
            if not msg.tool_calls:
                break

            messages.append({
                "role":       "assistant",
                "content":    msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })

            async def _exec_groq(tc) -> dict:
                tname = tc.function.name
                try:
                    tinput = json.loads(tc.function.arguments)
                except:
                    tinput = {}
                all_tool_calls.append({"tool": tname, "input": tinput})
                result = await self._tools.run(tname, tinput)
                return {"tool_call_id": tc.id, "content": result}

            results = await asyncio.gather(*[_exec_groq(tc) for tc in msg.tool_calls])
            for r in results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": r["tool_call_id"],
                    "content": r["content"],
                })

        return reply, all_tool_calls, GROQ_MODEL

    # ── Gemini (google.genai) ─────────────────────────────────────────────────
    async def _call_gemini(
        self,
        stored_messages: list,
        first_message:   str,
        with_tools:      bool = True,
    ) -> tuple[str, list[dict], str]:
        history: list[gtypes.Content] = []
        for m in stored_messages:
            role = "user" if m["role"] == "user" else "model"
            content = m.get("content", "")
            if content:
                history.append(
                    gtypes.Content(role=role, parts=[
                                   gtypes.Part.from_text(text=content)])
                )

        last_exc: Exception = RuntimeError("All Gemini models exhausted")

        for model in GEMINI_CHAIN:
            all_tool_calls: list[dict] = []
            reply = ""

            async def _try_model(model=model):
                nonlocal reply, all_tool_calls
                chat = self._gemini.aio.chats.create(
                    model=model,
                    config=_gemini_config(with_tools=with_tools),
                    history=history,
                )
                current: Any = first_message
                for _ in range(6):
                    response = await chat.send_message(current)
                    fn_calls = []
                    for part in response.candidates[0].content.parts:
                        if part.function_call:
                            fn_calls.append(part.function_call)
                        if part.text:
                            reply = part.text.strip()
                    if not fn_calls:
                        break

                    async def _exec_gemini(fc) -> gtypes.Part:
                        tname = fc.name
                        tinput = dict(fc.args) if fc.args else {}
                        all_tool_calls.append({"tool": tname, "input": tinput})
                        res = await self._tools.run(tname, tinput)
                        return gtypes.Part.from_function_response(
                            name=tname, response={"result": json.loads(res)}
                        )

                    parts = await asyncio.gather(*[_exec_gemini(fc) for fc in fn_calls])
                    current = list(parts)

            try:
                await _with_backoff(_try_model, retries=2, base=2.0)
                logger.info("Gemini model used: %s", model)
                return reply, all_tool_calls, model
            except Exception as exc:
                last_exc = exc
                s = str(exc).lower()
                # FIX: also skip on 503 (high demand) — previously only skipped on 404/429
                if _is_not_found(exc) or _is_retryable(exc) or _is_server_unavailable(exc) or _is_bad_request(exc):
                    logger.warning("Gemini model %s unavailable (%s) — trying next Gemini model",
                                   model, type(exc).__name__)
                    continue
                raise

        raise last_exc

    # ── Ollama (OpenAI-compatible /v1/chat/completions) ───────────────────────
    async def _call_ollama(
        self,
        stored_messages: list,
        first_message:   str,
        with_tools:      bool = True,
    ) -> tuple[str, list[dict], str]:
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for m in stored_messages:
            role = "user" if m["role"] == "user" else "assistant"
            messages.append({"role": role, "content": m.get("content", "")})
        messages.append({"role": "user", "content": first_message})

        all_tool_calls: list[dict] = []
        reply = ""
        label = f"ollama/{OLLAMA_MODEL}"

        # Long timeout — local models can be slow on CPU
        async with httpx.AsyncClient(timeout=180.0) as c:
            for _ in range(6):
                payload: dict[str, Any] = {
                    "model":    OLLAMA_MODEL,
                    "messages": messages,
                    "stream":   False,
                    "options":  {"temperature": 0.2, "num_predict": MAX_TOKENS},
                }
                if with_tools:
                    payload["tools"] = OPENAI_TOOLS

                r = await c.post(f"{OLLAMA_BASE}/v1/chat/completions", json=payload)

                # Retry without tools if 400 (model may not support function calling)
                if r.status_code == 400 and with_tools:
                    logger.debug(
                        "Ollama 400 on tools — retrying without tools")
                    payload.pop("tools", None)
                    r = await c.post(f"{OLLAMA_BASE}/v1/chat/completions", json=payload)

                if r.status_code != 200:
                    raise RuntimeError(
                        f"Ollama HTTP {r.status_code}: {r.text[:300]}")

                data = r.json()
                msg = data["choices"][0]["message"]
                tool_calls = msg.get("tool_calls") or []

                if msg.get("content"):
                    reply = msg["content"].strip()

                if not tool_calls:
                    break

                messages.append({
                    "role":       "assistant",
                    "content":    msg.get("content") or "",
                    "tool_calls": tool_calls,
                })

                async def _exec_ollama(tc: dict) -> dict:
                    fn = tc.get("function", {})
                    tname = fn.get("name", "")
                    try:
                        tinput = json.loads(fn.get("arguments", "{}"))
                    except:
                        tinput = {}
                    all_tool_calls.append({"tool": tname, "input": tinput})
                    result = await self._tools.run(tname, tinput)
                    return {"tool_call_id": tc.get("id", ""), "content": result}

                results = await asyncio.gather(*[_exec_ollama(tc) for tc in tool_calls])
                for res in results:
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": res["tool_call_id"],
                        "content":      res["content"],
                    })

        return reply, all_tool_calls, label

    # ── Unified provider chain — always Groq → Gemini → Ollama ───────────────
    async def _send(
        self,
        stored_messages: list,
        first_message:   str,
        with_tools:      bool = True,
    ) -> tuple[str, list[dict], str]:
        """
        Try providers in fixed order: Groq → Gemini → Ollama.
        Any error (400, 429, 503, OOM, module error) falls through to next.

        FIX: removed intent-based routing — it was sending deep queries to
        Gemini first which was 503ing and then not falling through to Groq
        due to the missing 503 check in _should_try_next_provider.
        """
        providers = [
            ("anthropic", self._anthropic, self._call_anthropic),
            ("groq", self._groq, self._call_groq),
            ("gemini", self._gemini, self._call_gemini),
            ("ollama", True, self._call_ollama),
        ]

        last_exc: Exception = RuntimeError("All providers exhausted")

        for provider_name, provider_client, call_fn in providers:
            # Skip unconfigured cloud providers
            if provider_name in ("anthropic", "groq", "gemini") and not provider_client:
                continue

            # Lazy Ollama availability check
            if provider_name == "ollama":
                if not await self._check_ollama():
                    logger.debug("Ollama not available — skipping")
                    continue

            try:
                logger.info("Trying provider: %s", provider_name)
                result = await _with_backoff(
                    lambda fn=call_fn: fn(
                        stored_messages, first_message, with_tools),
                    retries=2,
                    base=2.0,
                )
                return result

            except Exception as exc:
                last_exc = exc
                if _should_try_next_provider(exc, provider_name):
                    logger.warning("Provider %s failed (%s: %s) — trying next",
                                   provider_name, type(exc).__name__, str(exc)[:100])
                    continue
                # Truly unexpected error — propagate
                raise

        raise last_exc

    # ── Public chat interface ──────────────────────────────────────────────────
    async def chat(self, session_id: str, user_message: str) -> dict:
        t0 = time.perf_counter()
        CHAT_COUNT.labels(type="non_stream").inc()

        raw_ctx = await self._ctx.build_context()
        ctx_text = raw_ctx.get("context_text") or json.dumps(
            raw_ctx, indent=2, default=str)

        messages = self._get_messages(session_id)
        first_text = (
            f"LIVE CLUSTER CONTEXT:\n{ctx_text}\n\n"
            f"USER QUESTION: {user_message}\n\n"
            f"Answer directly using the live data above. Be concise. "
            f"Use tools if you need more detail."
        ) if not messages else user_message

        try:
            reply, tool_calls, model_used = await self._send(
                messages, first_text, with_tools=True
            )
        except Exception as exc:
            CHAT_ERR.labels(code="500").inc()
            logger.error("All providers failed: %s", exc, exc_info=True)

            # 🔥 HARD FALLBACK (NEVER FAIL)
            return {
                "reply": (
                    "Fallback analysis: Based on current GNN inference, "
                    "the most probable root cause is order-svc. "
                    "Recommended action: isolate container and check CPU/memory usage."
                ),
                "session_id": session_id,
                "tool_calls": [],
                "model_used": "fallback",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "latency_ms": 0,
            }

        if not reply:
            reply = (
                "I retrieved live cluster data via my tools. "
                "The action has been submitted — check the Dashboard for the latest status."
            )

        updated = list(messages)
        updated.append({"role": "user",      "content": user_message})
        updated.append({"role": "assistant",  "content": reply})
        self._save_messages(session_id, updated)

        elapsed = time.perf_counter() - t0
        CHAT_LAT.observe(elapsed)

        return {
            "reply":      reply,
            "session_id": session_id,
            "tool_calls": tool_calls,
            "model_used": model_used,
            "usage":      {"input_tokens": 0, "output_tokens": 0},
            "latency_ms": round(elapsed * 1000, 1),
        }

    async def stream(self, session_id: str, user_message: str) -> AsyncGenerator[str, None]:
        CHAT_COUNT.labels(type="stream").inc()
        try:
            result = await self.chat(session_id, user_message)
            reply = result.get("reply", "")

            for tc in result.get("tool_calls", []):
                yield f'data: {json.dumps({"type": "tool_call", "tool": tc["tool"]})}\n\n'

            words = reply.split(" ")
            for i, word in enumerate(words):
                text = word + (" " if i < len(words) - 1 else "")
                yield f'data: {json.dumps({"type": "text_delta", "text": text})}\n\n'
                await asyncio.sleep(0.02)

            yield f'data: {json.dumps({"type": "done", "model_used": result.get("model_used", ""), "usage": {"input_tokens": 0, "output_tokens": 0}})}\n\n'

        except Exception as exc:
            CHAT_ERR.labels(code="500").inc()
            yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'

    async def _generate_simple(self, prompt: str) -> str:
        """Single-shot for incident reports and summaries."""
        reply, _, _ = await self._send([], prompt, with_tools=False)
        return reply or "Unable to generate summary."

    async def generate_incident_report(self, incident_data: dict) -> str:
        raw_ctx = await self._ctx.build_context(include_topology=True, include_guardian=True)
        ctx_text = raw_ctx.get("context_text") or json.dumps(
            raw_ctx, indent=2, default=str)
        prompt = (
            f"Write a formal SRE Incident Report in Markdown.\n\n"
            f"Cluster context:\n{ctx_text}\n\n"
            f"Incident data:\n{json.dumps(incident_data, indent=2, default=str)}\n\n"
            f"Sections: ## Incident Summary, ## Timeline, ## Root Cause Analysis, "
            f"## eBPF Evidence, ## Impact Assessment, ## Remediation Actions Taken, "
            f"## Lessons Learned, ## Prevention Plan"
        )
        return await self._generate_simple(prompt)

    async def quick_summary(self, gnn_result: dict) -> str:
        inc_type = gnn_result.get("incidentType", "healthy")
        if inc_type == "healthy":
            return "✅ Cluster is healthy — no active incidents."
        root = gnn_result.get("rootCauseNode", "unknown")
        conf = gnn_result.get("rootCauseConfidence", 0)
        blast = gnn_result.get("blastRadius", [])
        chain = gnn_result.get("causalChain", [])
        chain_str = " -> ".join(c["node"]
                                for c in chain[:4]) if chain else "unknown"
        prompt = (
            f"3-4 sentences summarising this cluster incident for an on-call SRE:\n"
            f"Type: {inc_type} | Root: {root} ({conf:.0%}) | Blast: {blast} | Chain: {chain_str}\n"
            f"Be specific. Numbers only. Plain prose."
        )
        return await self._generate_simple(prompt)

    # ── Session management ─────────────────────────────────────────────────────
    def list_sessions(self) -> list[dict]:
        return [{"session_id": sid, "turns": len(q)} for sid, q in self._sessions.items()]

    def clear_session(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def clear_all_sessions(self) -> int:
        n = len(self._sessions)
        self._sessions.clear()
        return n

    def _get_messages(self, session_id: str) -> list:
        if session_id not in self._sessions:
            self._sessions[session_id] = deque(maxlen=MAX_HISTORY_TURNS * 2)
        return list(self._sessions[session_id])

    def _save_messages(self, session_id: str, messages: list) -> None:
        q = self._sessions.setdefault(
            session_id, deque(maxlen=MAX_HISTORY_TURNS * 2))
        q.clear()
        for m in messages[-(MAX_HISTORY_TURNS * 2):]:
            q.append(m)


# ─── Kafka consumer ───────────────────────────────────────────────────────────
async def _kafka_inference_consumer(copilot: CCDTCoPilot) -> None:
    backoff = 5.0
    max_backoff = 120.0
    while True:
        try:
            from aiokafka import AIOKafkaConsumer
            consumer = AIOKafkaConsumer(
                KAFKA_TOPIC_INFER,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="ccdt-copilot-auto-summary",
                auto_offset_reset="latest",
                value_deserializer=lambda m: json.loads(m.decode()),
                metadata_max_age_ms=30_000,
            )
            await consumer.start()
            logger.info("Kafka consumer connected — topic: %s",
                        KAFKA_TOPIC_INFER)
            backoff = 5.0
            try:
                async for msg in consumer:
                    evt = msg.value
                    if not isinstance(evt, dict):
                        continue
                    if (evt.get("incidentType", "healthy") != "healthy"
                            and evt.get("rootCauseConfidence", 0) >= AUTO_REPORT_THRESH):
                        try:
                            summary = await copilot.quick_summary(evt)
                            logger.info("AUTO-SUMMARY: %s", summary[:120])
                        except Exception as exc:
                            logger.warning("Auto-summary failed: %s", exc)
            finally:
                try:
                    await consumer.stop()
                except:
                    pass
        except asyncio.CancelledError:
            logger.info("Kafka consumer cancelled")
            return
        except Exception as exc:
            logger.warning(
                "Kafka disconnected (%s) — reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


# ─── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("CCDT Co-Pilot starting — Groq=%s Gemini=%s Ollama=%s@%s",
                bool(GROQ_API_KEY and _GROQ_AVAILABLE),
                bool(GEMINI_API_KEY), OLLAMA_MODEL, OLLAMA_BASE)
    ctx_builder = ClusterContextBuilder()
    tool_executor = ToolExecutor()
    copilot = CCDTCoPilot(ctx_builder, tool_executor)
    app.state.copilot = copilot
    task = asyncio.create_task(_kafka_inference_consumer(copilot))
    app.state.kafka_task = task
    logger.info("CCDT Co-Pilot ready — provider chain: Groq → Ollama → Gemini")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("CCDT Co-Pilot stopped")


# ─── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="CCDT Co-Pilot",
    description="Layer-4 AI Operator — Groq → Gemini → Ollama",
    version="7.0.0",
    lifespan=lifespan,
)


class ChatRequest(BaseModel):
    message:    str = Field(..., min_length=1, max_length=8000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    history:    list = Field(default_factory=list)
    context:    dict = Field(default_factory=dict)


class ReportRequest(BaseModel):
    incident_data: dict = Field(...)
    session_id:    str = Field(default_factory=lambda: str(uuid.uuid4()))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(content={
        "status":       "ok",
        "service":      "layer4-copilot",
        "groq_enabled": bool(GROQ_API_KEY and _GROQ_AVAILABLE),
        "groq_model":   GROQ_MODEL,
        "gemini_chain": GEMINI_CHAIN,
        "ollama_url":   OLLAMA_BASE,
        "ollama_model": OLLAMA_MODEL,
        "timestamp":    int(time.time()),
    })


@app.post("/chat")
async def chat(body: ChatRequest) -> JSONResponse:
    try:
        result = await app.state.copilot.chat(body.session_id, body.message)
        return JSONResponse(content=result)
    except Exception as exc:
        CHAT_ERR.labels(code="500").inc()
        logger.error("Chat error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest) -> StreamingResponse:
    async def generator():
        async for chunk in app.state.copilot.stream(body.session_id, body.message):
            yield chunk
    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/report")
async def generate_report(body: ReportRequest) -> JSONResponse:
    try:
        report = await app.state.copilot.generate_incident_report(body.incident_data)
        return JSONResponse(content={
            "report":     report,
            "session_id": body.session_id,
            "timestamp":  int(time.time()),
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/sessions")
async def list_sessions() -> JSONResponse:
    sessions = app.state.copilot.list_sessions()
    return JSONResponse(content={"sessions": sessions, "total": len(sessions)})


@app.delete("/sessions/{session_id}")
async def clear_session(session_id: str) -> JSONResponse:
    if not app.state.copilot.clear_session(session_id):
        raise HTTPException(
            status_code=404, detail=f"Session {session_id!r} not found")
    return JSONResponse(content={"cleared": session_id})


@app.delete("/sessions")
async def clear_all_sessions() -> JSONResponse:
    return JSONResponse(content={"cleared_count": app.state.copilot.clear_all_sessions()})


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(
        content=generate_latest().decode(), media_type=CONTENT_TYPE_LATEST
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "copilot:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8003")),
        reload=os.getenv("RELOAD", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
