"""Auto-extract solutions from tool-use conversations."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

from ..memory.store import MemoryStore
from ..providers.base import NormalizedMessage
from ..providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT = """\
Analyze the conversation turn below. If a specific technical problem was \
solved (via SSH commands, scripts, web lookups, file operations, etc.), \
extract it as a reusable solution.

Rules:
- Only extract when a real problem was actively solved — skip greetings, \
chitchat, time checks, simple factual answers, and status-only lookups \
(e.g. just checking uptime without fixing anything).
- The problem should be general enough to help with similar future situations.
- The solution must include key commands or concrete steps, not just a summary.
- Return **only** valid JSON, no markdown fences.

If worth saving:
{{"save": true, "title": "short title (under 60 chars)", \
"problem": "what was the problem", \
"solution": "how it was solved — include key commands/steps", \
"tags": ["relevant", "tags"]}}

If NOT worth saving:
{{"save": false}}

---

## User Message
{user_message}

## Assistant Response
{assistant_text}

## Tool Calls
{tool_log}
"""


async def run_extract(
    registry: ProviderRegistry,
    model_id: str,
    user_message: str,
    assistant_text: str,
    tool_log: list[dict],
    store: MemoryStore,
) -> AsyncIterator[dict]:
    """Stream learn events: extracting → text deltas → saved/skipped."""
    if not tool_log:
        return

    # Build tool log summary
    log_lines: list[str] = []
    for entry in tool_log:
        log_lines.append(f"### {entry['name']}({json.dumps(entry['input'])})")
        output = entry.get("output", "")
        if len(output) > 500:
            output = output[:500] + "…"
        log_lines.append(output)
    tool_log_str = "\n".join(log_lines)

    prompt = _EXTRACT_PROMPT.format(
        user_message=user_message[:2000],
        assistant_text=assistant_text[:2000],
        tool_log=tool_log_str[:3000],
    )

    try:
        logger.info("Learner: extracting solution from %d tool interactions", len(tool_log))
        yield {"type": "learn", "status": "extracting"}

        # Stream the LLM response so the user sees it
        messages = [NormalizedMessage(role="user", text=prompt)]
        buf: list[str] = []
        async for event in registry.stream(
            model_id=model_id,
            messages=messages,
            tools=[],
            system="You extract structured data from conversations. Output only valid JSON.",
            thinking=True,
            max_tokens=4096,
        ):
            if event.type == "text_delta":
                buf.append(event.delta)
                yield {"type": "learn", "status": "text", "content": event.delta}
            elif event.type == "thinking_delta":
                yield {"type": "learn", "status": "thinking", "content": event.delta}

        text = "".join(buf)
        logger.debug("Learner: raw LLM response: %s", text[:500])
        result = _parse_json(text)

        if not result or not result.get("save"):
            logger.info("Learner: nothing worth saving")
            yield {"type": "learn", "status": "skipped"}
            return

        problem = _sanitize(result.get("problem", ""), _MAX_PROBLEM_LEN)
        solution = _sanitize(result.get("solution", ""), _MAX_SOLUTION_LEN)
        if not problem or not solution:
            logger.info("Learner: solution rejected (empty or injection attempt)")
            yield {"type": "learn", "status": "skipped"}
            return

        # Dedup: check if a similar solution already exists
        existing = await store.search_solutions(problem, limit=3)
        for ex in existing:
            if (
                ex.get("title", "").lower() == result.get("title", "").lower()
                or _overlap(ex.get("problem", ""), problem) > 0.6
            ):
                logger.info("Learner: similar solution already exists (id=%s)", ex.get("id"))
                yield {"type": "learn", "status": "skipped", "reason": "duplicate"}
                return

        title = result.get("title", problem[:60])
        tags = result.get("tags", [])
        sid = await store.save_solution(
            problem=problem,
            solution=solution,
            title=title,
            tags=tags,
        )
        logger.info("Learner: auto-saved solution #%s — %s", sid, title)
        yield {
            "type": "learn", "status": "saved",
            "id": sid, "title": title, "problem": problem,
            "solution": solution, "tags": tags,
        }

    except Exception:
        logger.warning("Learner: extraction failed", exc_info=True)
        yield {"type": "learn", "status": "error"}


_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_INJECTION_RE = re.compile(
    r"SYSTEM\s*:|###\s*system\b|ignore\s+(?:previous|prior|above)\s+instructions"
    r"|<\s*script\b|<\s*iframe\b|javascript\s*:",
    re.IGNORECASE,
)
_MAX_PROBLEM_LEN = 1000
_MAX_SOLUTION_LEN = 3000


def _sanitize(text: str, max_len: int) -> str | None:
    """Strip HTML tags, enforce length cap, reject prompt-injection attempts."""
    text = _HTML_TAG_RE.sub("", text).strip()
    if _INJECTION_RE.search(text):
        return None
    return text[:max_len]


def _parse_json(text: str) -> dict | None:
    """Parse JSON from LLM output, stripping markdown fences if present."""
    text = text.strip()
    # Strip ```json ... ``` fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _overlap(a: str, b: str) -> float:
    """Word-level Jaccard similarity."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
