"""Administrace: nastavení LLM a údržba katalogu (ADR-0010).

Všechno tady je za heslem jako zbytek ``/api``. Klíč proxy se přijímá,
ale nikdy nevrací — GUI vidí jen, jestli je vyplněný.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import catalog, feedcare, settings_store
from .criteria import get_db
from .scoring import ollama_status

router = APIRouter(prefix="/api/admin", tags=["admin"])

DbSession = Annotated[Session, Depends(get_db)]


class LlmSettingsIn(BaseModel):
    """``None`` = beze změny. U klíče prázdný řetězec = smazat."""

    ollama_url: str | None = None
    ollama_key: str | None = None
    llm_main: str | None = None
    llm_bulk: str | None = None
    llm_load_wait_s: float | None = Field(None, ge=0, le=86400)
    #: Od jakého skóre obchůzka hlídá cenu nerelevantního nálezu.
    watch_min_score: float | None = Field(None, ge=0, le=100)
    #: Po kolika dnech smazat feed, který nikomu neslouží (0 = nikdy).
    feed_purge_days: int | None = Field(None, ge=0, le=365)


@router.get("/settings")
def get_settings():
    return {"llm": settings_store.snapshot()}


@router.put("/settings")
def put_settings(body: LlmSettingsIn, session: DbSession):
    settings_store.save(session, body.model_dump())
    return {"llm": settings_store.snapshot()}


@router.post("/settings/reset")
def reset_settings(session: DbSession):
    """Zahoď hodnoty z GUI, vrať se k proměnným prostředí."""
    settings_store.reset(session)
    return {"llm": settings_store.snapshot()}


@router.post("/settings/test")
def test_settings():
    """Ověř aktuální nastavení naostro — dosažitelnost, modely, proxy."""
    return ollama_status()


@router.get("/catalog")
def catalog_overview(session: DbSession):
    """Co katalog obsahuje a co z toho obchůzka reálně obchází."""
    return catalog.overview(session)


@router.post("/catalog/purge")
def catalog_purge(session: DbSession):
    """Zahoď produkty, které nezná žádná aktivní past — i s cenami.
    Nevratné; GUI se ptá předem."""
    return catalog.purge_orphans(session)


@router.get("/feeds")
def feeds_overview(session: DbSession):
    """Kolik zdrojů feedů ještě někomu slouží."""
    return feedcare.overview(session)


@router.post("/feeds/review")
def feeds_review(session: DbSession):
    """Projdi zdroje hned, bez čekání na obchůzku."""
    return feedcare.review(session)
