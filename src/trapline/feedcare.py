"""Zdroje feedů, které už nikomu neslouží (ADR-0011).

Feed přežije past, kvůli které vznikl. Zůstane zapnutý a v každé obchůzce
sype do katalogu produkty, o které nikdo nestojí — po smazané pasti
„Příbory" jich tak přibylo přes tisíc.

**Slouží** feed, který naimportoval aspoň jednu nabídku produktu, jejž
zná aspoň jedna aktivní past (i pod prahem hlídání — past ho zná, jen ho
neobchází). Nabídky se k feedu vážou přes ``Offer.shop == FeedSource.name``,
jak je zapisuje ``discovery``.

Úklid je **dvoustupňový**, a to schválně:

1. Feed přestane sloužit → **vypne se** a dostane poznámku s datem. Tím
   hned přestane sypat sirotky, ale URL a filtr zůstanou — když past
   obnovíš nebo založíš podobnou, stačí feed zapnout zpátky.
2. Zůstane-li vypnutý a k ničemu ``TRAPLINE_FEED_PURGE_DAYS`` dní
   (výchozí 14, 0 = nikdy), **smaže se**.

Dvě věci se nikdy nesahají:

* feed, který **ještě neběžel** (``last_run`` je prázdný) — nemá co dokázat;
* feed, který nemá **žádnou** nabídku — buď zatím nic nedodal, nebo
  dočasně selhává (výpadek, změna formátu). Takový stejně nic nesype,
  takže není co řešit, a vypnout ho kvůli výpadku sítě by byla chyba.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from . import catalog
from .config import settings
from .models import FeedSource, Offer, Source

log = logging.getLogger("trapline.feedcare")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _offer_counts(session: Session) -> dict[str, int]:
    """{název feedu: kolik má aktivních nabídek}."""
    rows = session.execute(
        select(Offer.shop, func.count())
        .where(Offer.source == Source.HEUREKA_FEED, Offer.active)
        .group_by(Offer.shop)
    ).all()
    return {shop: count for shop, count in rows}


def _serving_shops(session: Session) -> set[str]:
    """Názvy feedů, jejichž nabídky patří produktu známému aktivní pasti."""
    known = catalog.known_product_ids(session)
    if not known:
        return set()
    rows = session.execute(
        select(Offer.shop)
        .where(
            Offer.source == Source.HEUREKA_FEED,
            Offer.active,
            Offer.product_id.in_(known),
        )
        .distinct()
    ).all()
    return {shop for (shop,) in rows}


def review(session: Session) -> dict:
    """Vypni feedy, které nikomu neslouží, a smaž ty dlouho vypnuté.

    Vrací ``{"disabled": [...], "deleted": [...], "serving": n}``.
    """
    sources = session.scalars(select(FeedSource).order_by(FeedSource.id)).all()
    counts = _offer_counts(session)
    serving = _serving_shops(session)
    now = _now()
    grace = timedelta(days=settings.feed_purge_days)

    disabled: list[str] = []
    deleted: list[str] = []
    serving_n = 0

    for source in sources:
        if source.last_run is None:
            continue                      # ještě neběžel — nemá co dokázat
        if not counts.get(source.name):
            continue                      # nic nedodal / dočasně selhává
        if source.name in serving:
            serving_n += 1
            source.useless_since = None
            continue

        if source.useless_since is None:
            source.useless_since = now
        if source.enabled:
            source.enabled = False
            source.last_status = (
                f"vypnuto automaticky {now:%-d. %-m. %Y} — žádný z jeho produktů "
                "nezajímá aktivní past"
            )
            disabled.append(source.name)
            log.info("feedy: %s nikomu neslouží, vypínám", source.name)
        elif (
            settings.feed_purge_days > 0
            and now - source.useless_since >= grace
        ):
            deleted.append(source.name)
            session.execute(
                delete(FeedSource).where(FeedSource.id == source.id)
            )
            log.info(
                "feedy: %s je %d dní k ničemu, mažu",
                source.name, settings.feed_purge_days,
            )

    session.commit()
    if disabled or deleted:
        log.info(
            "feedy: vypnuto %d, smazáno %d, slouží %d",
            len(disabled), len(deleted), serving_n,
        )
    return {"disabled": disabled, "deleted": deleted, "serving": serving_n}


def overview(session: Session) -> dict:
    """Čísla pro Administraci — kolik feedů slouží a kolik čeká na smazání."""
    sources = session.scalars(select(FeedSource)).all()
    counts = _offer_counts(session)
    serving = _serving_shops(session)
    useless = [
        s for s in sources
        if s.last_run is not None
        and counts.get(s.name)
        and s.name not in serving
    ]
    return {
        "sources": len(sources),
        "serving": sum(1 for s in sources if s.name in serving),
        "useless": len(useless),
        "waiting_for_delete": sum(1 for s in useless if s.useless_since),
        "purge_days": settings.feed_purge_days,
    }


def has_offers(session: Session, source: FeedSource) -> bool:
    """Dodal ten feed vůbec někdy něco? (pro GUI)"""
    return bool(session.scalar(
        select(exists().where(
            Offer.source == Source.HEUREKA_FEED,
            Offer.shop == source.name,
            Offer.active,
        ))
    ))
