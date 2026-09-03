"""CRUD pro kritéria — „pasti", které agent obchází.

První doménový router nad databází. Hard/soft constraints jsou volný JSON
(viz models.Criteria), takže se schéma požadavků dá měnit bez migrace.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import db, scoring
from ..config import settings
from ..models import Criteria, CriteriaMatch, Listing, ListingMatch, Product

router = APIRouter(prefix="/api/criteria", tags=["criteria"])


def get_db():
    if not db.ensure_ready():
        raise HTTPException(503, "Databáze není dostupná.")
    session = db.open_session()
    try:
        yield session
    finally:
        session.close()


class CriteriaIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    query_terms: list[str] = []
    prefilter: str = Field(default="", max_length=500)
    hard: dict = {}
    soft: dict = {}
    budget_max: float | None = Field(default=None, gt=0)
    active: bool = True


class CriteriaPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    query_terms: list[str] | None = None
    prefilter: str | None = Field(default=None, max_length=500)
    hard: dict | None = None
    soft: dict | None = None
    budget_max: float | None = Field(default=None, gt=0)
    active: bool | None = None


class CriteriaOut(CriteriaIn):
    model_config = {"from_attributes": True}

    id: int
    created_at: datetime | None = None


DbSession = Annotated[Session, Depends(get_db)]


def _get_or_404(session: Session, criteria_id: int) -> Criteria:
    row = session.get(Criteria, criteria_id)
    if row is None:
        raise HTTPException(404, "Kritérium neexistuje.")
    return row


@router.get("", response_model=list[CriteriaOut])
def list_criteria(session: DbSession):
    rows = session.scalars(select(Criteria).order_by(Criteria.id)).all()
    return rows


@router.post("", response_model=CriteriaOut, status_code=201)
def create_criteria(payload: CriteriaIn, session: DbSession):
    row = Criteria(**payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.patch("/{criteria_id}", response_model=CriteriaOut)
def update_criteria(criteria_id: int, payload: CriteriaPatch, session: DbSession):
    row = _get_or_404(session, criteria_id)
    data = payload.model_dump(exclude_unset=True)

    #: Změna zadání dělá stará skóre bezcennými — vyhází se hned, ne až je
    #: přepíše další běh. Jinak by stránka pasti ukazovala výsledky proti
    #: zadání, které už neplatí.
    zadani_changed = any(
        key in data and data[key] != getattr(row, key)
        for key in ("query_terms", "hard", "soft")
    )
    prefilter_changed = "prefilter" in data and data["prefilter"] != row.prefilter

    for key, value in data.items():
        setattr(row, key, value)

    if zadani_changed:
        session.execute(
            delete(CriteriaMatch).where(CriteriaMatch.criteria_id == criteria_id)
        )
    elif prefilter_changed:
        # jen zúžení záběru: vyhoď výsledky produktů, které novým
        # předfiltrem neprojdou; zbylá skóre platí dál
        keep = {
            p.id for p in session.scalars(select(Product))
            if scoring.passes_prefilter(row, p)
        }
        session.execute(
            delete(CriteriaMatch).where(
                CriteriaMatch.criteria_id == criteria_id,
                CriteriaMatch.product_id.not_in(keep),
            )
        )

    session.commit()
    session.refresh(row)

    # po změně zadání rovnou přeskórovat, ať uživatel nečeká na ruční klik
    if (zadani_changed or prefilter_changed) and row.active and settings.ollama_url:
        scoring.start()  # False = už běží, to nevadí
    return row


@router.get("/{criteria_id}/listings")
def trap_listings(criteria_id: int, session: DbSession):
    """Bazarové inzeráty vyhodnocené pro past, nejnovější první."""
    _get_or_404(session, criteria_id)
    rows = session.execute(
        select(ListingMatch, Listing)
        .join(Listing, Listing.id == ListingMatch.listing_id)
        .where(ListingMatch.criteria_id == criteria_id)
        .order_by(Listing.first_seen.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": listing.id,
            "title": listing.title,
            "url": listing.url,
            "price": listing.price or None,
            "locality": listing.locality,
            "bazar": listing.source.value,
            "score": round(match.confidence * 100),
            "relevant": match.confidence >= 0.6,
            "condition": match.condition.value,
            "red_flags": match.red_flags or [],
            "first_seen": listing.first_seen.isoformat()
            if listing.first_seen else None,
            "gone": listing.gone_at is not None,
        }
        for match, listing in rows
    ]


@router.delete("/{criteria_id}", status_code=204)
def delete_criteria(criteria_id: int, session: DbSession):
    row = _get_or_404(session, criteria_id)
    # výsledky skóringu i bazarové vyhodnocení drží FK na past — bez tohohle
    # mazání spadne na MariaDB (SQLite FK v testech vynucuje pragma fixture)
    session.execute(
        delete(CriteriaMatch).where(CriteriaMatch.criteria_id == criteria_id)
    )
    session.execute(
        delete(ListingMatch).where(ListingMatch.criteria_id == criteria_id)
    )
    session.delete(row)
    session.commit()
