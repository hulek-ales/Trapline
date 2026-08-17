"""Správa zdrojů feedů a spouštění discovery (ADR-0003)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import discovery
from ..models import Criteria, CriteriaMatch, FeedSource, PriceHistory, Product
from .criteria import get_db

router = APIRouter(prefix="/api/discovery", tags=["discovery"])

DbSession = Annotated[Session, Depends(get_db)]


# --- zdroje ----------------------------------------------------------------

class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: HttpUrl
    category_filter: str = Field(default="", max_length=500)
    enabled: bool = True


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: HttpUrl | None = None
    category_filter: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class SourceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    url: str
    category_filter: str
    enabled: bool
    last_run: datetime | None
    last_status: str | None


@router.get("/sources", response_model=list[SourceOut])
def list_sources(session: DbSession):
    return session.scalars(select(FeedSource).order_by(FeedSource.id)).all()


@router.post("/sources", response_model=SourceOut, status_code=201)
def create_source(payload: SourceIn, session: DbSession):
    data = payload.model_dump()
    data["url"] = str(data["url"])
    dup = session.scalars(
        select(FeedSource).where(FeedSource.url == data["url"])
    ).first()
    if dup:
        raise HTTPException(409, "Zdroj s tímhle URL už existuje.")
    row = FeedSource(**data)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.patch("/sources/{source_id}", response_model=SourceOut)
def update_source(source_id: int, payload: SourcePatch, session: DbSession):
    row = session.get(FeedSource, source_id)
    if row is None:
        raise HTTPException(404, "Zdroj neexistuje.")
    data = payload.model_dump(exclude_unset=True)
    if "url" in data:
        data["url"] = str(data["url"])
    for key, value in data.items():
        setattr(row, key, value)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: int, session: DbSession):
    row = session.get(FeedSource, source_id)
    if row is None:
        raise HTTPException(404, "Zdroj neexistuje.")
    session.delete(row)
    session.commit()


# --- běh -------------------------------------------------------------------

@router.post("/run", status_code=202)
def run(session: DbSession):
    """Spustí stažení všech povolených zdrojů na pozadí."""
    n = session.scalar(select(func.count()).where(FeedSource.enabled)) or 0
    if n == 0:
        raise HTTPException(400, "Žádný povolený zdroj feedu — nejdřív nějaký přidej.")
    if not discovery.start():
        raise HTTPException(409, "Discovery už běží.")
    return {"message": f"Spuštěno, {n} zdrojů."}


@router.get("/status")
def run_status():
    return discovery.status()


# --- produkty --------------------------------------------------------------

class MatchOut(BaseModel):
    criteria_id: int
    criteria_name: str
    score: float
    relevant: bool
    breakdown: list


class ProductOut(BaseModel):
    id: int
    ean: str | None
    brand: str
    title: str
    specs: dict
    offers: int
    price_min: float | None
    price_max: float | None
    urls: list[str]
    matches: list[MatchOut] = []


@router.get("/products", response_model=list[ProductOut])
def list_products(session: DbSession, limit: int = 200):
    """Katalog nalezených produktů s poslední cenou z každé nabídky."""
    limit = max(1, min(limit, 1000))
    products = session.scalars(
        select(Product).order_by(Product.id.desc()).limit(limit)
    ).all()
    trap_names = dict(session.execute(select(Criteria.id, Criteria.name)).all())
    matches_by_product: dict[int, list[MatchOut]] = {}
    for m in session.scalars(select(CriteriaMatch)):
        matches_by_product.setdefault(m.product_id, []).append(
            MatchOut(
                criteria_id=m.criteria_id,
                criteria_name=trap_names.get(m.criteria_id, "?"),
                score=m.score,
                relevant=m.relevant,
                breakdown=m.breakdown or [],
            )
        )
    out: list[ProductOut] = []
    for product in products:
        prices: list[float] = []
        urls: list[str] = []
        for offer in product.offers:
            if not offer.active:
                continue
            urls.append(offer.url)
            last = session.scalars(
                select(PriceHistory)
                .where(PriceHistory.offer_id == offer.id)
                .order_by(PriceHistory.ts.desc())
                .limit(1)
            ).first()
            if last:
                prices.append(last.price)
        out.append(
            ProductOut(
                id=product.id,
                ean=product.ean,
                brand=product.brand,
                title=product.title,
                specs=product.specs or {},
                offers=len(urls),
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                urls=urls,
                matches=matches_by_product.get(product.id, []),
            )
        )
    return out
