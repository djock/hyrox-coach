"""Application factory."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, db
from .config import Config, load_config
from .plan import load_plan

log = logging.getLogger(__name__)

PUBLIC_PATHS = {"/login", "/healthz", "/manifest.webmanifest", "/sw.js"}


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    app = FastAPI(title="Hyrox Coach", docs_url=None, redoc_url=None)

    conn = db.connect(config.db_path)
    db.migrate(conn)
    plan = load_plan()
    db.seed(conn, plan, plan_start=config.plan_start)

    app.state.config = config
    app.state.conn = conn

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def attach_principal(request: Request, call_next):
        request.state.principal = auth.read_cookie(
            config, request.cookies.get(auth.COOKIE_NAME)
        )
        path = request.url.path
        if (
            request.state.principal is None
            and path not in PUBLIC_PATHS
            and not path.startswith("/static/")
        ):
            if path.startswith("/api/"):
                return Response(status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # The service worker must be served from the origin root to control the
    # whole scope; a file under /static/ could only control /static/.
    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> Response:
        return Response(
            (static_dir / "sw.js").read_text(),
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> Response:
        return Response(
            (static_dir / "manifest.webmanifest").read_text(),
            media_type="application/manifest+json",
        )

    from .web import router

    app.include_router(router)

    if config.dev_mode:
        log.warning("HYROX_SESSION_SECRET unset -- running with an insecure dev secret")

    return app


def main() -> None:  # pragma: no cover -- convenience entry point
    import uvicorn

    uvicorn.run("hyrox.app:create_app", factory=True, host="0.0.0.0", port=8000)
