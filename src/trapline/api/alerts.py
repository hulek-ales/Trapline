"""Upozornění: výpis, ruční vyhodnocení, test notifikace."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import alerts as alerts_mod
from ..models import Alert
from .criteria import get_db

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

DbSession = Annotated[Session, Depends(get_db)]


class AlertOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    dedup_key: str
    score: float
    payload: dict
    sent_at: datetime | None


@router.get("", response_model=list[AlertOut])
def list_alerts(session: DbSession, limit: int = Query(20, le=200)):
    return session.scalars(
        select(Alert).order_by(Alert.id.desc()).limit(limit)
    ).all()


@router.post("/run")
def run_now(session: DbSession):
    """Ruční vyhodnocení alertů (jinak běží v obchůzce)."""
    sent = alerts_mod.evaluate_all(session)
    return {"sent": sent}


@router.post("/test")
def test_notification():
    """Ověř ntfy konfiguraci z kontejneru — pošle zkušební zprávu."""
    if not alerts_mod.ntfy_enabled():
        raise HTTPException(400, "NTFY_TOPIC není nastavený.")
    ok = alerts_mod.send_ntfy(
        "Trapline: test", "Notifikace fungují — past je nastražená."
    )
    if not ok:
        raise HTTPException(502, "ntfy nedoručilo — zkontroluj NTFY_URL/TOPIC v logu.")
    return {"ok": True}
