"""Native chat API with SSE streaming."""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...agent.loop import AgentLoop
from ...api.auth import require_api_key
from ...config import get as get_cfg
from ...memory.store import get_store
from ...providers.base import NormalizedMessage
from ...providers.registry import get_registry
from ...tools.registry import build_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    thinking: bool = True
    # Optional: immediately save a solution after this turn
    save_solution: bool = False
    solution_problem: Optional[str] = None
    solution_text: Optional[str] = None


class TitleUpdate(BaseModel):
    title: str


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, _auth: str = Depends(require_api_key)):
    cfg = get_cfg()
    store = get_store()
    registry = get_registry()

    # Resolve or create conversation
    conv_id = req.conversation_id
    model_id = req.model or getattr(cfg, "default_model", "anthropic/claude-sonnet-4-6")

    if not conv_id:
        conv_id = await store.create_conversation(model=model_id)

    # Persist user message
    await store.add_message(conv_id, "user", req.message)

    # Load history and convert to NormalizedMessages
    raw_msgs = await store.get_messages(conv_id)
    history = _to_normalized(raw_msgs)

    # Inject relevant past solutions into system prompt
    solutions = await store.search_solutions(req.message, limit=3)

    tools = build_registry(cfg)
    loop = AgentLoop(registry, tools, cfg)

    assistant_text_parts: list[str] = []
    thinking_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0

    async def generate():
        nonlocal input_tokens, output_tokens

        yield f"data: {json.dumps({'type': 'conv_id', 'conversation_id': conv_id})}\n\n"

        try:
            async for event in loop.run(history, model_id, injected_solutions=solutions, thinking=req.thinking):
                etype = event.get("type")
                if etype == "text":
                    assistant_text_parts.append(event["content"])
                elif etype == "thinking":
                    thinking_parts.append(event["content"])
                elif etype == "usage":
                    input_tokens = event.get("input_tokens", input_tokens)
                    output_tokens = event.get("output_tokens", output_tokens)
                elif etype == "done":
                    # Persist assistant response
                    full_text = "".join(assistant_text_parts)
                    full_thinking = "".join(thinking_parts)
                    if full_text or full_thinking:
                        await store.add_message(
                            conv_id, "assistant", full_text,
                            thinking=full_thinking or None,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            model=model_id,
                        )

                    # Auto-title from first user message
                    if len(raw_msgs) <= 1:
                        title = req.message[:70] + ("…" if len(req.message) > 70 else "")
                        await store.update_conversation(conv_id, title)

                    # Optionally save solution
                    if req.save_solution and req.solution_problem and req.solution_text:
                        await store.save_solution(
                            problem=req.solution_problem,
                            solution=req.solution_text,
                            source_conv_id=conv_id,
                        )

                yield f"data: {json.dumps(event)}\n\n"

        except Exception as exc:
            logger.exception("Agent loop error")
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/conversations")
async def list_conversations(_auth: str = Depends(require_api_key)):
    return await get_store().list_conversations()


@router.get("/conversations/{cid}")
async def get_conversation(cid: str, _auth: str = Depends(require_api_key)):
    messages = await get_store().get_messages(cid)
    return {"messages": messages}


@router.patch("/conversations/{cid}")
async def rename_conversation(
    cid: str, body: TitleUpdate, _auth: str = Depends(require_api_key)
):
    await get_store().update_conversation(cid, body.title)
    return {"ok": True}


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: str, _auth: str = Depends(require_api_key)):
    await get_store().delete_conversation(cid)
    return {"ok": True}


@router.get("/models")
async def list_models(_auth: str = Depends(require_api_key)):
    registry = get_registry()
    models = await registry.list_models()
    return {
        "models": [
            {
                "id": m.full_id,
                "name": m.name,
                "provider": m.provider,
                "context_length": m.context_length,
                "supports_thinking": m.supports_thinking,
            }
            for m in models
        ]
    }


@router.get("/solutions")
async def list_solutions(_auth: str = Depends(require_api_key)):
    return await get_store().list_solutions()


@router.post("/solutions")
async def create_solution(body: dict, _auth: str = Depends(require_api_key)):
    sid = await get_store().save_solution(
        problem=body.get("problem", ""),
        solution=body.get("solution", ""),
        title=body.get("title", ""),
        tags=body.get("tags", []),
    )
    return {"id": sid}


@router.delete("/solutions/{sid}")
async def delete_solution(sid: int, _auth: str = Depends(require_api_key)):
    await get_store().delete_solution(sid)
    return {"ok": True}


@router.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


def _to_normalized(raw_msgs: list[dict]) -> list[NormalizedMessage]:
    """Convert stored message dicts back to NormalizedMessages for the agent."""
    result: list[NormalizedMessage] = []
    for m in raw_msgs:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role in ("user", "assistant"):
            result.append(NormalizedMessage(role=role, text=content))  # type: ignore[arg-type]
    return result
