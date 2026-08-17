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

from .. import db
from ..models import Criteria, CriteriaMatch

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
    hard: dict = {}
    soft: dict = {}
    budget_max: float | None = Field(default=None, gt=0)
    active: bool = True


class CriteriaPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    query_terms: list[str] | None = None
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
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/{criteria_id}", status_code=204)
def delete_criteria(criteria_id: int, session: DbSession):
    row = _get_or_404(session, criteria_id)
    # výsledky skóringu drží FK na past — bez tohohle mazání spadne na
    # MariaDB (SQLite FK v testech nevynucuje, proto fixture zapíná pragma)
    session.execute(
        delete(CriteriaMatch).where(CriteriaMatch.criteria_id == criteria_id)
    )
    session.delete(row)
    session.commit()
