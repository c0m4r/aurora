"""API key authentication dependency."""
from __future__ import annotations

from fastapi import Header, HTTPException


def require_api_key(
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
) -> str:
    from ..config import get
    cfg = get()
    expected = getattr(getattr(cfg, "server", None), "api_key", None) or ""

    # If no key configured or still the example value, allow all
    if not expected or expected in ("change-me-please", "change-me-in-production"):
        return ""

    provided = ""
    if x_api_key:
        provided = x_api_key
    elif authorization and authorization.startswith("Bearer "):
        provided = authorization[7:]

    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return provided
