"""Ruční verdikt k produktu — přebíjí automatické skóre.

Model se občas splete (uvěří marketingovému popisu); like/dislike od
uživatele je konečné slovo. Watcher později hlídá ceny podle kombinace:
dislike vyřazuje vždy, like zařazuje vždy, jinak rozhoduje skóre.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, UserFeedback, Verdict
from .criteria import get_db

router = APIRouter(prefix="/api/products", tags=["feedback"])

DbSession = Annotated[Session, Depends(get_db)]


class VerdictIn(BaseModel):
    verdict: Verdict


@router.put("/{product_id}/verdict")
def set_verdict(product_id: int, payload: VerdictIn, session: DbSession):
    if session.get(Product, product_id) is None:
        raise HTTPException(404, "Produkt neexistuje.")
    row = session.scalars(
        select(UserFeedback).where(UserFeedback.product_id == product_id)
    ).first()
    if row is None:
        row = UserFeedback(product_id=product_id)
        session.add(row)
    row.verdict = payload.verdict
    session.commit()
    return {"product_id": product_id, "verdict": payload.verdict}
