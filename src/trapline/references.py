"""Přepočet retailových referenčních cen (ADR-0002, retail větev).

Pro každý produkt s aktivními nabídkami se z posledních cen spočítá
``retail_median`` (winsorizovaný medián) a ``retail_best`` (10. percentil =
reálně dosažitelná nová cena, ne UVP). Zapisuje se nový snapshot do
``price_reference`` — přepočet jede v obchůzce, ne při requestu.

Bazarová větev (used_*) se doplní s crawlery inzerátů.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PriceHistory, PriceReference, Product
from .pricing.reference import percentile, winsorize

log = logging.getLogger("trapline.references")


def latest_prices(session: Session, product: Product) -> list[float]:
    """Poslední cena z každé aktivní důvěryhodné nabídky produktu."""
    prices: list[float] = []
    for offer in product.offers:
        if not offer.active or not offer.trusted:
            continue
        last = session.scalars(
            select(PriceHistory)
            .where(PriceHistory.offer_id == offer.id)
            .order_by(PriceHistory.ts.desc())
            .limit(1)
        ).first()
        if last is not None:
            prices.append(last.price + last.shipping)
    return prices


def recompute_product(session: Session, product: Product) -> PriceReference | None:
    prices = latest_prices(session, product)
    if not prices:
        return None
    # winsorizace má smysl až od pár vzorků; do té doby syrová data
    cleaned = winsorize(prices) if len(prices) >= 5 else prices
    ref = PriceReference(
        product_id=product.id,
        retail_median=percentile(cleaned, 0.5),
        retail_best=percentile(cleaned, 0.10),
        retail_n=len(prices),
    )
    session.add(ref)
    return ref


def recompute_all(session: Session) -> int:
    """Snapshot referencí pro všechny produkty. Vrací počet zapsaných."""
    count = 0
    for product in session.scalars(select(Product)):
        if recompute_product(session, product) is not None:
            count += 1
    session.commit()
    log.info("reference přepočítány: %d produktů", count)
    return count
