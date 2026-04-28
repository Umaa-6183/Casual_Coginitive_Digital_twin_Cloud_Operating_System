"""
CCDT Layer-4 Co-Pilot — FastAPI Server + Google GenAI SDK (google.genai)
Uses the NEW google.genai package. requirements.txt must have: google-genai>=1.0.0

Model fallback chain (in order):
  gemini-2.5-flash  →  gemini-2.0-flash  →  gemini-2.0-flash-lite
If the primary model is 429-rate-limited or 404-not-found, the next model
in the chain is tried automatically. Each attempt uses exponential backoff.
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

from google import genai
from google.genai import types as gtypes

from context_builder import ClusterContextBuilder

logger = logging.getLogger("ccdt.copilot")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

# ─── Configuration ─────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Primary model from env — strip accidental "models/" prefix
_raw_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PRIMARY_MODEL = _raw_model[len(
    "models/"):] if _raw_model.startswith("models/") else _raw_model

# Fallback chain — tried in order if primary returns 429 or 404
_FALLBACK_CHAIN = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
# Build final model list: primary first, then the rest of the chain (deduped)
MODEL_CHAIN: list[str] = [PRIMARY_MODEL] + [
    m for m in _FALLBACK_CHAIN if m != PRIMARY_MODEL
]

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2048"))
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
You are CCDT Co-Pilot, the AI operator for a Level-4 Autonomous AIOps platform.

You have access to live cluster telemetry tools. Use them to answer questions accurately.

RULES:
- Always answer DIRECTLY using real data from your tools
- For specific questions (blast radius, root cause, MTTR) give a DIRECT answer first
- Use bullet points for lists, arrows for causal chains: A -> B -> C
- Quantify everything: confidence %, MTTR minutes, node counts
- For attacks: recommend isolate_container before rollback
- Never recommend drain_node autonomously

CAPABILITIES:
1. Root cause analysis using GNN causal chains
2. Fault vs attack classification
3. Counterfactual "what if" analysis via Ghost Preview
4. Action proposals with OPA safety check
5. Novel zero-day policy authoring (author_opa_policy tool)
"""

# ─── Safety settings ─────────────────────────────────────────────────────────
SAFETY_SETTINGS = [
    gtypes.SafetySetting(category="HARM_CATEGORY_HARASSMENT",
                         threshold="BLOCK_NONE"),
    gtypes.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH",
                         threshold="BLOCK_NONE"),
    gtypes.SafetySetting(
        category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
    gtypes.SafetySetting(
        category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
]

# ─── Tool definitions ─────────────────────────────────────────────────────────
TOOLS = [
    gtypes.Tool(
        function_declarations=[
            gtypes.FunctionDeclaration(
                name="run_ghost_preview",
                description="Simulate a remediation action before execution. Returns risk score, MTTR delta, OPA approval.",
                parameters=gtypes.Schema(
                    type="OBJECT",
                    properties={
                        "action_id":     gtypes.Schema(type="INTEGER",
                                                       description="Action 0-14: 0=no_op 1=isolate 2=rollback "
                                                       "3=scale_down 4=scale_up 5=restart "
                                                       "11=oom_threshold 12=throttle_cpu"),
                        "target_node":   gtypes.Schema(type="STRING",
                                                       description="Node ID e.g. order-svc"),
                        "incident_type": gtypes.Schema(type="STRING",
                                                       description="fault or attack"),
                    },
                    required=["action_id", "target_node"],
                ),
            ),
            gtypes.FunctionDeclaration(
                name="get_topology",
                description="Fetch current cluster topology — all node statuses, CPU, memory, causal GNN classification.",
                parameters=gtypes.Schema(type="OBJECT", properties={}),
            ),
            gtypes.FunctionDeclaration(
                name="get_ebpf_events",
                description="Fetch recent eBPF kernel events: capability escalations, OOM kills, TCP retransmits, suspicious syscalls.",
                parameters=gtypes.Schema(
                    type="OBJECT",
                    properties={
                        "limit":      gtypes.Schema(type="INTEGER",
                                                    description="1-100 events, default 30"),
                        "event_type": gtypes.Schema(type="STRING",
                                                    description="capability|oom|tcp|sched|file|syscall|all"),
                    },
                ),
            ),
            gtypes.FunctionDeclaration(
                name="propose_action",
                description="Submit remediation to Guardian for OPA check + Ghost Preview. Does NOT execute unless autonomy=full-auto.",
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
                description="Write a new OPA Rego policy for a novel zero-day attack. Saved as PENDING for human approval.",
                parameters=gtypes.Schema(
                    type="OBJECT",
                    properties={
                        "name":         gtypes.Schema(type="STRING",
                                                      description="snake_case policy name e.g. block_xmrig_exec"),
                        "description":  gtypes.Schema(type="STRING",
                                                      description="One sentence: what threat this blocks"),
                        "rego_code":    gtypes.Schema(type="STRING",
                                                      description="Valid OPA Rego starting with 'package ccdt'"),
                        "triggered_by": gtypes.Schema(type="STRING",
                                                      description="Incident ID that triggered this"),
                    },
                    required=["name", "description", "rego_code"],
                ),
            ),
        ]
    )
]


def _make_config(*, with_tools: bool = True) -> gtypes.GenerateContentConfig:
    return gtypes.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
        top_p=0.9,
        top_k=40,
        max_output_tokens=MAX_TOKENS,
        safety_settings=SAFETY_SETTINGS,
        tools=TOOLS if with_tools else [],
    )


def _is_retryable(exc: Exception) -> bool:
    """Return True if the error is a transient 429 rate-limit."""
    return "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)


def _is_not_found(exc: Exception) -> bool:
    """Return True if the model name is invalid (404 / NOT_FOUND)."""
    return "404" in str(exc) or "NOT_FOUND" in str(exc)


async def _call_with_retry(
    fn,
    *,
    max_retries: int = 3,
    base_delay:  float = 2.0,
) -> Any:
    """
    Call an async callable with exponential back-off on 429.
    Raises the last exception if all retries are exhausted.
    """
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if _is_retryable(exc):
                wait = base_delay ** attempt   # 1s, 2s, 4s
                logger.warning(
                    "Rate-limited (429) — retrying in %.1fs (attempt %d/%d)",
                    wait, attempt + 1, max_retries,
                )
                await asyncio.sleep(wait)
            else:
                raise  # non-retryable — propagate immediately
    raise last_exc


# ─── Tool executor ───────────────────────────────────────────────────────────────
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
                "message":   (
                    f"Policy '{name}' saved (id={policy_id}). "
                    "Pending approval in Dashboard → Policies tab."
                ),
            }


# ─── Co-Pilot ────────────────────────────────────────────────────────────────────
class CCDTCoPilot:
    def __init__(self, context_builder: ClusterContextBuilder, tool_executor: ToolExecutor) -> None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY environment variable is not set")
        self._client = genai.Client(api_key=GEMINI_API_KEY)
        self._ctx = context_builder
        self._tools = tool_executor
        self._sessions: dict[str, deque] = {}
        logger.info("Model chain: %s", " → ".join(MODEL_CHAIN))

    def _to_sdk_history(self, messages: list) -> list[gtypes.Content]:
        history = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            content = m.get("content", "")
            if isinstance(content, str) and content:
                history.append(
                    gtypes.Content(role=role, parts=[
                                   gtypes.Part.from_text(text=content)])
                )
        return history

    async def _send_with_fallback(
        self,
        history: list[gtypes.Content],
        first_message: Any,
        with_tools: bool = True,
    ) -> tuple[str, list[dict], str]:
        """
        Try each model in MODEL_CHAIN. For each model, attempt up to 3 retries
        on 429. On 404 (model not found), immediately try the next model.
        Returns (reply_text, tool_calls_list, model_used).
        """
        last_exc: Exception = RuntimeError("No models available")

        for model in MODEL_CHAIN:
            all_tool_calls: list[dict] = []
            reply = ""

            async def _try_model():
                nonlocal reply, all_tool_calls

                chat_session = self._client.aio.chats.create(
                    model=model,
                    config=_make_config(with_tools=with_tools),
                    history=history,
                )

                current: Any = first_message
                for _ in range(6):  # max 6 tool-use rounds
                    response = await chat_session.send_message(current)

                    fn_calls: list = []
                    for part in response.candidates[0].content.parts:
                        if part.function_call:
                            fn_calls.append(part.function_call)
                        if part.text:
                            reply = part.text.strip()

                    if not fn_calls:
                        break

                    async def _exec(fc) -> gtypes.Part:
                        tname = fc.name
                        tinput = dict(fc.args) if fc.args else {}
                        all_tool_calls.append({"tool": tname, "input": tinput})
                        res_str = await self._tools.run(tname, tinput)
                        return gtypes.Part.from_function_response(
                            name=tname,
                            response={"result": json.loads(res_str)},
                        )

                    parts = await asyncio.gather(*[_exec(fc) for fc in fn_calls])
                    current = list(parts)

            try:
                await _call_with_retry(_try_model, max_retries=3, base_delay=2.0)
                logger.info("Model used: %s", model)
                return reply, all_tool_calls, model

            except Exception as exc:
                last_exc = exc
                if _is_not_found(exc):
                    logger.warning(
                        "Model %s not found — trying next in chain", model)
                    continue
                elif _is_retryable(exc):
                    logger.warning(
                        "Model %s exhausted after retries — trying next in chain", model)
                    continue
                else:
                    raise  # unexpected error — don't swallow it

        raise last_exc  # all models failed

    async def chat(self, session_id: str, user_message: str) -> dict:
        t0 = time.perf_counter()
        CHAT_COUNT.labels(type="non_stream").inc()

        raw_ctx = await self._ctx.build_context()
        ctx_text = raw_ctx.get("context_text") or json.dumps(
            raw_ctx, indent=2, default=str)

        messages = self._get_messages(session_id)
        history = self._to_sdk_history(messages)

        first_text = (
            f"LIVE CLUSTER CONTEXT:\n{ctx_text}\n\n"
            f"USER QUESTION: {user_message}\n\n"
            f"Answer directly using the live data above. "
            f"Be concise. Use the tools if you need more detail."
        ) if not history else user_message

        try:
            reply, all_tool_calls, model_used = await self._send_with_fallback(
                history, first_text, with_tools=True
            )
        except Exception as exc:
            CHAT_ERR.labels(code="500").inc()
            logger.error("All models failed: %s", exc, exc_info=True)
            raise

        if not reply:
            reply = (
                "I retrieved live cluster data via my tools. "
                "The action has been submitted — check the Dashboard for the latest status."
            )

        msgs_updated = list(messages)
        msgs_updated.append({"role": "user",      "content": user_message})
        msgs_updated.append({"role": "assistant",  "content": reply})
        self._save_messages(session_id, msgs_updated)

        elapsed = time.perf_counter() - t0
        CHAT_LAT.observe(elapsed)

        return {
            "reply":      reply,
            "session_id": session_id,
            "tool_calls": all_tool_calls,
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

            yield f'data: {json.dumps({"type": "done", "usage": {"input_tokens": 0, "output_tokens": 0}})}\n\n'

        except Exception as exc:
            CHAT_ERR.labels(code="500").inc()
            yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'

    async def _generate_simple(self, prompt: str) -> str:
        """Single-shot generation with model fallback and retry (no tools, no history)."""
        last_exc: Exception = RuntimeError("No models available")
        for model in MODEL_CHAIN:
            async def _try():
                resp = await self._client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=_make_config(with_tools=False),
                )
                return resp.text
            try:
                return await _call_with_retry(_try, max_retries=3, base_delay=2.0)
            except Exception as exc:
                last_exc = exc
                if _is_not_found(exc) or _is_retryable(exc):
                    logger.warning(
                        "Model %s unavailable for simple generate — trying next", model)
                    continue
                raise
        raise last_exc

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
            session_id, deque(maxlen=MAX_HISTORY_TURNS * 2)
        )
        q.clear()
        for m in messages[-(MAX_HISTORY_TURNS * 2):]:
            q.append(m)


# ─── Kafka consumer — with backoff so it doesn't spam logs ───────────────────
async def _kafka_inference_consumer(copilot: CCDTCoPilot) -> None:
    """
    Consume GNN inference results and generate auto-summaries for high-confidence
    non-healthy incidents. Backs off exponentially if the topic doesn't exist yet,
    so the log isn't flooded while Kafka is initialising.
    """
    backoff = 5.0          # seconds between reconnect attempts
    max_backoff = 120.0    # cap at 2 minutes

    while True:
        try:
            from aiokafka import AIOKafkaConsumer
            consumer = AIOKafkaConsumer(
                KAFKA_TOPIC_INFER,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="ccdt-copilot-auto-summary",
                auto_offset_reset="latest",
                value_deserializer=lambda m: json.loads(m.decode()),
                # Silence per-poll metadata errors — we handle reconnect ourselves
                metadata_max_age_ms=30_000,
            )
            await consumer.start()
            logger.info("Kafka consumer connected — topic: %s",
                        KAFKA_TOPIC_INFER)
            backoff = 5.0  # reset on successful connect

            try:
                async for msg in consumer:
                    evt = msg.value
                    if not isinstance(evt, dict):
                        continue
                    if (
                        evt.get("incidentType", "healthy") != "healthy"
                        and evt.get("rootCauseConfidence", 0) >= AUTO_REPORT_THRESH
                    ):
                        try:
                            summary = await copilot.quick_summary(evt)
                            logger.info("AUTO-SUMMARY: %s", summary[:120])
                        except Exception as exc:
                            logger.warning("Auto-summary failed: %s", exc)
            finally:
                try:
                    await consumer.stop()
                except Exception:
                    pass

        except asyncio.CancelledError:
            logger.info("Kafka consumer cancelled — shutting down")
            return

        except Exception as exc:
            logger.warning(
                "Kafka consumer disconnected (%s) — reconnecting in %.0fs",
                exc, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


# ─── Lifespan ─────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("CCDT Co-Pilot starting — model chain: %s",
                " → ".join(MODEL_CHAIN))
    ctx_builder = ClusterContextBuilder()
    tool_executor = ToolExecutor()
    copilot = CCDTCoPilot(ctx_builder, tool_executor)
    app.state.copilot = copilot
    task = asyncio.create_task(_kafka_inference_consumer(copilot))
    app.state.kafka_task = task
    logger.info("CCDT Co-Pilot ready")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("CCDT Co-Pilot stopped")


# ─── FastAPI app ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="CCDT Co-Pilot",
    description="Layer-4 Gemini AI Operator — natural language cluster intelligence",
    version="4.0.0",
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
        "status":      "ok",
        "service":     "layer4-copilot",
        "sdk":         "google.genai",
        "model_chain": MODEL_CHAIN,
        "timestamp":   int(time.time()),
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
