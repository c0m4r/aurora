"""File tools — read and write files inside the ./files/ sandbox directory.

The agent can only access paths under ./files/ (relative to the server's
working directory). Path traversal attempts are blocked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import BaseTool, ToolDefinition
from .sandbox import sandbox as _sandbox, resolve as _resolve, list_all_sessions


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
                    "all_sessions": {
                        "type": "boolean",
                        "description": (
                            "If true, list files from ALL conversation sessions "
                            "(not just the current one). Only useful with path='.' "
                            "when the user asks about files from other conversations."
                        ),
                        "default": False,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, path: str = ".", max_lines: int = 500, all_sessions: bool = False, **_) -> str:
        # Cross-session listing
        if all_sessions and (not path or path in (".", "")):
            return _list_all_sessions()

        target = _resolve(path or ".")
        if target is None:
            if path and path.strip().startswith("~"):
                return (
                    "[BLOCKED] Tilde paths like '~/...' are not supported. "
                    "All paths are relative to the ./files/ sandbox. "
                    "Use just the filename, e.g. 'test.py' instead of '~/test.py'."
                )
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
                "IMPORTANT: Do NOT include 'files/' or './files/' in the path — "
                "it is added automatically. Use 'report.md', not './files/report.md'. "
                "Do NOT use tilde paths like '~/file.py' — use just 'file.py'. "
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
            if path and path.strip().startswith("~"):
                return (
                    "[BLOCKED] Tilde paths like '~/...' are not supported. "
                    "All paths are relative to the ./files/ sandbox. "
                    "Use just the filename, e.g. 'script.py' instead of '~/script.py'."
                )
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
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        ext = Path(path).suffix.lstrip(".")
        lang = _EXT_TO_LANG.get(ext, ext) if ext else ""

        # Build result with an embedded code preview for the frontend
        header = f"{action} files/{path} ({size} bytes, {line_count} lines)."
        # Include full content as a fenced code block so the frontend can
        # render it with syntax highlighting.
        preview = content if len(content) <= 20_000 else content[:20_000] + "\n…[truncated]"
        return f"{header}\n\n```{lang}\n{preview}\n```"


# Common file extensions → highlight.js language identifiers
_EXT_TO_LANG: dict[str, str] = {
    "py": "python", "js": "javascript", "ts": "typescript", "jsx": "jsx",
    "tsx": "tsx", "rb": "ruby", "rs": "rust", "go": "go", "java": "java",
    "c": "c", "cpp": "cpp", "h": "c", "hpp": "cpp", "cs": "csharp",
    "sh": "bash", "bash": "bash", "zsh": "bash", "fish": "fish",
    "html": "html", "htm": "html", "css": "css", "scss": "scss",
    "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml",
    "xml": "xml", "sql": "sql", "md": "markdown", "txt": "",
    "dockerfile": "dockerfile", "makefile": "makefile",
    "conf": "ini", "ini": "ini", "cfg": "ini", "env": "bash",
    "php": "php", "pl": "perl", "lua": "lua", "swift": "swift",
    "kt": "kotlin", "r": "r", "m": "matlab",
}


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


def _list_all_sessions() -> str:
    """List files across all conversation sessions."""
    sessions = list_all_sessions()
    if not sessions:
        return "No session files found."

    lines = ["=== Files across all sessions ===\n"]
    root = Path.cwd() / "files" / "sessions"
    for sid in sessions:
        session_dir = root / sid
        file_count = sum(1 for f in session_dir.rglob("*") if f.is_file())
        lines.append(f"  Session {sid[:12]}…  ({file_count} file{'s' if file_count != 1 else ''})")
        for f in sorted(session_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(session_dir)
                lines.append(f"    📄  {rel}  ({_human_size(f.stat().st_size)})")
    lines.append(f"\nTotal: {len(sessions)} session(s)")
    return "\n".join(lines)


def _human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"
