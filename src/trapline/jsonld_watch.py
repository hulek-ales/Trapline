"""Hlídání ceny na konkrétní produktové stránce eshopu (source=jsonld).

Uživatel v GUI připne produktu URL detailu v libovolném obchodě (Alza,
Datart, …). Transportní vrstva (ADR-0006) stránku stáhne — přímým HTTP, a
když obchod blokuje, skutečným Chromem přes browserless — a JSON-LD extraktor
z ní přečte cenu a dostupnost. Obchůzka pak cenu obnovuje jako u Zboží.cz.

Tiché selhání (stránka jde stáhnout, ale nenese Product s cenou) se hlásí
nahoru a HTML se uloží do data/failures/ (ADR-0007) — nikdy se nezapisuje
prázdno do DB.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db, transport
from .config import settings
from .crawlers import jsonld
from .models import Offer, PriceHistory, Product, Source

log = logging.getLogger("trapline.jsonld_watch")


class ExtractError(RuntimeError):
    """Stránka se stáhla, ale nenese JSON-LD Product s cenou."""


def snapshot_price(url: str) -> tuple[jsonld.JsonLdProduct, str]:
    """Stáhne stránku a vrátí (produkt s cenou, transport). Vyhazuje
    TransportError/ExtractError — volající rozhodne, jak hlasitě."""
    page = transport.fetch(url)
    product = jsonld.best(page.text)
    if product is None or product.price is None:
        saved = transport.save_failure(transport.domain_of(url), page.text)
        raise ExtractError(
            "stránka nenese JSON-LD Product s cenou"
            + (f" (HTML uloženo: {saved})" if saved else "")
        )
    return product, page.via


def _sku_for(url: str) -> str:
    """Unikátní klíč nabídky v rámci (source, shop). Hash URL místo SKU ze
    stránky — dva eshopy (i dvě stránky) klidně sdílí výrobcovo SKU a unique
    constraint by pak zablokoval připnutí."""
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def attach(session: Session, product: Product, url: str) -> dict:
    """Připne/obnoví hlídání stránky a hned zapíše první cenu."""
    found, via = snapshot_price(url)
    shop = transport.domain_of(url)
    offer = session.scalars(
        select(Offer).where(
            Offer.product_id == product.id,
            Offer.source == Source.JSONLD,
            Offer.url == url,
        )
    ).first()
    if offer is None:
        offer = Offer(
            product_id=product.id, source=Source.JSONLD,
            shop=shop, sku=_sku_for(url),
        )
        session.add(offer)
    offer.url = url
    offer.active = True
    offer.last_checked = datetime.now(UTC)
    session.flush()
    session.add(PriceHistory(
        offer_id=offer.id, price=found.price,
        in_stock=found.in_stock if found.in_stock is not None else True,
    ))
    if not product.ean and found.ean:
        # EAN je unikátní — když ho už nese jiný produkt (uživatel připnul
        # stejnou stránku dvěma variantám), nech ho být.
        taken = session.scalars(
            select(Product).where(Product.ean == found.ean, Product.id != product.id)
        ).first()
        if taken is None:
            product.ean = found.ean
    return {
        "shop": shop, "name": found.name, "price": found.price,
        "currency": found.currency or "CZK",
        "in_stock": found.in_stock, "via": via,
    }


def refresh_offer(session: Session, offer: Offer) -> jsonld.JsonLdProduct:
    found, _via = snapshot_price(offer.url)
    session.add(PriceHistory(
        offer_id=offer.id, price=found.price,
        in_stock=found.in_stock if found.in_stock is not None else True,
    ))
    offer.last_checked = datetime.now(UTC)
    return found


def refresh_all() -> int:
    """Obnov všechny připnuté stránky. Vrací počet úspěšných."""
    if not db.ensure_ready():
        log.warning("jsonld: databáze nedostupná")
        return 0
    done = 0
    with db.open_session() as session:
        offers = session.scalars(
            select(Offer).where(Offer.source == Source.JSONLD, Offer.active)
        ).all()
        if not offers:
            return 0
        log.info("jsonld: obnovuji %d stránek", len(offers))
        for i, offer in enumerate(offers):
            if i:
                time.sleep(settings.request_delay_s)
            try:
                found = refresh_offer(session, offer)
                session.commit()
                done += 1
                log.info(
                    "jsonld: %s @ %s — %.0f Kč%s",
                    found.name, offer.shop, found.price,
                    "" if found.in_stock in (True, None) else " (není skladem)",
                )
            except Exception as exc:  # noqa: BLE001 — jedna stránka nesmí shodit běh
                session.rollback()
                log.warning("jsonld: %s selhalo: %s", offer.url, exc)
    return done
