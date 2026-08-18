"""Obnova cen ze Zboží.cz pro produkty s připnutou nabídkou (source=zbozi).

Součást obchůzky: každé zbozi nabídce se stáhne detail (robots-povolená
stránka), zapíše se aktuální minimální cena napříč obchody do append-only
historie a doplní se released produktu, pokud chybí (odpisová křivka).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db
from .config import settings
from .crawlers import zbozi
from .models import Offer, PriceHistory, Product, Source

log = logging.getLogger("trapline.zbozi_watch")


def refresh_offer(session: Session, offer: Offer) -> zbozi.ZboziDetail:
    detail = zbozi.fetch_detail(offer.url)
    session.add(PriceHistory(offer_id=offer.id, price=detail.min_price))
    offer.active = True
    offer.last_checked = datetime.now(UTC)
    product = session.get(Product, offer.product_id)
    if product is not None and product.released is None and detail.released:
        product.released = detail.released.date()
    return detail


def refresh_all() -> int:
    """Obnov všechny zbozi nabídky. Vrací počet úspěšných."""
    if not db.ensure_ready():
        log.warning("zbozi: databáze nedostupná")
        return 0
    done = 0
    with db.open_session() as session:
        offers = session.scalars(
            select(Offer).where(Offer.source == Source.ZBOZI, Offer.active)
        ).all()
        if not offers:
            return 0
        log.info("zbozi: obnovuji %d produktů", len(offers))
        for i, offer in enumerate(offers):
            if i:
                time.sleep(settings.request_delay_s)
            try:
                detail = refresh_offer(session, offer)
                session.commit()
                done += 1
                log.info(
                    "zbozi: %s — %.0f Kč (%d obchodů, nejlevnější %s)",
                    detail.name, detail.min_price, detail.shop_count,
                    detail.cheapest_shop or "?",
                )
            except Exception as exc:  # noqa: BLE001 — jeden produkt nesmí shodit běh
                session.rollback()
                log.warning("zbozi: %s selhalo: %s", offer.url, exc)
    return done
