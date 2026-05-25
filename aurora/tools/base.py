from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON Schema object


class BaseTool(ABC):
    @abstractmethod
    def definition(self) -> ToolDefinition:
        ...

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """Run the tool and return a plain-text or JSON string result."""
        ...

    def approval_warning(self, tool_input: dict) -> str | None:
        """Optional warning shown above the secure-mode approval prompt.

        Return a short message when the given input would normally be refused
        by a built-in guard (e.g. non-whitelisted domain). A non-None return
        also tells the loop to pass `_secure_override=True` so the tool can
        bypass that guard once the user has explicitly approved.
        """
        return None

    def to_dict(self) -> dict:
        d = self.definition()
        return {
            "name": d.name,
            "description": d.description,
            "parameters": d.parameters,
        }
