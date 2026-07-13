"""FastAPI app entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import Scope

from .auth import router as auth_router
from .config import get_settings
from .jira_client import make_httpx_client
from .routes import router

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that tells browsers never to cache — the app is fast to
    re-fetch and we don't want stale JS/CSS after a redeploy."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Open one shared httpx client for the whole app lifetime, close on
    shutdown. Reused across every request — saves the ~50-100ms TLS handshake
    that per-request clients would pay each time. Stored on `app.state.httpx_client`
    so `routes._jira()` can hand it to each JiraClient instance."""
    settings = get_settings()
    app.state.httpx_client = make_httpx_client(settings)
    try:
        yield
    finally:
        await app.state.httpx_client.aclose()
        app.state.httpx_client = None


def create_app() -> FastAPI:
    app = FastAPI(title="HotFix Booking", version="0.1.0", lifespan=_lifespan)

    settings = get_settings()
    if not settings.session_secret_key:
        raise RuntimeError(
            "SESSION_SECRET_KEY is not set. Generate one with "
            '`python -c "import secrets; print(secrets.token_hex(32))"` '
            "and add it to your .env."
        )
    # Session cookie signed with SESSION_SECRET_KEY. Rotating the secret
    # invalidates every session (everyone re-logs in). `Secure` flag is only
    # sent when APP_BASE_URL is HTTPS — dev over http://localhost:3001 needs
    # https_only=False or the cookie won't be sent.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_key,
        session_cookie="hb_session",
        max_age=settings.session_max_age_days * 24 * 60 * 60,
        same_site="lax",
        https_only=settings.app_base_url.lower().startswith("https://"),
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(router)

    if _STATIC_DIR.is_dir():
        app.mount("/", NoCacheStaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


app = create_app()
