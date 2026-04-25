"""
CCDT API Gateway — Co-Pilot Router
Routes chat requests to Layer-4 Co-Pilot service.
No canned responses — all requests go directly to Layer-4.
"""
from __future__ import annotations

import asyncio
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
    "COPILOT_SERVICE_URL", "http://layer4-copilot:8003")


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
                    yield f'data: {json.dumps({"type":"error","message":f"Layer-4 error {resp.status_code}: {error_body.decode()[:200]}"})}\n\n'
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        yield line + "\n\n"
                        if line == "data: [DONE]":
                            return
    except httpx.ConnectError as exc:
        yield f'data: {json.dumps({"type":"error","message":f"Co-Pilot service unreachable: {exc}"})}\n\n'
    except Exception as exc:
        logger.error("Proxy stream error: %s", exc)
        yield f'data: {json.dumps({"type":"error","message":str(exc)})}\n\n'


@router.post("/copilot/chat")
async def copilot_chat(body: ChatRequest, request: Request):
    session_id = body.session_id
    _append_history(session_id, "user", body.message)

    if body.stream:
        async def event_generator():
            assistant_reply_parts = []
            async for chunk in _proxy_stream(session_id, body.message, body.context):
                if chunk.startswith("data: ") and "data: [DONE]" not in chunk:
                    assistant_reply_parts.append(chunk[6:].strip())
                yield chunk
            if assistant_reply_parts:
                _append_history(session_id, "assistant",
                                "".join(assistant_reply_parts))

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    else:
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
                    status_code=500, detail=f"Layer-4 error: {resp.text}")
            data = resp.json()
            reply = data.get("reply", "")

        _append_history(session_id, "assistant", reply)
        return JSONResponse({"session_id": session_id, "reply": reply, "timestamp": int(time.time())})


@router.post("/copilot/report")
async def generate_report(body: ReportRequest):
    prompt = f"Generate a full incident report for incident {body.incident_id}."
    session_id = f"report-{body.incident_id}-{int(time.time())}"

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
                status_code=500, detail=f"Report failed: {resp.text}")
        data = resp.json()
        report = data.get("reply", "")

    return JSONResponse({
        "incident_id":  body.incident_id,
        "format":       body.format,
        "report":       report,
        "generated_at": int(time.time()),
    })


@router.get("/copilot/sessions")
async def list_sessions():
    return JSONResponse({
        "sessions": [{"session_id": sid, "turns": len(hist)} for sid, hist in _SESSIONS.items()],
        "total":    len(_SESSIONS),
    })


@router.delete("/copilot/sessions/{session_id}")
async def clear_session(session_id: str):
    if session_id not in _SESSIONS:
        raise HTTPException(status_code=404, detail="Session not found")
    turns = len(_SESSIONS.pop(session_id))
    return JSONResponse({"session_id": session_id, "cleared_turns": turns})
