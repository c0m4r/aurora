"""Core agentic tool-use loop — yields SSE-ready dicts."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfoNotFoundError

from ..providers.base import ContentBlock, NormalizedMessage, StreamEvent
from ..providers.registry import ProviderRegistry
from ..tools.registry import ToolRegistry
from ..tools.sandbox import set_session

logger = logging.getLogger(__name__)

# ─── Secure-mode tool-call approvals ────────────────────────────────────────
# When secure mode is on, the agent loop pauses before each tool execution
# until the client approves or declines via POST /api/tool_approve.
_pending_approvals: dict[str, asyncio.Future] = {}
_APPROVAL_TIMEOUT = 300.0  # seconds


def submit_approval(conversation_id: str, tool_id: str, approve: bool) -> bool:
    """Resolve a pending approval future. Returns True if a future was waiting.

    Approvals are scoped to a conversation so one authenticated client cannot
    approve tool calls belonging to a different conversation.
    """
    key = f"{conversation_id}:{tool_id}"
    fut = _pending_approvals.get(key)
    if fut is None or fut.done():
        return False
    fut.set_result(approve)
    return True

# ─── System prompt loading ───────────────────────────────────────────────────
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt from the prompts/ directory. Falls back to empty string."""
    path = _PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning("Prompt file not found: %s", path)
        return ""


def _current_datetime_block() -> str:
    """Return a short block describing the current date/time in UTC and local time."""
    now_utc = datetime.now(timezone.utc)
    utc_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Try to get the local timezone name
    try:
        local_now = datetime.now().astimezone()
        tz_name = local_now.tzname() or "local"
        local_str = local_now.strftime(f"%Y-%m-%d %H:%M:%S {tz_name}")
    except Exception:
        local_str = utc_str

    return f"Current date/time: {utc_str} / {local_str}"


def _build_system(cfg: Any, injected_solutions: list[dict]) -> str:
    extra = ""
    agent_cfg = getattr(cfg, "agent", None)
    if agent_cfg:
        extra = getattr(agent_cfg, "system_prompt_extra", "") or ""

    system = _load_prompt("system.md")
    system += f"\n\n## Current Time\n{_current_datetime_block()}\n"
    if extra:
        system += f"\n\n## Additional Instructions\n{extra}"

    if injected_solutions:
        system += "\n\n## Relevant Past Solutions\n"
        for sol in injected_solutions[:3]:
            system += (
                f"\n**Problem**: {sol['problem']}\n"
                f"**Solution**: {sol['solution']}\n---"
            )

    return system


class AgentLoop:
    def __init__(
        self,
        registry: ProviderRegistry,
        tools: ToolRegistry,
        cfg: Any,
        conversation_id: str | None = None,
    ):
        self.registry = registry
        self.tools = tools
        self.cfg = cfg
        self.conversation_id = conversation_id
        self._max_iter = int(
            getattr(getattr(cfg, "agent", None), "max_tool_iterations", 15) or 15
        )
        self._tool_timeout = float(
            getattr(getattr(cfg, "agent", None), "tool_timeout_seconds", 60) or 60
        )

    async def run(
        self,
        messages: list[NormalizedMessage],
        model_id: str,
        injected_solutions: list[dict] | None = None,
        debug: bool = False,
        secure: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[dict]:
        # Scope file tools to this conversation's session directory
        set_session(self.conversation_id)

        system = _build_system(self.cfg, injected_solutions or [])
        tool_schemas = self.tools.schemas()

        # Working message history (mutable copy)
        history = list(messages)

        logger.debug("AgentLoop.run: starting with %d message(s) in history, model=%s", len(history), model_id)
        for idx, msg in enumerate(history):
            text_preview = (msg.text or "")[:120]
            block_types = [b.type for b in msg.blocks]
            logger.debug("  history[%d] role=%s text=%r blocks=%s", idx, msg.role, text_preview, block_types)

        # Emit debug payload at the start
        if debug:
            # Serialize history into a simple dict format
            serialized_history = []
            for msg in history:
                if msg.blocks:
                    for blk in msg.blocks:
                        if blk.type == "text":
                            serialized_history.append({"role": msg.role, "content": blk.text})
                        elif blk.type == "thinking":
                            serialized_history.append({"role": msg.role, "type": "thinking", "content": blk.text})
                        elif blk.type == "tool_use":
                            serialized_history.append({
                                "role": msg.role,
                                "type": "tool_use",
                                "id": blk.tool_use_id,
                                "name": blk.tool_name,
                                "input": blk.tool_input,
                            })
                        elif blk.type == "tool_result":
                            serialized_history.append({
                                "role": msg.role,
                                "type": "tool_result",
                                "for_id": blk.tool_result_for_id,
                                "content": blk.tool_result_content or "",
                                "error": bool(getattr(blk, "tool_is_error", False)),
                            })
                        elif blk.type in ("image", "video"):
                            serialized_history.append({
                                "role": msg.role,
                                "type": blk.type,
                                "media_type": getattr(blk, "image_media_type", getattr(blk, "video_media_type", "")),
                                "data_length": len(getattr(blk, "image_data", getattr(blk, "video_data", ""))),
                            })
                elif msg.text:
                    # Message with only text (no blocks)
                    serialized_history.append({"role": msg.role, "content": msg.text})

            yield {
                "type": "debug",
                "system": system,
                "tools": tool_schemas,
                "history": serialized_history,
                "history_summary": {
                    "total_messages": len(history),
                    "by_role": {
                        "user": sum(1 for m in history if m.role == "user"),
                        "assistant": sum(1 for m in history if m.role == "assistant"),
                        "system": sum(1 for m in history if m.role == "system"),
                    },
                },
            }
            logger.debug("Debug payload emitted (%d history messages)", len(serialized_history))

        for iteration in range(self._max_iter):
            tool_calls_this_turn: list[dict] = []
            text_buf = ""
            thinking_buf = ""
            usage_snapshot: dict = {}

            try:
                async for event in self.registry.stream(
                    model_id=model_id,
                    messages=history,
                    tools=tool_schemas,
                    system=system,
                    **kwargs,
                ):
                    if event.type == "thinking_delta":
                        thinking_buf += event.delta
                        yield {"type": "thinking", "content": event.delta}

                    elif event.type == "text_delta":
                        text_buf += event.delta
                        yield {"type": "text", "content": event.delta}

                    elif event.type == "tool_input_start":
                        yield {
                            "type": "tool_input_start",
                            "id": event.tool_id,
                            "name": event.tool_name,
                        }

                    elif event.type == "tool_input_delta":
                        yield {
                            "type": "tool_input_delta",
                            "id": event.tool_id,
                            "delta": event.delta,
                        }

                    elif event.type == "tool_call":
                        tc = {
                            "id": event.tool_id,
                            "name": event.tool_name,
                            "input": event.tool_input or {},
                        }
                        tool_calls_this_turn.append(tc)
                        yield {"type": "tool_call", **tc}

                    elif event.type == "usage":
                        if event.usage:
                            usage_snapshot = {
                                "input_tokens": event.usage.input_tokens,
                                "output_tokens": event.usage.output_tokens,
                            }
                            yield {"type": "usage", **usage_snapshot}

                    elif event.type == "error":
                        yield {"type": "error", "content": event.error}
                        return

            except Exception as exc:
                logger.exception("Provider stream error: %s", exc)
                yield {"type": "error", "content": "A provider error occurred"}
                return

            if not tool_calls_this_turn:
                # No tools invoked — we're done
                break

            # Build assistant message for history
            assistant_blocks: list[ContentBlock] = []
            if thinking_buf:
                assistant_blocks.append(ContentBlock(type="thinking", text=thinking_buf))
            if text_buf:
                assistant_blocks.append(ContentBlock(type="text", text=text_buf))
            for tc in tool_calls_this_turn:
                assistant_blocks.append(ContentBlock(
                    type="tool_use",
                    tool_use_id=tc["id"],
                    tool_name=tc["name"],
                    tool_input=tc["input"],
                ))

            history.append(NormalizedMessage(role="assistant", blocks=assistant_blocks))

            # Secure mode: ask the user to approve each tool call before running it.
            approvals: dict[str, bool] = {}
            if secure:
                for tc in tool_calls_this_turn:
                    fut: asyncio.Future = asyncio.get_running_loop().create_future()
                    key = f"{self.conversation_id or ''}:{tc['id']}"
                    _pending_approvals[key] = fut
                    yield {
                        "type": "tool_approval_required",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["input"],
                    }
                    try:
                        approved = await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT)
                    except asyncio.TimeoutError:
                        approved = False
                    finally:
                        _pending_approvals.pop(key, None)
                    approvals[tc["id"]] = approved
                    yield {
                        "type": "tool_approval_resolved",
                        "id": tc["id"],
                        "approved": approved,
                    }

            # Execute tools (concurrently); declined ones get a synthetic error.
            # Stream live progress chunks through a shared queue so the UI can
            # see stdout/stderr from long-running tools in real time.
            progress_queue: asyncio.Queue = asyncio.Queue()

            async def _run_or_decline(tc: dict) -> tuple[str, str, bool]:
                if secure and not approvals.get(tc["id"], False):
                    return (
                        tc["id"],
                        "User declined to run this tool. Adjust your approach or ask for clarification.",
                        True,
                    )

                async def on_progress(chunk: str) -> None:
                    await progress_queue.put((tc["id"], chunk))

                return await self._exec_tool(tc, on_progress=on_progress)

            exec_tasks = [
                asyncio.create_task(_run_or_decline(tc))
                for tc in tool_calls_this_turn
            ]
            gather_task = asyncio.gather(*exec_tasks)

            # Drain progress chunks until all executions complete.
            while not gather_task.done() or not progress_queue.empty():
                try:
                    tid, chunk = await asyncio.wait_for(
                        progress_queue.get(), timeout=0.1
                    )
                except asyncio.TimeoutError:
                    continue
                yield {
                    "type": "tool_output_delta",
                    "id": tid,
                    "delta": chunk,
                }

            results = await gather_task

            # Stream results and build tool_result blocks for next turn
            result_blocks: list[ContentBlock] = []
            for tool_id, output, is_error in results:
                tc_name = next((t["name"] for t in tool_calls_this_turn if t["id"] == tool_id), "")
                yield {
                    "type": "tool_result",
                    "id": tool_id,
                    "name": tc_name,
                    "output": output,
                    "error": is_error,
                }
                result_blocks.append(ContentBlock(
                    type="tool_result",
                    tool_result_for_id=tool_id,
                    tool_result_content=output,
                    tool_is_error=is_error,
                ))

            history.append(NormalizedMessage(role="user", blocks=result_blocks))

        else:
            yield {"type": "text", "content": "\n\n*[Max tool iterations reached. Stopping.]*"}

        yield {"type": "done"}


    async def _exec_tool(
        self,
        tc: dict,
        on_progress: Any = None,
    ) -> tuple[str, str, bool]:
        try:
            kwargs = dict(tc["input"])
            if on_progress is not None:
                kwargs["_progress_cb"] = on_progress
            result = await asyncio.wait_for(
                self.tools.execute(tc["name"], **kwargs),
                timeout=self._tool_timeout,
            )
            return tc["id"], result, False
        except asyncio.TimeoutError:
            return tc["id"], f"Tool '{tc['name']}' timed out after {self._tool_timeout}s", True
        except Exception as exc:
            return tc["id"], f"Tool error: {exc}", True


async def run_agent(
    registry: ProviderRegistry,
    tools: ToolRegistry,
    cfg: Any,
    messages: list[NormalizedMessage],
    model_id: str,
    injected_solutions: list[dict] | None = None,
    conversation_id: str | None = None,
    **kwargs: Any,
) -> AsyncIterator[dict]:
    loop = AgentLoop(registry, tools, cfg, conversation_id=conversation_id)
    async for event in loop.run(messages, model_id, injected_solutions, **kwargs):
        yield event
