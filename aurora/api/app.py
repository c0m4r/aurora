"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent.parent.parent / "web"


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
    app = FastAPI(
        title="Aurora",
        version="1.0.0",
        description="General purpose AI agent",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    from .routes.chat import router as chat_router
    from .routes.compat import router as compat_router

    app.include_router(chat_router)
    app.include_router(compat_router)

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

    load_cfg(args.config)
    cfg = get_cfg()

    host = args.host or getattr(getattr(cfg, "server", None), "host", "0.0.0.0")
    port = args.port or int(getattr(getattr(cfg, "server", None), "port", 8000) or 8000)

    import uvicorn

    uvicorn.run(
        "aurora.api.app:app",
        host=host,
        port=port,
        log_level=args.log_level.lower(),
        reload=False,
    )
