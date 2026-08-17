"""FastAPI aplikace — zatím jen skořápka GUI a self-update z Gitu.

Vlastní domény (kritéria, produkty, inzeráty) se doplní, až budou hotové
crawlery. Aby šlo appku nasadit a aktualizovat z webu už teď, běží server
bez databáze — ``/api/system/*`` na ní nezávisí.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import db, logbuffer
from ..config import settings
from . import auth, criteria, discovery, scoring, system

log = logging.getLogger("trapline.api")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Co projde i bez přihlášení. Zbytek /api/ je za heslem — včetně
#: /api/system/*, kde update umí restartovat proces.
_OPEN_PREFIX = "/api/auth/"
_OPEN_EXACT = frozenset({"/api/health"})


def _warn_o_zabezpeceni() -> None:
    if settings.auth_enabled:
        return
    log.warning("APP_PASSWORD není nastavené – GUI i API jsou otevřené komukoli.")
    if settings.update_enabled:
        # Nejhorší kombinace: /api/system/update umí spustit git pull
        # a restartovat proces, a je bez hesla.
        log.warning(
            "UPDATE_ENABLED=true BEZ hesla – kdokoli, kdo na appku dosáhne, "
            "může spustit aktualizaci z Gitu a restart. Nastav APP_PASSWORD."
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    logbuffer.install()
    _warn_o_zabezpeceni()
    # Nezdar nevadí — start na DB nečeká, doménové endpointy vrací 503
    # a při dalším requestu se o inicializaci pokusí znovu.
    db.ensure_ready()
    yield


app = FastAPI(title="Trapline", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(system.router)
app.include_router(criteria.router)
app.include_router(discovery.router)
app.include_router(scoring.router)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    open_path = path.startswith(_OPEN_PREFIX) or path in _OPEN_EXACT
    protected = path.startswith("/api/") and not open_path
    if protected and not auth.is_authenticated(request):
        return JSONResponse({"detail": "Neautorizováno"}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
def health():
    # db je poslední známý stav, ne živý dotaz — health nesmí čekat na timeout.
    return {"status": "ok", "db": db.is_ready()}


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # Statická skořápka je veřejná; data si tahá až po přihlášení.
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
