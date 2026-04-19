"""FastAPI application factory."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent.parent.parent / "web"


# ─── Security headers ────────────────────────────────────────────────────────
#
# CSP allows the pinned CDN sources used by web/index.html (highlight.js,
# marked, DOMPurify). script-src has NO 'unsafe-inline' — all event handlers
# use addEventListener / data-action delegation. Inline styles are still used
# in a few places so style-src retains 'unsafe-inline' for now.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://cdn.jsdelivr.net; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)

_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
}


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


class _RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter keyed by client IP.

    Chat/completions endpoints: 20 req/min (expensive LLM calls).
    Other API endpoints:         60 req/min.
    Static/web routes: unlimited.
    """

    _CHAT_LIMIT = 20
    _API_LIMIT = 60
    _WINDOW = 60  # seconds

    def __init__(self, app) -> None:
        super().__init__(app)
        # {bucket_key: deque of monotonic timestamps}
        self._windows: dict[str, deque] = defaultdict(deque)

    def _client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _check(self, key: str, limit: int) -> bool:
        """Slide the window and return True if the request is within the limit."""
        now = time.monotonic()
        cutoff = now - self._WINDOW
        dq = self._windows[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not (path.startswith("/api/") or path.startswith("/v1/")):
            return await call_next(request)

        ip = self._client_ip(request)
        if path in ("/api/chat/stream", "/v1/chat/completions"):
            allowed = self._check(f"chat:{ip}", self._CHAT_LIMIT)
        else:
            allowed = self._check(f"api:{ip}", self._API_LIMIT)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please slow down."},
                headers={"Retry-After": str(self._WINDOW)},
            )
        return await call_next(request)


def _cors_origins(cfg) -> list[str]:
    raw = getattr(getattr(cfg, "server", None), "cors_origins", None)
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return [o for o in raw if isinstance(o, str) and o]


def _compat_enabled(cfg) -> bool:
    """/v1 OpenAI-compatible router is opt-in (off by default)."""
    return bool(getattr(getattr(cfg, "server", None), "enable_openai_compat", False))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────
    from ..config import get as get_cfg
    from ..providers.registry import get_registry
    from ..memory.store import get_store

    cfg = get_cfg()

    # Init memory DB
    store = get_store()
    await store.init()
    logger.info("Memory store initialised at %s", store.db_path)

    # Register model providers
    registry = get_registry()
    registry.from_config(cfg)
    logger.info("Providers registered: %s", registry.provider_names)

    yield
    # ── Shutdown ─────────────────────────────────────────────────────────
    logger.info("Aurora shutting down")


def create_app() -> FastAPI:
    from ..config import get as get_cfg

    cfg = get_cfg()

    app = FastAPI(
        title="Aurora",
        version="1.0.0",
        description="General purpose AI agent",
        lifespan=_lifespan,
    )

    origins = _cors_origins(cfg)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "X-API-Key"],
        )

    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(_RateLimitMiddleware)

    # API routes
    from .routes.chat import router as chat_router

    app.include_router(chat_router)

    if _compat_enabled(cfg):
        from .routes.compat import router as compat_router
        app.include_router(compat_router)
        logger.warning(
            "OpenAI-compatible /v1 router is ENABLED. "
            "Ensure server.api_key is set — /v1 endpoints require the same key as /api."
        )
    else:
        logger.info("OpenAI-compatible /v1 router is disabled (server.enable_openai_compat=false).")

    # Serve web UI static assets
    if WEB_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        @app.get("/{path:path}", include_in_schema=False)
        async def serve_spa(path: str = ""):
            # All non-API routes → index.html (SPA)
            if path.startswith(("api/", "v1/")):
                from fastapi import HTTPException
                raise HTTPException(404)
            index = WEB_DIR / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return {"message": "Aurora API running. Place web/index.html to serve UI."}

    return app


app = create_app()


def run():
    """Entry point for aurora-server CLI."""
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    parser = argparse.ArgumentParser(description="Aurora Server")
    parser.add_argument("--config", "-c", default=None, help="Config file path")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)-20s %(levelname)s %(message)s",
    )

    from ..config import load as load_cfg, get as get_cfg
    from .auth import validate_auth_config

    load_cfg(args.config)
    cfg = get_cfg()

    host = args.host or getattr(getattr(cfg, "server", None), "host", "127.0.0.1")
    port = args.port or int(getattr(getattr(cfg, "server", None), "port", 8000) or 8000)

    # Fail closed on dangerous auth configurations before binding the port.
    validate_auth_config(host)

    import uvicorn

    uvicorn.run(
        "aurora.api.app:app",
        host=host,
        port=port,
        log_level=args.log_level.lower(),
        reload=False,
    )
