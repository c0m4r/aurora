"""OpenAI-compatible /v1 endpoint — for opencode, Cursor, LM Studio clients, etc."""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...agent.loop import AgentLoop
from ...api.auth import require_api_key
from ...config import get as get_cfg
from ...providers.base import NormalizedMessage
from ...providers.registry import get_registry
from ...tools.registry import build_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")


class OAIMessage(BaseModel):
    role: str
    content: Any  # str or list
    name: Optional[str] = None
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None


class OAIRequest(BaseModel):
    model: str
    messages: list[OAIMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[list] = None


def _oai_messages_to_normalized(messages: list[OAIMessage]) -> tuple[str, list[NormalizedMessage]]:
    """Extract system prompt and convert to NormalizedMessages."""
    system = ""
    normalized: list[NormalizedMessage] = []
    for m in messages:
        if m.role == "system":
            system = m.content if isinstance(m.content, str) else str(m.content)
            continue
        content = m.content if isinstance(m.content, str) else (
            " ".join(p.get("text", "") for p in m.content if isinstance(p, dict)) if m.content else ""
        )
        if m.role in ("user", "assistant"):
            normalized.append(NormalizedMessage(role=m.role, text=content))  # type: ignore[arg-type]
    return system, normalized


@router.get("/models")
async def oai_list_models(_auth: str = Depends(require_api_key)):
    registry = get_registry()
    models = await registry.list_models()
    return {
        "object": "list",
        "data": [
            {
                "id": m.full_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": m.provider,
            }
            for m in models
        ],
    }


@router.post("/chat/completions")
async def oai_chat_completions(req: OAIRequest, _auth: str = Depends(require_api_key)):
    cfg = get_cfg()
    registry = get_registry()
    tools_reg = build_registry(cfg)

    system, messages = _oai_messages_to_normalized(req.messages)
    model_id = req.model

    # If model doesn't contain '/', try to use the configured default
    if "/" not in model_id:
        # Keep it as-is and let the registry resolve by trying providers
        default = getattr(cfg, "default_model", "")
        if default and "/" in default:
            provider_name = default.split("/")[0]
            model_id = f"{provider_name}/{model_id}"

    kwargs: dict[str, Any] = {}
    if system:
        kwargs["extra_system"] = system
    if req.max_tokens:
        kwargs["max_tokens"] = req.max_tokens

    loop = AgentLoop(registry, tools_reg, cfg)
    comp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if req.stream:
        async def stream_gen():
            async for event in loop.run(messages, model_id, **kwargs):
                etype = event.get("type")
                if etype == "text":
                    chunk = {
                        "id": comp_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_id,
                        "choices": [{"index": 0, "delta": {"content": event["content"]}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                elif etype == "done":
                    stop_chunk = {
                        "id": comp_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(stop_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

        return StreamingResponse(
            stream_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming
    parts: list[str] = []
    async for event in loop.run(messages, model_id, **kwargs):
        if event.get("type") == "text":
            parts.append(event["content"])

    return {
        "id": comp_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(parts)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
