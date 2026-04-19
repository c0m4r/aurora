"""Shared sandbox helpers for file tools.

All file tools operate inside a ./files/ sandbox directory relative to the
server's working directory. This module provides path resolution and validation
that is shared across file_tool, file_edit_tool, and scp_upload_tool.

Session isolation
-----------------
When a ``session_id`` is provided, files are transparently stored under
``./files/sessions/<session_id>/`` instead of ``./files/`` directly. The model
never sees the session prefix — it still uses plain paths like ``report.md``.
"""
from __future__ import annotations

import contextvars
import unicodedata
from pathlib import Path, PurePosixPath

SANDBOX_NAME = "files"
_SESSIONS_DIR = "sessions"

# Context variable set per-request by the agent loop so that tools
# automatically scope to the active conversation's directory.
_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_session", default=None,
)


def set_session(session_id: str | None) -> None:
    """Set the session ID for the current async context."""
    _current_session.set(session_id)


def get_session() -> str | None:
    """Return the session ID for the current async context (or None)."""
    return _current_session.get()


def sandbox(session_id: str | None = ...) -> Path:  # type: ignore[assignment]
    """Return the sandbox root, creating it if needed.

    If *session_id* is provided (or set via context), returns the
    session-scoped directory ``./files/sessions/<session_id>/``.
    Pass ``session_id=None`` explicitly to get the global ``./files/`` root.
    """
    if session_id is ...:
        session_id = get_session()

    root = Path.cwd() / SANDBOX_NAME
    if session_id:
        root = root / _SESSIONS_DIR / session_id

    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve(rel_path: str, session_id: str | None = ...) -> Path | None:  # type: ignore[assignment]
    """Resolve a relative path inside the sandbox.

    Handles common model mistakes:
    - Paths prefixed with ``files/`` or ``./files/`` (the model thinks it's
      writing from the project root, but the tool already operates inside
      ``./files/``).
    - Paths starting with ``./`` or ``/`` (treated as relative to sandbox).
    - Tilde paths like ``~/foo`` (rejected — sandbox only).
    - Path traversal attempts with ``..`` (rejected).

    Returns the resolved :class:`Path` on success, or ``None`` if the path
    escapes the sandbox.
    """
    if not rel_path or not rel_path.strip():
        return None

    # Null bytes would be silently truncated at the OS level on some platforms
    if "\x00" in rel_path:
        return None

    # NFC-normalise to prevent homoglyph / NFC-vs-NFD bypass on normalising filesystems
    clean = unicodedata.normalize("NFC", rel_path.strip())

    # Reject tilde paths — they would create a literal '~' directory
    if clean.startswith("~"):
        return None

    # Normalise using PurePosixPath to handle redundant separators and dots
    # without touching the filesystem.
    posix = PurePosixPath(clean)

    # Rebuild as a string without leading '/' so it stays relative
    parts = list(posix.parts)
    while parts and parts[0] in ("/", "."):
        parts.pop(0)

    # Strip accidental 'files/' or 'files' prefix that the model may include
    # (it already lives inside ./files/, so "files/report.md" should resolve
    # to ./files/report.md, not ./files/files/report.md).
    if parts and parts[0] == SANDBOX_NAME:
        parts.pop(0)

    # Reject empty path after stripping (caller should handle "." separately)
    if not parts:
        # Resolves to the sandbox root itself
        return sandbox(session_id).resolve()

    relative = Path(*parts)

    # Build the full path and resolve symlinks
    sb = sandbox(session_id)
    resolved = (sb / relative).resolve()

    # Ensure the resolved path is inside the sandbox
    try:
        resolved.relative_to(sb.resolve())
        return resolved
    except ValueError:
        return None  # path traversal attempt


def list_all_sessions() -> list[str]:
    """Return a list of session IDs that have files stored."""
    sessions_root = Path.cwd() / SANDBOX_NAME / _SESSIONS_DIR
    if not sessions_root.is_dir():
        return []
    return sorted(
        d.name for d in sessions_root.iterdir()
        if d.is_dir() and any(d.iterdir())
    )
