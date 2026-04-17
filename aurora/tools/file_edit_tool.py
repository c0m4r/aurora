"""File edit tool — make precise SEARCH/REPLACE edits to files in ./files/ sandbox.

The model specifies exact text to find and what to replace it with.
Multiple edits can be applied in a single call.
Always returns a unified diff of the changes.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolDefinition
from .sandbox import resolve as _resolve

logger = logging.getLogger(__name__)


class FileEditTool(BaseTool):
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="file_edit",
            description=(
                "Make precise edits to a file inside the local ./files/ sandbox.\n\n"
                "**ALWAYS call `file_read` first** to see the exact current content — the SEARCH "
                "text must match character-for-character (whitespace, indentation, newlines).\n\n"
                "**Rules:**\n"
                "1. SEARCH text must match the file EXACTLY — copy it from `file_read` output.\n"
                "2. Include 2-3 lines of context so the match is unique.\n"
                "3. REPLACE is what the matched SEARCH becomes (leave empty to delete).\n"
                "4. One SEARCH/REPLACE pair edits one section; use multiple for multiple changes.\n\n"
                "**Preferred format** for `edits` — one string with SEARCH/REPLACE blocks:\n"
                "```\n"
                "<<<<<<< SEARCH\n"
                "exact existing lines\n"
                "=======\n"
                "replacement lines\n"
                ">>>>>>> REPLACE\n"
                "```\n\n"
                "Also accepted: a JSON array of `{\"search\": \"...\", \"replace\": \"...\"}` objects."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path inside ./files/ of the file to edit. "
                            "Examples: 'report.md', 'scripts/setup.sh'."
                        ),
                    },
                    "edits": {
                        "type": "string",
                        "description": (
                            "One or more edits. Two accepted formats:\n\n"
                            "**(A) SEARCH/REPLACE blocks** (preferred — concatenate multiple in one string):\n"
                            "<<<<<<< SEARCH\n"
                            "exact existing text\n"
                            "=======\n"
                            "replacement text\n"
                            ">>>>>>> REPLACE\n\n"
                            "**(B) JSON array of edit objects**:\n"
                            "[{\"search\": \"exact existing text\", \"replace\": \"replacement text\"}]\n\n"
                            "Example — change a variable value (format A):\n"
                            "<<<<<<< SEARCH\n"
                            "MAX_RETRIES = 3\n"
                            "=======\n"
                            "MAX_RETRIES = 10\n"
                            ">>>>>>> REPLACE\n\n"
                            "Example — add a line after an existing line (format A):\n"
                            "<<<<<<< SEARCH\n"
                            "#!/bin/bash\n"
                            "=======\n"
                            "#!/bin/bash\n"
                            "set -euo pipefail\n"
                            ">>>>>>> REPLACE\n\n"
                            "The SEARCH text must match the file's current content exactly — "
                            "read the file first with file_read if unsure."
                        ),
                    },
                },
                "required": ["path", "edits"],
            },
        )

    async def execute(self, path: str, edits: Any, **_) -> str:
        if not path or not path.strip():
            return "Error: path must not be empty."

        edits_text = _normalize_edits(edits)

        target = _resolve(path)
        if target is None:
            return "[BLOCKED] Path traversal outside ./files/ is not allowed."

        if not target.exists():
            return f"Not found: files/{path}. Use file_write to create it first."

        if not target.is_file():
            return f"Not a file: files/{path} (it's a directory)."

        try:
            original = target.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Error reading files/{path}: {exc}"

        # Parse SEARCH/REPLACE blocks
        parsed = _parse_edits(edits_text)
        if not parsed:
            return (
                "Error: no valid SEARCH/REPLACE blocks found in `edits`.\n\n"
                "Pass either (A) SEARCH/REPLACE blocks as a string:\n"
                "<<<<<<< SEARCH\n"
                "exact existing lines\n"
                "=======\n"
                "replacement lines\n"
                ">>>>>>> REPLACE\n\n"
                "or (B) a JSON array: [{\"search\": \"...\", \"replace\": \"...\"}]"
            )

        # Apply edits sequentially
        content = original
        for search_text, replace_text in parsed:
            if search_text not in content:
                # Report which SEARCH block failed
                preview = search_text[:120].replace("\n", "\\n")
                return (
                    f"Error: SEARCH block not found in file:\n"
                    f"  {preview}{'...' if len(search_text) > 120 else ''}\n\n"
                    f"Read the file first with file_read to confirm exact content, "
                    f"then retry with the correct SEARCH text."
                )
            # Replace only the FIRST occurrence (to avoid unintended multiple replacements)
            content = content.replace(search_text, replace_text, 1)

        if content == original:
            return f"No changes applied to files/{path}."

        # Write the result
        try:
            target.write_text(content, encoding="utf-8")
        except Exception as exc:
            return f"Error writing files/{path}: {exc}"

        # Generate unified diff
        diff = _make_diff(f"files/{path}", original, content)

        lines_changed = sum(
            1 for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
            or line.startswith("-") and not line.startswith("---")
        )

        return f"Edited files/{path} ({lines_changed} line{'s' if lines_changed != 1 else ''} changed).\n\n{diff}"


_SEARCH_KEYS = ("search", "old", "old_string", "find", "from", "before")
_REPLACE_KEYS = ("replace", "new", "new_string", "to", "after", "replacement")


def _normalize_edits(edits: Any) -> str:
    """Coerce the model's `edits` argument into a SEARCH/REPLACE block string.

    Accepts:
    - a string with SEARCH/REPLACE blocks (preferred)
    - a JSON-encoded list/object of edit records (e.g. [{"search":..., "replace":...}])
    - an already-parsed list/dict
    """
    if edits is None:
        return ""

    if isinstance(edits, str):
        stripped = edits.strip()
        if stripped.startswith(("[", "{")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return edits
            return _records_to_blocks(parsed) or edits
        return edits

    return _records_to_blocks(edits)


def _records_to_blocks(data: Any) -> str:
    """Convert a list/dict of edit records into SEARCH/REPLACE block text."""
    items = data if isinstance(data, list) else [data]
    blocks: list[str] = []
    for item in items:
        if isinstance(item, str):
            blocks.append(item)
            continue
        if not isinstance(item, dict):
            continue
        search = _pick(item, _SEARCH_KEYS)
        if search is None:
            continue
        replace = _pick(item, _REPLACE_KEYS) or ""
        blocks.append(
            f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"
        )
    return "\n\n".join(blocks)


def _pick(d: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        if k in d and d[k] is not None:
            return str(d[k])
    return None


def _parse_edits(edits: str) -> list[tuple[str, str]]:
    """Parse SEARCH/REPLACE blocks from the edits string."""
    results = []
    lines = edits.split("\n")
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "<<<<<<< SEARCH":
            # Collect SEARCH lines
            search_lines = []
            i += 1
            while i < len(lines) and lines[i].strip() != "=======":
                search_lines.append(lines[i])
                i += 1

            # Skip ======= separator
            if i < len(lines):
                i += 1  # skip '======='

            # Collect REPLACE lines
            replace_lines = []
            while i < len(lines) and lines[i].strip() != ">>>>>>> REPLACE":
                replace_lines.append(lines[i])
                i += 1

            # Skip >>>>>>> REPLACE marker
            if i < len(lines):
                i += 1

            # Reconstruct — preserve newlines
            search_text = "\n".join(search_lines)
            replace_text = "\n".join(replace_lines)

            if search_text:
                results.append((search_text, replace_text))
        else:
            i += 1

    return results


def _make_diff(path: str, old: str, new: str) -> str:
    """Generate a unified diff string."""
    import difflib

    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=4,
    )
    return "".join(diff)
