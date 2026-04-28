"""
CCDT API Gateway — Co-Pilot Router
Routes chat requests to Layer-4 Co-Pilot service.
No canned responses — all requests proxy directly to Layer-4.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import defaultdict
from typing import Any, AsyncGenerator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("ccdt.routers.copilot")

router = APIRouter(prefix="/api/v1", tags=["copilot"])

COPILOT_SERVICE_URL = os.getenv(
    "COPILOT_SERVICE_URL", "http://layer4-copilot:8003"
)


class ChatRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    message:    str = Field(..., min_length=1, max_length=4096)
    stream:     bool = Field(True)
    context:    dict[str, Any] = Field(default_factory=dict)


class ReportRequest(BaseModel):
    incident_id: str
    format:      str = "markdown"


_SESSIONS: dict[str, list[dict[str, str]]] = defaultdict(list)
_SESSION_MAX_TURNS = 20


def _append_history(session_id: str, role: str, content: str) -> None:
    _SESSIONS[session_id].append({"role": role, "content": content})
    if len(_SESSIONS[session_id]) > _SESSION_MAX_TURNS * 2:
        _SESSIONS[session_id] = _SESSIONS[session_id][-_SESSION_MAX_TURNS * 2:]


async def _proxy_stream(
    session_id: str,
    message:    str,
    context:    dict[str, Any],
) -> AsyncGenerator[str, None]:
    """
    Forward a streaming chat request to Layer-4 and pass frames through verbatim.

    Layer-4 emits JSON-framed SSE lines:
        data: {"type": "tool_call",  "tool": "get_topology"}
        data: {"type": "text_delta", "text": "word "}
        data: {"type": "done",       "usage": {...}}
        data: {"type": "error",      "message": "..."}

    We pass all frames through unchanged so the Dashboard always receives
    consistently-structured JSON frames.
    """
    history = _SESSIONS[session_id]
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{COPILOT_SERVICE_URL}/chat/stream",
                json={
                    "session_id": session_id,
                    "message":    message,
                    "history":    history,
                    "context":    context,
                },
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    # Build JSON outside f-string — nested {} in f-strings
                    # requires Python 3.12+; containers run 3.11.
                    err_frame = json.dumps({
                        "type":    "error",
                        "message": f"Layer-4 returned {resp.status_code}: "
                                   + error_body.decode()[:200],
                    })
                    yield f"data: {err_frame}\n\n"
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    yield line + "\n\n"

                    # FIX: Layer-4 sends {"type":"done",...} as the terminal frame,
                    # NOT the literal string "[DONE]". Parse the JSON payload to
                    # detect termination correctly.
                    try:
                        payload = json.loads(line[6:])
                        if payload.get("type") == "done":
                            return
                    except (json.JSONDecodeError, AttributeError):
                        pass  # non-JSON lines are silently skipped

    except httpx.ConnectError as exc:
        yield f'data: {json.dumps({"type": "error", "message": f"Co-Pilot unreachable: {exc}"})}\n\n'
    except Exception as exc:
        logger.error("Proxy stream error: %s", exc)
        yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'

    # Always emit a done frame so the Dashboard doesn't hang waiting
    yield f'data: {json.dumps({"type": "done", "usage": {"input_tokens": 0, "output_tokens": 0}})}\n\n'


@router.post("/copilot/chat")
async def copilot_chat(body: ChatRequest, request: Request):
    session_id = body.session_id
    _append_history(session_id, "user", body.message)

    if body.stream:
        async def event_generator():
            assistant_parts: list[str] = []

            async for chunk in _proxy_stream(session_id, body.message, body.context):
                yield chunk

                # FIX: extract only the actual text from text_delta frames for
                # session history. The old code did chunk[6:].strip() which
                # stored raw JSON like '{"type":"text_delta","text":"word "}'
                # instead of just 'word '.
                if chunk.startswith("data: "):
                    try:
                        payload = json.loads(chunk[6:])
                        if payload.get("type") == "text_delta":
                            assistant_parts.append(payload.get("text", ""))
                    except (json.JSONDecodeError, AttributeError):
                        pass

            if assistant_parts:
                _append_history(session_id, "assistant",
                                "".join(assistant_parts))

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control":  "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    else:
        # Synchronous JSON path
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{COPILOT_SERVICE_URL}/chat",
                    json={
                        "session_id": session_id,
                        "message":    body.message,
                        "history":    _SESSIONS[session_id],
                        "context":    body.context,
                    },
                )
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Layer-4 error {resp.status_code}: {resp.text[:200]}",
                    )
                data = resp.json()
                reply = data.get("reply", "")
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Sync chat proxy error: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc))

        _append_history(session_id, "assistant", reply)
        return JSONResponse({
            "session_id": session_id,
            "reply":      reply,
            "timestamp":  int(time.time()),
        })


@router.post("/copilot/report")
async def generate_report(body: ReportRequest):
    prompt = f"Generate a full incident report for incident {body.incident_id}."
    session_id = f"report-{body.incident_id}-{int(time.time())}"

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{COPILOT_SERVICE_URL}/chat",
                json={
                    "session_id": session_id,
                    "message":    prompt,
                    "history":    [],
                    "context":    {"incident_id": body.incident_id, "format": body.format},
                },
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=500,
                    detail=f"Report failed: {resp.text[:200]}",
                )
            data = resp.json()
            report = data.get("reply", "")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Report generation error: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))

    return JSONResponse({
        "incident_id":  body.incident_id,
        "format":       body.format,
        "report":       report,
        "generated_at": int(time.time()),
    })


@router.get("/copilot/sessions")
async def list_sessions():
    return JSONResponse({
        "sessions": [
            {"session_id": sid, "turns": len(hist)}
            for sid, hist in _SESSIONS.items()
        ],
        "total": len(_SESSIONS),
    })


@router.delete("/copilot/sessions/{session_id}")
async def clear_session(session_id: str):
    if session_id not in _SESSIONS:
        raise HTTPException(status_code=404, detail="Session not found")
    turns = len(_SESSIONS.pop(session_id))
    return JSONResponse({"session_id": session_id, "cleared_turns": turns})
