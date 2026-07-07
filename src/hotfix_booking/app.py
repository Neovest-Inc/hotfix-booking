"""FastAPI app entry point."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .routes import router

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="HotFix Booking", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(router)

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


app = create_app()
