"""Spouštění LLM skóringu a diagnostika Ollamy."""

from __future__ import annotations

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
    out = {"url": settings.ollama_url, "model": settings.llm_main}
    if not settings.ollama_url:
        return {**out, "reachable": False, "error": "OLLAMA_URL není nastavené."}
    try:
        models = llm.available_models()
    except Exception as exc:  # noqa: BLE001
        return {**out, "reachable": False, "error": str(exc)}
    running = []
    try:
        running = llm.running_models()
    except Exception:  # noqa: BLE001 — starší Ollama /api/ps nemusí mít
        pass
    return {
        **out,
        "reachable": True,
        "model_available": settings.llm_main in models,
        "models": models,
        # co teď reálně sedí v paměti: vram_pct < 100 = část modelu na CPU
        # = řádové zpomalení generování
        "running": running,
    }


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
