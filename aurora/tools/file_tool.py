"""File tools — read and write files inside the ./files/ sandbox directory.

The agent can only access paths under ./files/ (relative to the server's
working directory). Path traversal attempts are blocked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import BaseTool, ToolDefinition

# Sandbox root — always resolved relative to cwd at call time so it works
# regardless of where the server is started from.
_SANDBOX_NAME = "files"


def _sandbox() -> Path:
    root = Path.cwd() / _SANDBOX_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve(rel_path: str) -> Path | None:
    """Resolve a relative path inside the sandbox. Returns None on traversal."""
    sandbox = _sandbox()
    # Normalise: strip leading slashes / dots so the path stays relative
    clean = rel_path.lstrip("/").lstrip("./")
    resolved = (sandbox / clean).resolve()
    try:
        resolved.relative_to(sandbox.resolve())
        return resolved
    except ValueError:
        return None  # path traversal attempt


class FileReadTool(BaseTool):
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="file_read",
            description=(
                "Read a file or list a directory inside the local ./files/ sandbox. "
                "All paths are relative to ./files/ — you cannot access files outside it. "
                "Use this to read files you previously wrote, check their contents, or "
                "browse what is available."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path inside ./files/. "
                            "Examples: 'report.md', 'scripts/setup.sh', '' or '.' to list root."
                        ),
                        "default": ".",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Max lines to return for large files (default: 500).",
                        "default": 500,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, path: str = ".", max_lines: int = 500, **_) -> str:
        target = _resolve(path or ".")
        if target is None:
            return "[BLOCKED] Path traversal outside ./files/ is not allowed."

        if not target.exists():
            return f"Not found: files/{path}"

        if target.is_dir():
            return _list_dir(target)

        # Read file
        try:
            text = target.read_text(errors="replace")
        except PermissionError:
            return f"Permission denied: files/{path}"
        except Exception as exc:
            return f"Error reading files/{path}: {exc}"

        lines = text.splitlines()
        if len(lines) > max_lines:
            preview = "\n".join(lines[:max_lines])
            return (
                f"files/{path} ({len(lines)} lines, showing first {max_lines}):\n\n"
                f"{preview}\n\n[…{len(lines) - max_lines} more lines]"
            )
        size = target.stat().st_size
        return f"files/{path} ({size} bytes):\n\n{text}"


class FileWriteTool(BaseTool):
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="file_write",
            description=(
                "Create or overwrite a file inside the local ./files/ sandbox. "
                "You can also append to an existing file. "
                "Parent directories are created automatically. "
                "All paths are relative to ./files/ — you cannot write outside it. "
                "Use this to save reports, scripts, config snippets, notes, or any "
                "content the user wants to keep."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path inside ./files/. "
                            "Examples: 'report.md', 'scripts/setup.sh', 'data/output.json'. "
                            "Parent dirs are created if they don't exist."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write.",
                    },
                    "append": {
                        "type": "boolean",
                        "description": "If true, append to the file instead of overwriting. Default: false.",
                        "default": False,
                    },
                },
                "required": ["path", "content"],
            },
        )

    async def execute(self, path: str, content: str, append: bool = False, **_) -> str:
        if not path or not path.strip():
            return "Error: path must not be empty."

        target = _resolve(path)
        if target is None:
            return "[BLOCKED] Path traversal outside ./files/ is not allowed."

        # Create parent directories
        target.parent.mkdir(parents=True, exist_ok=True)

        mode = "a" if append else "w"
        try:
            with open(target, mode, encoding="utf-8") as fh:
                fh.write(content)
        except PermissionError:
            return f"Permission denied: files/{path}"
        except Exception as exc:
            return f"Error writing files/{path}: {exc}"

        action = "Appended to" if append else "Wrote"
        size = target.stat().st_size
        return f"{action} files/{path} ({size} bytes)."


def _list_dir(d: Path) -> str:
    sandbox = _sandbox()
    try:
        entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return f"Permission denied: {d}"

    if not entries:
        rel = str(d.relative_to(sandbox))
        return f"files/{rel} is empty." if rel != "." else "files/ is empty."

    lines = []
    for entry in entries:
        rel = entry.relative_to(sandbox)
        if entry.is_dir():
            sub_count = sum(1 for _ in entry.iterdir())
            lines.append(f"  📁  {rel}/  ({sub_count} items)")
        else:
            size = entry.stat().st_size
            lines.append(f"  📄  {rel}  ({_human_size(size)})")

    rel_dir = str(d.relative_to(sandbox))
    header = f"files/{rel_dir}/" if rel_dir != "." else "files/"
    return f"{header}\n" + "\n".join(lines)


def _human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"
