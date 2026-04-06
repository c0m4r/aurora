"""Canonical internal message types and abstract provider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Optional


# ─── Canonical content blocks ────────────────────────────────────────────────

@dataclass
class ContentBlock:
    type: Literal["text", "thinking", "tool_use", "tool_result", "image"]
    # text / thinking
    text: Optional[str] = None
    # image (base64)
    image_data: Optional[str] = None
    image_media_type: Optional[str] = None  # e.g. "image/png"
    # tool_use
    tool_use_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    # tool_result
    tool_result_for_id: Optional[str] = None
    tool_result_content: Optional[str] = None
    tool_is_error: bool = False


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class NormalizedMessage:
    role: Literal["user", "assistant", "system"]
    # Simple string content (most user/system messages)
    text: Optional[str] = None
    # Rich content blocks (assistant responses with tools/thinking)
    blocks: list[ContentBlock] = field(default_factory=list)
    usage: Optional[TokenUsage] = None

    @property
    def content_text(self) -> str:
        """Flatten all text blocks to a single string."""
        if self.text is not None:
            return self.text
        return "".join(b.text or "" for b in self.blocks if b.type in ("text",))


# ─── Streaming events ─────────────────────────────────────────────────────────

@dataclass
class StreamEvent:
    """Events emitted by provider.stream() and forwarded to SSE clients."""
    type: Literal[
        "thinking_delta",
        "text_delta",
        "tool_call",
        "tool_result",
        "usage",
        "done",
        "error",
    ]
    # delta content
    delta: str = ""
    # tool
    tool_id: str = ""
    tool_name: str = ""
    tool_input: Optional[dict] = None
    tool_output: Optional[str] = None
    tool_error: bool = False
    # usage
    usage: Optional[TokenUsage] = None
    # error
    error: str = ""


# ─── Model metadata ───────────────────────────────────────────────────────────

@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str
    context_length: int = 128_000
    supports_tools: bool = True
    supports_thinking: bool = False

    @property
    def full_id(self) -> str:
        return f"{self.provider}/{self.id}"


# ─── Abstract provider ────────────────────────────────────────────────────────

class BaseProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def stream(
        self,
        messages: list[NormalizedMessage],
        tools: list[dict],  # JSON-schema tool definitions
        model: str,
        system: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Yield StreamEvents for a single LLM turn (no tool execution here)."""
        ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...
