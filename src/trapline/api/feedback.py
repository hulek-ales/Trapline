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

from ..crawlers import zbozi
from ..models import Offer, PriceHistory, Product, Source, UserFeedback, Verdict
from .criteria import get_db

router = APIRouter(prefix="/api/products", tags=["feedback"])

DbSession = Annotated[Session, Depends(get_db)]


class VerdictIn(BaseModel):
    verdict: Verdict


class ZboziIn(BaseModel):
    url: str


@router.put("/{product_id}/zbozi")
def attach_zbozi(product_id: int, payload: ZboziIn, session: DbSession):
    """Připni produktu detail na Zboží.cz — obchůzka pak hlídá minimální
    cenu napříč všemi obchody včetně velkých řetězců."""
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "Produkt neexistuje.")
    url = payload.url.strip().rstrip("/") + "/"
    if not zbozi.PRODUCT_URL_RE.match(url):
        raise HTTPException(
            400, "Čekám URL ve tvaru https://www.zbozi.cz/vyrobek/…"
        )
    try:
        detail = zbozi.fetch_detail(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Stránku nejde přečíst: {exc}") from None

    offer = session.scalars(
        select(Offer).where(
            Offer.product_id == product_id, Offer.source == Source.ZBOZI
        )
    ).first()
    if offer is None:
        offer = Offer(
            product_id=product_id, source=Source.ZBOZI,
            shop="Zboží.cz", sku=detail.slug or url,
        )
        session.add(offer)
    offer.url = url
    offer.active = True
    session.flush()
    session.add(PriceHistory(offer_id=offer.id, price=detail.min_price))
    if product.released is None and detail.released:
        product.released = detail.released.date()
    session.commit()
    return {
        "name": detail.name,
        "min_price": detail.min_price,
        "median_price": detail.median_price,
        "shop_count": detail.shop_count,
        "cheapest_shop": detail.cheapest_shop,
    }


@router.delete("/{product_id}/zbozi", status_code=204)
def detach_zbozi(product_id: int, session: DbSession):
    offer = session.scalars(
        select(Offer).where(
            Offer.product_id == product_id, Offer.source == Source.ZBOZI
        )
    ).first()
    if offer is None:
        raise HTTPException(404, "Produkt zbozi nabídku nemá.")
    offer.active = False
    session.commit()


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
