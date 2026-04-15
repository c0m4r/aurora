"""Anthropic provider with native streaming and extended thinking support."""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import anthropic

from .base import (
    BaseProvider,
    ContentBlock,
    ModelInfo,
    NormalizedMessage,
    StreamEvent,
    TokenUsage,
)

logger = logging.getLogger(__name__)

KNOWN_MODELS = [
    ModelInfo("claude-opus-4-6",             "Claude Opus 4.6",     "anthropic", 200_000, True, True),
    ModelInfo("claude-sonnet-4-6",           "Claude Sonnet 4.6",   "anthropic", 200_000, True, True),
    ModelInfo("claude-haiku-4-5-20251001",   "Claude Haiku 4.5",    "anthropic", 200_000, True, False),
    ModelInfo("claude-3-5-sonnet-20241022",  "Claude 3.5 Sonnet",   "anthropic", 200_000, True, False),
    ModelInfo("claude-3-5-haiku-20241022",   "Claude 3.5 Haiku",    "anthropic", 200_000, True, False),
    ModelInfo("claude-3-opus-20240229",      "Claude 3 Opus",       "anthropic", 200_000, True, False),
]

# Models that support extended thinking
_THINKING_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6"}


def _to_api_messages(messages: list[NormalizedMessage]) -> list[dict]:
    """Convert NormalizedMessages to Anthropic API format."""
    result = []
    for msg in messages:
        if msg.role == "system":
            continue  # system goes in separate param

        if msg.text is not None and not msg.blocks:
            result.append({"role": msg.role, "content": msg.text})
            continue

        content = []
        for blk in msg.blocks:
            if blk.type == "image" and blk.image_data:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": blk.image_media_type or "image/png",
                        "data": blk.image_data,
                    },
                })
            elif blk.type == "video" and blk.video_data:
                content.append({
                    "type": "video",
                    "source": {
                        "type": "base64",
                        "media_type": blk.video_media_type or "video/mp4",
                        "data": blk.video_data,
                    },
                })
            elif blk.type == "text" and blk.text:
                content.append({"type": "text", "text": blk.text})
            elif blk.type == "thinking" and blk.text:
                content.append({"type": "thinking", "thinking": blk.text})
            elif blk.type == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": blk.tool_use_id,
                    "name": blk.tool_name,
                    "input": blk.tool_input or {},
                })
            elif blk.type == "tool_result":
                content.append({
                    "type": "tool_result",
                    "tool_use_id": blk.tool_result_for_id,
                    "content": blk.tool_result_content or "",
                    "is_error": blk.tool_is_error,
                })

        if content:
            result.append({"role": msg.role, "content": content})

    return result


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def list_models(self) -> list[ModelInfo]:
        return KNOWN_MODELS

    async def stream(
        self,
        messages: list[NormalizedMessage],
        tools: list[dict],
        model: str,
        system: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        api_messages = _to_api_messages(messages)
        logger.debug(
            "AnthropicProvider.stream: %d normalized input messages -> %d API messages, model=%s",
            len(messages), len(api_messages), model,
        )
        for idx, api_msg in enumerate(api_messages):
            role = api_msg.get("role", "?")
            content = api_msg.get("content", "")
            content_preview = content[:150] if isinstance(content, str) else str(content)[:150]
            logger.debug("  api_msg[%d] role=%s content=%r", idx, role, content_preview)

        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in (tools or [])
        ]

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": kwargs.get("max_tokens", 8192),
            "messages": api_messages,
        }
        if system:
            params["system"] = system
        if anthropic_tools:
            params["tools"] = anthropic_tools

        # Extended thinking
        if model in _THINKING_MODELS and kwargs.get("thinking", True):
            budget = kwargs.get("thinking_budget", 5000)
            params["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # thinking requires max_tokens > budget
            params["max_tokens"] = max(params["max_tokens"], budget + 2048)

        current_tool_id: str | None = None
        current_tool_name: str | None = None
        current_tool_input_buf: str = ""

        async with self._client.messages.stream(**params) as stream_ctx:
            async for event in stream_ctx:
                etype = getattr(event, "type", None)

                if etype == "content_block_start":
                    blk = event.content_block
                    # Always reset the tool-input buffer on any new content block
                    # so streamed bytes from a prior block can't leak into the next.
                    current_tool_input_buf = ""
                    current_tool_id = None
                    current_tool_name = None
                    if getattr(blk, "type", None) == "tool_use":
                        current_tool_id = blk.id
                        current_tool_name = blk.name
                        yield StreamEvent(
                            type="tool_input_start",
                            tool_id=current_tool_id,
                            tool_name=current_tool_name or "",
                        )

                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", None)
                    if dtype == "thinking_delta":
                        yield StreamEvent(type="thinking_delta", delta=delta.thinking)
                    elif dtype == "text_delta":
                        yield StreamEvent(type="text_delta", delta=delta.text)
                    elif dtype == "input_json_delta":
                        # Only accumulate while we're inside an active tool_use block.
                        if current_tool_id:
                            current_tool_input_buf += delta.partial_json
                            yield StreamEvent(
                                type="tool_input_delta",
                                tool_id=current_tool_id,
                                delta=delta.partial_json,
                            )

                elif etype == "content_block_stop":
                    if current_tool_id:
                        # Prefer the authoritative parsed input from the SDK when available;
                        # fall back to our streamed buffer otherwise.
                        tool_input: Any = None
                        blk = getattr(event, "content_block", None)
                        if blk is not None and getattr(blk, "type", None) == "tool_use":
                            tool_input = getattr(blk, "input", None)
                        if tool_input is None:
                            try:
                                tool_input = (
                                    json.loads(current_tool_input_buf)
                                    if current_tool_input_buf else {}
                                )
                            except json.JSONDecodeError:
                                tool_input = {"_raw": current_tool_input_buf}
                        yield StreamEvent(
                            type="tool_call",
                            tool_id=current_tool_id,
                            tool_name=current_tool_name or "",
                            tool_input=tool_input,
                        )
                        current_tool_id = None
                        current_tool_name = None
                        current_tool_input_buf = ""

            # Final usage from the complete message
            try:
                final = await stream_ctx.get_final_message()
                yield StreamEvent(
                    type="usage",
                    usage=TokenUsage(
                        input_tokens=final.usage.input_tokens,
                        output_tokens=final.usage.output_tokens,
                    ),
                )
            except Exception:
                pass
