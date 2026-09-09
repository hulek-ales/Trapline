"""Administrace: nastavení LLM z GUI (ADR-0010).

Všechno tady je za heslem jako zbytek ``/api``. Klíč proxy se přijímá,
ale nikdy nevrací — GUI vidí jen, jestli je vyplněný.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import settings_store
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
