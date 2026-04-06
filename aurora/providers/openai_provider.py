"""OpenAI-compatible provider — covers OpenAI, Gemini, Ollama, vLLM, etc."""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from .base import (
    BaseProvider,
    ContentBlock,
    ModelInfo,
    NormalizedMessage,
    StreamEvent,
    TokenUsage,
)

logger = logging.getLogger(__name__)

def _is_gemma4(model: str) -> bool:
    m = model.lower()
    return "gemma4" in m or "gemma-4" in m


class _ThinkParser:
    """
    Streams text through, splitting <think>...</think> blocks into
    thinking_delta events and passing the rest as text_delta events.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, text: str) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        self._buf += text
        while True:
            if not self._in_think:
                tag = "<think>"
                idx = self._buf.find(tag)
                if idx == -1:
                    cutoff = max(0, len(self._buf) - len(tag) + 1)
                    if cutoff > 0:
                        events.append(StreamEvent(type="text_delta", delta=self._buf[:cutoff]))
                        self._buf = self._buf[cutoff:]
                    break
                if idx > 0:
                    events.append(StreamEvent(type="text_delta", delta=self._buf[:idx]))
                self._buf = self._buf[idx + len(tag):]
                self._in_think = True
            else:
                tag = "</think>"
                idx = self._buf.find(tag)
                if idx == -1:
                    cutoff = max(0, len(self._buf) - len(tag) + 1)
                    if cutoff > 0:
                        events.append(StreamEvent(type="thinking_delta", delta=self._buf[:cutoff]))
                        self._buf = self._buf[cutoff:]
                    break
                if idx > 0:
                    events.append(StreamEvent(type="thinking_delta", delta=self._buf[:idx]))
                self._buf = self._buf[idx + len(tag):]
                self._in_think = False
        return events

    def flush(self) -> list[StreamEvent]:
        if not self._buf:
            return []
        etype = "thinking_delta" if self._in_think else "text_delta"
        events = [StreamEvent(type=etype, delta=self._buf)]
        self._buf = ""
        return events


def _to_api_messages(messages: list[NormalizedMessage], system: str) -> list[dict]:
    """Convert NormalizedMessages to OpenAI chat format."""
    result: list[dict] = []
    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        if msg.role == "system":
            continue  # already handled

        if msg.text is not None and not msg.blocks:
            result.append({"role": msg.role, "content": msg.text})
            continue

        # Blocks present — need special handling
        content_parts: list[dict] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []

        for blk in msg.blocks:
            if blk.type == "image" and blk.image_data:
                mt = blk.image_media_type or "image/png"
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mt};base64,{blk.image_data}"},
                })
            elif blk.type in ("text", "thinking") and blk.text:
                content_parts.append({"type": "text", "text": blk.text})
            elif blk.type == "tool_use":
                tool_calls.append({
                    "id": blk.tool_use_id or "",
                    "type": "function",
                    "function": {
                        "name": blk.tool_name or "",
                        "arguments": json.dumps(blk.tool_input or {}),
                    },
                })
            elif blk.type == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": blk.tool_result_for_id or "",
                    "content": blk.tool_result_content or "",
                })

        if tool_results:
            result.extend(tool_results)
        elif tool_calls or content_parts:
            api_msg: dict[str, Any] = {"role": msg.role}
            # Use content array when images are present, plain string otherwise
            has_images = any(p.get("type") == "image_url" for p in content_parts)
            if content_parts and has_images:
                api_msg["content"] = content_parts
            elif content_parts:
                api_msg["content"] = "\n".join(p["text"] for p in content_parts if p.get("text"))
            else:
                api_msg["content"] = None
            if tool_calls:
                api_msg["tool_calls"] = tool_calls
            result.append(api_msg)

    return result


class OpenAIProvider(BaseProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        name: str = "openai",
    ):
        self.name = name
        self.api_key = api_key or "dummy"
        self.base_url = base_url
        self._client = AsyncOpenAI(api_key=self.api_key, base_url=base_url)

    def is_available(self) -> bool:
        return True

    async def list_models(self) -> list[ModelInfo]:
        try:
            resp = await self._client.models.list()
            return [
                ModelInfo(
                    id=m.id,
                    name=m.id,
                    provider=self.name,
                    context_length=128_000,
                )
                for m in resp.data
            ]
        except Exception as exc:
            logger.debug("list_models failed for %s: %s", self.name, exc)
            return []

    async def stream(
        self,
        messages: list[NormalizedMessage],
        tools: list[dict],
        model: str,
        system: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        use_thinking = kwargs.get("thinking", True)

        # Gemma4: inject <|think|> token to enable chain-of-thought
        if _is_gemma4(model) and use_thinking:
            system = "<|think|>\n" + system if system else "<|think|>"

        # Parse <think>...</think> blocks for all OpenAI-compat models when thinking is on
        think_parser = _ThinkParser() if use_thinking else None

        api_messages = _to_api_messages(messages, system)

        params: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ]
            params["tool_choice"] = "auto"

        if "max_tokens" in kwargs:
            params["max_tokens"] = kwargs["max_tokens"]
        if "temperature" in kwargs:
            params["temperature"] = kwargs["temperature"]

        # Ollama: explicitly set think parameter to enable/disable thinking
        if self.name == "ollama":
            params["extra_body"] = {"think": use_thinking}

        # Accumulate tool call deltas by index
        tc_buf: dict[int, dict] = {}

        stream_ctx = await self._client.chat.completions.create(**params)

        logger.debug("OpenAI stream params: %s", {k: v for k, v in params.items() if k != "messages"})

        async for chunk in stream_ctx:
            logger.debug("RAW CHUNK: %s", chunk.model_dump_json(exclude_none=True))

            choice = chunk.choices[0] if chunk.choices else None
            if choice and choice.delta:
                delta = choice.delta

                # Log all delta fields for debugging
                logger.debug(
                    "DELTA fields: role=%s content=%s thinking=%s tool_calls=%s extras=%s",
                    delta.role,
                    repr(delta.content[:80]) if delta.content else None,
                    repr(getattr(delta, "thinking", None)),
                    bool(delta.tool_calls),
                    getattr(delta, "model_extra", {}),
                )

                # Ollama returns thinking in delta.reasoning (model_extra)
                thinking_text = (
                    getattr(delta, "reasoning", None)
                    or getattr(delta, "thinking", None)
                    or (getattr(delta, "model_extra", None) or {}).get("reasoning")
                )
                # do not add use_thinking condition here
                if thinking_text:
                    yield StreamEvent(type="thinking_delta", delta=thinking_text)

                if delta.content:
                    if think_parser:
                        for ev in think_parser.feed(delta.content):
                            yield ev
                    else:
                        yield StreamEvent(type="text_delta", delta=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tc_buf:
                            tc_buf[idx] = {"id": "", "name": "", "args": ""}
                        if tc_delta.id:
                            tc_buf[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tc_buf[idx]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tc_buf[idx]["args"] += tc_delta.function.arguments

            if chunk.usage:
                yield StreamEvent(
                    type="usage",
                    usage=TokenUsage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                    ),
                )

        # Flush any buffered text from the think parser
        if think_parser:
            for ev in think_parser.flush():
                yield ev

        # Emit completed tool calls
        for tc_data in tc_buf.values():
            try:
                tool_input = json.loads(tc_data["args"]) if tc_data["args"] else {}
            except json.JSONDecodeError:
                tool_input = {"_raw": tc_data["args"]}
            yield StreamEvent(
                type="tool_call",
                tool_id=tc_data["id"],
                tool_name=tc_data["name"],
                tool_input=tool_input,
            )
