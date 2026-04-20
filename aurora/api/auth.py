"""API key authentication dependency."""
from __future__ import annotations

import hmac
import logging
import secrets
import string

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

_SENTINEL_KEYS = {"change-me-please", "change-me-in-production"}
_OTP_ALPHABET = string.ascii_uppercase + string.digits
_otp: str | None = None


def generate_otp() -> str:
    global _otp
    _otp = ''.join(secrets.choice(_OTP_ALPHABET) for _ in range(8))
    return _otp


def get_otp() -> str | None:
    return _otp


def validate_key(provided: str) -> bool:
    """Accept either the configured API key or the current OTP."""
    if not provided:
        return False
    expected = _configured_key()
    if expected and expected not in _SENTINEL_KEYS and hmac.compare_digest(provided, expected):
        return True
    otp = _otp
    if otp and hmac.compare_digest(provided, otp):
        return True
    return False


def _configured_key() -> str:
    from ..config import get
    cfg = get()
    return getattr(getattr(cfg, "server", None), "api_key", None) or ""


def _auth_disabled() -> bool:
    """Auth is considered disabled only when explicitly opted out via config."""
    from ..config import get
    cfg = get()
    return bool(getattr(getattr(cfg, "server", None), "allow_unauthenticated", False))


def require_api_key(
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
) -> str:
    if _auth_disabled():
        return ""

    expected = _configured_key()
    if not expected or expected in _SENTINEL_KEYS:
        # Fail closed. validate_auth_config() should have caught this at startup,
        # but guard here too so a misconfig never silently opens the server.
        raise HTTPException(
            status_code=503,
            detail=(
                "Server is misconfigured: server.api_key is empty or still a sentinel value. "
                "Set a real key in config.yaml, or explicitly set server.allow_unauthenticated: true."
            ),
        )

    provided = ""
    if x_api_key:
        provided = x_api_key
    elif authorization and authorization.startswith("Bearer "):
        provided = authorization[7:]

    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return provided


def validate_auth_config(host: str) -> None:
    """Raise at startup if the auth configuration is dangerous.

    Rules:
      - Empty or sentinel API key is refused unless allow_unauthenticated=true.
      - allow_unauthenticated=true combined with a non-loopback bind is refused
        unless the operator explicitly sets allow_unauthenticated_public=true.
    """
    expected = _configured_key()
    allow_unauth = _auth_disabled()

    key_is_usable = bool(expected) and expected not in _SENTINEL_KEYS

    if not allow_unauth and not key_is_usable:
        raise RuntimeError(
            "Refusing to start: server.api_key is empty or still a sentinel value "
            f"({expected!r}). Set a real key in config.yaml, or (only for local use) "
            "set server.allow_unauthenticated: true."
        )

    if allow_unauth:
        loopback = host in ("127.0.0.1", "::1", "localhost")
        from ..config import get
        cfg = get()
        allow_public = bool(getattr(getattr(cfg, "server", None), "allow_unauthenticated_public", False))
        if not loopback and not allow_public:
            raise RuntimeError(
                "Refusing to start: server.allow_unauthenticated=true combined with a "
                f"non-loopback bind ({host!r}) would expose the agent and all tools to the "
                "network with no authentication. Bind to 127.0.0.1, set a real api_key, "
                "or (accepting full responsibility) set server.allow_unauthenticated_public: true."
            )
        logger.warning(
            "AUTH DISABLED — server.allow_unauthenticated=true. "
            "Every endpoint is reachable without credentials. Host=%s",
            host,
        )
