"""Správa zdrojů feedů a spouštění discovery (ADR-0003)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import discovery, feedhunt, grouping
from ..config import settings
from ..models import (
    Criteria,
    CriteriaMatch,
    FeedSource,
    PriceHistory,
    Product,
    UserFeedback,
)
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


# --- automatické hledání zdrojů (feedhunt) ---------------------------------

@router.post("/hunt/{criteria_id}", status_code=202)
def hunt(criteria_id: int, session: DbSession):
    """Najdi nové zdroje pro past: SearXNG → domény → oťukání feedů.
    Nálezy se zakládají jako vypnuté návrhy v sekci Zdroje."""
    if not settings.searxng_url:
        raise HTTPException(400, "SEARXNG_URL není nastavené.")
    trap = session.get(Criteria, criteria_id)
    if trap is None:
        raise HTTPException(404, "Past neexistuje.")
    if not feedhunt.start(criteria_id):
        raise HTTPException(409, "Hledání zdrojů už běží.")
    return {"message": f"Hledám obchody pro past „{trap.name}“…"}


@router.get("/hunt/status")
def hunt_status():
    return feedhunt.status()


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
    image: str | None = None
    #: Produkt má připnuté hlídání na Zboží.cz.
    has_zbozi: bool = False
    #: Ruční verdikt uživatele (like/dislike/owned) — přebíjí skóre.
    verdict: str | None = None
    matches: list[MatchOut] = []
    #: Barevné varianty sloučené do jednoho řádku (názvy). 1 = bez variant.
    variant_count: int = 1
    variant_titles: list[str] = []


@router.get("/products", response_model=list[ProductOut])
def list_products(session: DbSession, limit: int = 200):
    """Katalog nalezených produktů s poslední cenou z každé nabídky."""
    limit = max(1, min(limit, 1000))
    products = session.scalars(
        select(Product).order_by(Product.id.desc()).limit(limit)
    ).all()
    trap_names = dict(session.execute(select(Criteria.id, Criteria.name)).all())
    verdicts = {
        fb.product_id: fb.verdict.value
        for fb in session.scalars(select(UserFeedback))
        if fb.verdict.value != "neutral"
    }
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
        specs = {
            k: v for k, v in (product.specs or {}).items() if not k.startswith("_")
        }
        prices: list[float] = []
        urls: list[str] = []
        has_zbozi = False
        for offer in product.offers:
            if offer.source.value == "zbozi" and offer.active:
                has_zbozi = True
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
                specs=specs,
                offers=len(urls),
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                urls=urls,
                image=(product.specs or {}).get("_img"),
                has_zbozi=has_zbozi,
                verdict=verdicts.get(product.id),
                matches=matches_by_product.get(product.id, []),
            )
        )
    return _group_families(out, products)


def _group_families(
    rows: list[ProductOut], products: list[Product]
) -> list[ProductOut]:
    """Sluč barevné varianty téhož modelu do jednoho řádku (grouping.py)."""
    by_id = {p.id: p for p in products}
    families: dict[str, list[ProductOut]] = {}
    order: list[str] = []
    for row in rows:
        key = grouping.family_key(row.brand, by_id[row.id].title)
        if key not in families:
            families[key] = []
            order.append(key)
        families[key].append(row)

    out: list[ProductOut] = []
    for key in order:
        members = families[key]
        if len(members) == 1:
            out.append(members[0])
            continue
        head = members[0]
        prices = [
            p for m in members for p in (m.price_min, m.price_max) if p is not None
        ]
        # skóre: nejlepší match na past napříč variantami
        best: dict[int, MatchOut] = {}
        for m in members:
            for match in m.matches:
                cur = best.get(match.criteria_id)
                if cur is None or match.score > cur.score:
                    best[match.criteria_id] = match
        out.append(
            ProductOut(
                id=head.id,
                ean=None,
                brand=head.brand,
                title=grouping.family_title([m.title for m in members]),
                specs=head.specs,
                offers=sum(m.offers for m in members),
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                urls=[u for m in members for u in m.urls],
                image=next((m.image for m in members if m.image), None),
                has_zbozi=any(m.has_zbozi for m in members),
                verdict=next((m.verdict for m in members if m.verdict), None),
                matches=sorted(best.values(), key=lambda x: x.criteria_id),
                variant_count=len(members),
                variant_titles=[m.title for m in members],
            )
        )
    return out
