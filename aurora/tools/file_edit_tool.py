"""File edit tool — make precise SEARCH/REPLACE edits to files in ./files/ sandbox.

The model specifies exact text to find and what to replace it with.
Multiple edits can be applied in a single call.
Always returns a unified diff of the changes.
"""
from __future__ import annotations

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
                "Make precise edits to a file inside the local ./files/ sandbox. "
                "Use SEARCH/REPLACE blocks to change specific parts of a file without "
                "rewriting it entirely.\n\n"
                "**Rules:**\n"
                "1. The SEARCH text must match the file content EXACTLY — character for character, "
                "including whitespace, indentation, and newlines.\n"
                "2. Always include enough context (2-3 lines) to uniquely identify the location.\n"
                "3. The REPLACE text is what the matched SEARCH block becomes.\n"
                "4. Each SEARCH/REPLACE block edits ONE section; use multiple blocks for multiple changes.\n"
                "5. SEARCH blocks must match existing content — you cannot search for something that "
                "doesn't exist in the file. Read the file first if unsure.\n\n"
                "**Format** for each edit:\n"
                "```\n"
                "<<<<<<< SEARCH\n"
                "exact existing lines to find\n"
                "=======\n"
                "replacement lines\n"
                ">>>>>>> REPLACE\n"
                "```\n\n"
                "Multiple SEARCH/REPLACE blocks can be provided in a single call, applied top-to-bottom."
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
                            "One or more SEARCH/REPLACE blocks.\n\n"
                            "Format:\n"
                            "<<<<<<< SEARCH\n"
                            "exact existing text to match\n"
                            "=======\n"
                            "replacement text\n"
                            ">>>>>>> REPLACE\n\n"
                            "Example — change a variable value:\n"
                            "<<<<<<< SEARCH\n"
                            "MAX_RETRIES = 3\n"
                            "=======\n"
                            "MAX_RETRIES = 10\n"
                            ">>>>>>> REPLACE\n\n"
                            "Example — add a line after an existing line:\n"
                            "<<<<<<< SEARCH\n"
                            "#!/bin/bash\n"
                            "=======\n"
                            "#!/bin/bash\n"
                            "set -euo pipefail\n"
                            ">>>>>>> REPLACE"
                        ),
                    },
                },
                "required": ["path", "edits"],
            },
        )

    async def execute(self, path: str, edits: str, **_) -> str:
        if not path or not path.strip():
            return "Error: path must not be empty."

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
        parsed = _parse_edits(edits)
        if not parsed:
            return (
                "Error: no valid SEARCH/REPLACE blocks found.\n\n"
                "Format each block like this:\n"
                "<<<<<<< SEARCH\n"
                "exact existing lines\n"
                "=======\n"
                "replacement lines\n"
                ">>>>>>> REPLACE"
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
