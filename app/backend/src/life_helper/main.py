"""FastAPI application factory."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .config import Settings, get_settings
from .context import AppContext, build_context

logger = logging.getLogger(__name__)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://github.com"
)


def _session_secret(settings: Settings) -> str:
    value = settings.session_secret.get_secret_value()
    if value:
        return value
    # Only reachable in development (production validation requires LH_SESSION_SECRET).
    path = settings.app_state_dir / "secrets" / "dev-session.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
    return path.read_text().strip()


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    settings = settings or get_settings()
    ctx = ctx or build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .bootstrap import shutdown_services, start_services
        from .security import install_log_masking

        install_log_masking(ctx.masker)
        await start_services(ctx)
        try:
            yield
        finally:
            await shutdown_services(ctx)

    app = FastAPI(title="Life Helper", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(settings),
        session_cookie="lh_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=not settings.is_dev,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    app.include_router(auth.router)
    from .bootstrap import include_routers

    include_routers(app)
    _mount_frontend(app, settings.static_dir)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    index = static_dir / "index.html"

    static_root = static_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith(("api/", "auth/")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (static_root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(static_root):
            return FileResponse(candidate)
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "frontend is not built"}, status_code=404)


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("life_helper.main:create_app", factory=True, host="0.0.0.0", port=8000, proxy_headers=True)  # noqa: S104
