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

    def to_dict(self) -> dict:
        d = self.definition()
        return {
            "name": d.name,
            "description": d.description,
            "parameters": d.parameters,
        }
