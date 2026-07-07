"""FastAPI app entry point."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

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


def create_app() -> FastAPI:
    app = FastAPI(title="HotFix Booking", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(router)

    if _STATIC_DIR.is_dir():
        app.mount("/", NoCacheStaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


app = create_app()
