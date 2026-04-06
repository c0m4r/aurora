"""Core agentic tool-use loop — yields SSE-ready dicts."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfoNotFoundError

from ..providers.base import ContentBlock, NormalizedMessage, StreamEvent
from ..providers.registry import ProviderRegistry
from ..tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM = """You are a general-purpose AI assistant with Linux system administration expertise.

## Tools Available
- **ssh** — run commands on remote Linux servers. Read-only by default (information gathering). \
Only use write/modification commands when the user has *explicitly* asked you to make a change. \
Before running any state-changing command, state clearly what it will do.
- **web** — search the web (DuckDuckGo) or fetch a specific URL from a whitelisted domain.
- **weather** — get current weather and forecast for any location using Open-Meteo (no API key needed).
- **file_read** — read or list files inside the local ./files/ directory.
- **file_write** — create or append files inside the local ./files/ directory. \
Use to save reports, scripts, configs, notes, or any output the user wants to keep.
- **file_edit** — make precise edits to existing files using SEARCH/REPLACE blocks. \
Read the file first, then use exact content in SEARCH blocks. Returns a git-style diff. \
Prefer this over rewriting the entire file with file_write.
- **scp_upload** — upload files from ./files/ to remote servers via SCP (uses SSH host config).
- **get_datetime** — get the current date, time, timezone, and handy relative timestamps for queries.

## Working Principles
1. **Be helpful and direct.** Answer questions, solve problems, and get things done.
2. **SSH read-only by default.** Gather information first, never modify a system unless asked.
3. **Before any write operation on a server**, tell the user exactly what the command will do.
4. **Be specific.** Quote actual output, log lines, command results — don't paraphrase.
5. **Use files/** to persist useful output (reports, generated scripts, configs) so the user can retrieve it.
6. **Edit, don't rewrite.** When modifying existing files, use `file_edit` with SEARCH/REPLACE blocks \
instead of `file_write` to rewrite the entire file. This is faster, safer, and shows the user a diff.
7. **Search when unsure.** Use `web` to look up docs, error messages, or current release versions \
rather than relying on potentially stale training data.
"""


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

    system = _DEFAULT_SYSTEM + f"\n\n## Current Time\n{_current_datetime_block()}\n"
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
    ):
        self.registry = registry
        self.tools = tools
        self.cfg = cfg
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
        **kwargs: Any,
    ) -> AsyncIterator[dict]:
        system = _build_system(self.cfg, injected_solutions or [])
        tool_schemas = self.tools.schemas()

        # Working message history (mutable copy)
        history = list(messages)

        # Emit debug payload at the start
        if debug:
            # Serialize history into a simple dict format
            serialized_history = []
            for msg in history:
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
                            "content": blk.text or "",
                            "error": bool(getattr(blk, "tool_is_error", False)),
                        })
                    elif blk.type in ("image", "video"):
                        serialized_history.append({
                            "role": msg.role,
                            "type": blk.type,
                            "media_type": getattr(blk, "media_type", ""),
                            "data_length": len(getattr(blk, "data", "")),
                        })

            yield {
                "type": "debug",
                "system": system,
                "tools": tool_schemas,
                "history": serialized_history,
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
                logger.exception("Provider stream error")
                yield {"type": "error", "content": str(exc)}
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

            # Execute tools (concurrently)
            results = await asyncio.gather(*[
                self._exec_tool(tc) for tc in tool_calls_this_turn
            ])

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


    async def _exec_tool(self, tc: dict) -> tuple[str, str, bool]:
        try:
            result = await asyncio.wait_for(
                self.tools.execute(tc["name"], **tc["input"]),
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
    **kwargs: Any,
) -> AsyncIterator[dict]:
    loop = AgentLoop(registry, tools, cfg)
    async for event in loop.run(messages, model_id, injected_solutions, **kwargs):
        yield event
