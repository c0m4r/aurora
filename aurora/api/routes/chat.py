"""Native chat API with SSE streaming."""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...agent.learner import run_extract
from ...agent.loop import AgentLoop
from ...api.auth import require_api_key
from ...config import get as get_cfg
from ...memory.store import get_store
from ...providers.base import ContentBlock, NormalizedMessage
from ...providers.registry import get_registry
from ...tools.registry import build_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class ImageData(BaseModel):
    data: str         # base64-encoded
    media_type: str   # e.g. "image/png"


class VideoData(BaseModel):
    data: str         # base64-encoded
    media_type: str   # e.g. "video/mp4"


class ChatRequest(BaseModel):
    message: str
    images: Optional[list[ImageData]] = None
    videos: Optional[list[VideoData]] = None
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    thinking: bool = True
    learn: Optional[bool] = None  # None = use config default; True/False = override
    debug: bool = False
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

    # Persist user message with images and videos
    user_blocks: list[dict] | None = None
    if req.images or req.videos:
        user_blocks = []
        if req.images:
            user_blocks.extend(
                {"type": "image", "image_data": img.data, "image_media_type": img.media_type}
                for img in req.images
            )
        if req.videos:
            user_blocks.extend(
                {"type": "video", "video_data": vid.data, "video_media_type": vid.media_type}
                for vid in req.videos
            )
        user_blocks.append({"type": "text", "text": req.message})
    await store.add_message(conv_id, "user", req.message, blocks=user_blocks)

    # Load history and convert to NormalizedMessages
    raw_msgs = await store.get_messages(conv_id)
    logger.debug("Loaded %d stored messages for conversation %s", len(raw_msgs), conv_id)
    for idx, msg in enumerate(raw_msgs):
        role = msg.get("role", "?")
        content_preview = (msg.get("content", "") or "")[:120]
        has_blocks = bool(msg.get("blocks"))
        logger.debug("  msg[%d] role=%s content=%r has_blocks=%s", idx, role, content_preview, has_blocks)
    history = _to_normalized(raw_msgs)
    logger.debug("Normalized into %d message(s) for agent loop", len(history))
    for idx, nm in enumerate(history):
        role = nm.role
        text_preview = (nm.text or "")[:120]
        block_types = [b.type for b in nm.blocks]
        logger.debug("  normalized[%d] role=%s text=%r blocks=%s", idx, role, text_preview, block_types)

    # Inject images and videos into the last user message (media before text for optimal performance)
    if (req.images or req.videos) and history and history[-1].role == "user":
        last = history[-1]
        media_blocks = []
        if req.images:
            media_blocks.extend([
                ContentBlock(type="image", image_data=img.data, image_media_type=img.media_type)
                for img in req.images
            ])
        if req.videos:
            media_blocks.extend([
                ContentBlock(type="video", video_data=vid.data, video_media_type=vid.media_type)
                for vid in req.videos
            ])
        text_block = ContentBlock(type="text", text=last.text or "")
        last.text = None
        last.blocks = media_blocks + [text_block]

    # Inject relevant past solutions into system prompt
    solutions = await store.search_solutions(req.message, limit=3)

    tools = build_registry(cfg)
    loop = AgentLoop(registry, tools, cfg, conversation_id=conv_id)

    cfg_auto_learn = getattr(getattr(cfg, "agent", None), "auto_learn", False)
    do_learn = req.learn if req.learn is not None else cfg_auto_learn

    assistant_text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_blocks: list[dict] = []   # ordered tool_use / tool_result for persistence
    tool_log: list[dict] = []      # flattened log for learner
    input_tokens = 0
    output_tokens = 0
    start_time = time.monotonic()

    async def generate():
        nonlocal input_tokens, output_tokens

        yield f"data: {json.dumps({'type': 'conv_id', 'conversation_id': conv_id})}\n\n"

        # Track tool calls so we can pair them with results for the learner
        pending_tools: dict[str, dict] = {}

        try:
            async for event in loop.run(history, model_id, injected_solutions=solutions, thinking=req.thinking, debug=req.debug):
                etype = event.get("type")
                if etype == "text":
                    assistant_text_parts.append(event["content"])
                elif etype == "thinking":
                    thinking_parts.append(event["content"])
                elif etype == "tool_call":
                    pending_tools[event["id"]] = {
                        "name": event["name"],
                        "input": event.get("input", {}),
                    }
                    tool_blocks.append({
                        "type": "tool_use",
                        "id": event["id"],
                        "name": event["name"],
                        "input": event.get("input", {}),
                    })
                elif etype == "tool_result":
                    tool_blocks.append({
                        "type": "tool_result",
                        "for_id": event.get("id", ""),
                        "output": event.get("output", ""),
                        "error": event.get("error", False),
                    })
                    tc = pending_tools.pop(event.get("id", ""), None)
                    if tc:
                        tool_log.append({
                            "name": tc["name"],
                            "input": tc["input"],
                            "output": event.get("output", ""),
                        })
                elif etype == "debug":
                    # Pass through debug payload to frontend — not persisted
                    try:
                        payload = json.dumps(event)
                        logger.info("Debug event forwarded to frontend (system=%d chars, tools=%d, history=%d)",
                                    len(event.get("system", "")),
                                    len(event.get("tools", [])),
                                    len(event.get("history", [])))
                        yield f"data: {payload}\n\n"
                    except Exception as exc:
                        logger.warning("Failed to serialize debug event: %s", exc)
                    continue
                elif etype == "usage":
                    input_tokens += event.get("input_tokens", 0)
                    output_tokens += event.get("output_tokens", 0)
                elif etype == "done":
                    # Calculate response time
                    response_time = time.monotonic() - start_time

                    # Persist assistant response
                    full_text = "".join(assistant_text_parts)
                    full_thinking = "".join(thinking_parts)
                    if full_text or full_thinking or tool_blocks:
                        await store.add_message(
                            conv_id, "assistant", full_text,
                            blocks=tool_blocks or None,
                            thinking=full_thinking or None,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            model=model_id,
                            response_time_ms=round(response_time * 1000, 2),
                        )

                    # Auto-title from first user message
                    if len(raw_msgs) <= 1:
                        title = req.message[:70] + ("…" if len(req.message) > 70 else "")
                        await store.update_conversation(conv_id, title)

                    # Optionally save solution (manual)
                    if req.save_solution and req.solution_problem and req.solution_text:
                        await store.save_solution(
                            problem=req.solution_problem,
                            solution=req.solution_text,
                            source_conv_id=conv_id,
                        )

                    # Auto-learn: extract and save solution inline (visible to user)
                    if do_learn and tool_log:
                        async for learn_event in run_extract(
                            registry=registry,
                            model_id=model_id,
                            user_message=req.message,
                            assistant_text=full_text,
                            tool_log=tool_log,
                            store=store,
                        ):
                            yield f"data: {json.dumps(learn_event)}\n\n"

                    # Emit response time
                    yield f"data: {json.dumps({'type': 'response_time', 'duration_ms': round(response_time * 1000, 2)})}\n\n"

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


class LearnRequest(BaseModel):
    conversation_id: str
    message_id: Optional[int] = None  # None = last tool-using message


@router.post("/learn")
async def learn_stream(req: LearnRequest, _auth: str = Depends(require_api_key)):
    """One-shot learn: extract a solution from a specific assistant message."""
    cfg = get_cfg()
    store = get_store()
    registry = get_registry()

    msgs = await store.get_messages(req.conversation_id)

    # Find target assistant message and its preceding user message
    target = None
    user_msg_text = ""
    for i, m in enumerate(msgs):
        if m["role"] != "assistant" or not m.get("blocks"):
            continue
        if req.message_id is not None and m.get("id") != req.message_id:
            continue
        target = m
        # Walk back to find the preceding user message
        for j in range(i - 1, -1, -1):
            if msgs[j]["role"] == "user":
                user_msg_text = msgs[j].get("content", "")
                break

    if not target:
        async def empty():
            yield f"data: {json.dumps({'type': 'learn', 'status': 'skipped', 'reason': 'no tool interactions found'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return StreamingResponse(empty(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Access-Control-Allow-Origin": "*"})

    # Reconstruct tool_log from saved blocks
    blocks = target["blocks"]
    results_by_id = {b["for_id"]: b for b in blocks if b.get("type") == "tool_result"}
    tool_log = []
    for b in blocks:
        if b.get("type") == "tool_use":
            res = results_by_id.get(b["id"], {})
            tool_log.append({
                "name": b["name"],
                "input": b.get("input", {}),
                "output": res.get("output", ""),
            })

    model_id = target.get("model") or getattr(cfg, "default_model", "")

    async def generate():
        async for event in run_extract(
            registry=registry,
            model_id=model_id,
            user_message=user_msg_text,
            assistant_text=target.get("content", ""),
            tool_log=tool_log,
            store=store,
        ):
            yield f"data: {json.dumps(event)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Access-Control-Allow-Origin": "*"},
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
    # Clean up session files if they exist
    from pathlib import Path
    import shutil
    session_dir = Path.cwd() / "files" / "sessions" / cid
    if session_dir.is_dir():
        shutil.rmtree(session_dir, ignore_errors=True)
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
    """Convert stored message dicts back to NormalizedMessages for the agent.

    For assistant messages with saved tool blocks, reconstruct the multi-message
    tool-use history so the model sees the full context.
    For user messages with image/video blocks, reconstruct those as well.
    """
    from ...providers.base import ContentBlock

    result: list[NormalizedMessage] = []
    for m in raw_msgs:
        role = m.get("role", "user")
        content = m.get("content", "")
        blocks = m.get("blocks") or []

        if role == "user":
            # Check for image/video blocks and reconstruct them
            media_blocks = []
            for blk in blocks:
                if blk["type"] == "image" and blk.get("image_data"):
                    media_blocks.append(ContentBlock(
                        type="image",
                        image_data=blk["image_data"],
                        image_media_type=blk.get("image_media_type", "image/png"),
                    ))
                elif blk["type"] == "video" and blk.get("video_data"):
                    media_blocks.append(ContentBlock(
                        type="video",
                        video_data=blk["video_data"],
                        video_media_type=blk.get("video_media_type", "video/mp4"),
                    ))

            if media_blocks:
                # Media blocks before text
                text_block = ContentBlock(type="text", text=content) if content else None
                result.append(NormalizedMessage(
                    role="user",
                    blocks=media_blocks + ([text_block] if text_block else []),
                ))
            else:
                result.append(NormalizedMessage(role="user", text=content))

        elif role == "assistant":
            if not blocks:
                result.append(NormalizedMessage(role="assistant", text=content))
                continue

            # Reconstruct tool iterations from saved blocks.
            # Stream order: [tool_use, tool_use, …, tool_result, tool_result, …]
            # repeating per iteration.  A tool_use after tool_results starts a new batch.
            iterations: list[tuple[list[dict], list[dict]]] = []
            cur_uses: list[dict] = []
            cur_results: list[dict] = []

            for blk in blocks:
                if blk["type"] == "tool_use":
                    if cur_results:
                        iterations.append((cur_uses, cur_results))
                        cur_uses, cur_results = [], []
                    cur_uses.append(blk)
                elif blk["type"] == "tool_result":
                    cur_results.append(blk)
            if cur_uses or cur_results:
                iterations.append((cur_uses, cur_results))

            for uses, results in iterations:
                # Assistant turn: tool_use blocks
                asst_blocks = [
                    ContentBlock(
                        type="tool_use",
                        tool_use_id=u["id"],
                        tool_name=u["name"],
                        tool_input=u.get("input", {}),
                    )
                    for u in uses
                ]
                result.append(NormalizedMessage(role="assistant", blocks=asst_blocks))

                # User turn: tool_result blocks
                res_blocks = [
                    ContentBlock(
                        type="tool_result",
                        tool_result_for_id=r["for_id"],
                        tool_result_content=r.get("output", ""),
                        tool_is_error=r.get("error", False),
                    )
                    for r in results
                ]
                result.append(NormalizedMessage(role="user", blocks=res_blocks))

            # Final assistant text
            if content:
                result.append(NormalizedMessage(role="assistant", text=content))

    return result
