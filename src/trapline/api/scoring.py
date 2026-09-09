"""Spouštění LLM skóringu a diagnostika Ollamy."""

from __future__ import annotations

import contextlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import llm, scoring
from ..config import settings
from ..models import Criteria, Product
from .criteria import get_db

router = APIRouter(prefix="/api/scoring", tags=["scoring"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("/ollama")
def ollama_status():
    """Dosažitelnost Ollamy z kontejneru — diagnostika sítě bez hádání."""
    out = {
        "url": settings.ollama_url,
        "model": settings.llm_main,
        "proxy": settings.llm_proxy_enabled,
    }
    if not settings.ollama_url:
        return {**out, "reachable": False, "error": "OLLAMA_URL není nastavené."}
    try:
        models = llm.available_models()
    except Exception as exc:  # noqa: BLE001
        return {**out, "reachable": False, "error": str(exc)}
    running: list[dict] = []
    # starší Ollama /api/ps nemusí mít — diagnostika bez něj pořád funguje
    with contextlib.suppress(Exception):
        running = llm.running_models()
    result = {
        **out,
        "reachable": True,
        "model_available": settings.llm_main in models,
        "models": models,
        # co teď reálně sedí v paměti: vram_pct < 100 = část modelu na CPU
        # = řádové zpomalení generování
        "running": running,
    }
    if settings.llm_proxy_enabled:
        # plánovač: kdo drží GPU a jak dlouhá je fronta (ADR-0009)
        result["proxy_status"] = llm.proxy_status()
    return result


@router.post("/run", status_code=202)
def run(session: DbSession):
    traps = session.scalar(
        select(func.count()).where(Criteria.active)
    ) or 0
    if traps == 0:
        raise HTTPException(400, "Žádná aktivní past.")
    products = session.scalar(select(func.count()).select_from(Product)) or 0
    if products == 0:
        raise HTTPException(400, "Katalog je prázdný — nejdřív spusť discovery.")
    if not settings.ollama_url:
        raise HTTPException(400, "OLLAMA_URL není nastavené.")
    if not scoring.start():
        raise HTTPException(409, "Skóring už běží.")
    return {"message": f"Spuštěno: {traps} pastí × {products} produktů."}


@router.get("/status")
def run_status():
    return scoring.status()
