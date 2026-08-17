"""FastAPI aplikace — zatím jen skořápka GUI a self-update z Gitu.

Vlastní domény (kritéria, produkty, inzeráty) se doplní, až budou hotové
crawlery. Aby šlo appku nasadit a aktualizovat z webu už teď, běží server
bez databáze — ``/api/system/*`` na ní nezávisí.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import system

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Trapline", version="0.1.0")

app.include_router(system.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
