"""Co z katalogu ještě někdo hlídá — a co je možné zahodit.

Produkty se do katalogu dostanou z feedů a z crawleru pastí (ADR-0007)
a už tam zůstanou — i když past, kvůli které se našly, zanikne. Obnova cen
proto potřebuje vědět, na co ještě sahat: každá hlídaná stránka je jeden
HTTP požadavek v každé obchůzce, u obchodů za Cloudflarem i spuštění
Chromu (ADR-0006). Bez filtru obchůzka donekonečna obchází stránky,
o které nikdo nestojí.

**Hlídá se** produkt, který má výsledek skóringu (``CriteriaMatch``)
k aspoň jedné **aktivní** pasti, a ten výsledek je buď relevantní, nebo
má skóre aspoň ``TRAPLINE_WATCH_MIN_SCORE`` (nastavitelné v Administraci,
ADR-0010). Práh je kompromis: skóre se s cenou nemění, takže hlídat kus
oskórovaný na 5 bodů je čirá ztráta času — ale širší vzorek cen zpřesňuje
odhad tržní ceny v ``references``, takže se neškrtá jen na relevantní.

Rozhodnutí je samoopravné: skóring běží v obchůzce před obnovou cen,
takže čerstvý nález dostane match dřív, než na něj dojde řada; smazaná
past si své ``CriteriaMatch`` bere s sebou; a když past znovu založíš,
skóring produkty potká a hlídání se samo obnoví.

**Sirotek** je produkt, který nemá match k žádné aktivní pasti — ten se
nehlídá vůbec a v Administraci jde zahodit (``purge``). Sirotci nejsou
totéž co „pod prahem": produkt pod prahem past pořád zná, jen ho nestojí
za to obcházet, takže zůstává v katalogu i po úklidu.
"""

from __future__ import annotations

import logging

from sqlalchemy import Integer, delete, func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Alert,
    Criteria,
    CriteriaMatch,
    Offer,
    PriceHistory,
    PriceReference,
    Product,
    UserFeedback,
)

log = logging.getLogger("trapline.catalog")


def _scored_ids(session: Session, *, only_watchable: bool) -> set[int]:
    query = (
        select(CriteriaMatch.product_id)
        .join(Criteria, Criteria.id == CriteriaMatch.criteria_id)
        .where(Criteria.active)
    )
    if only_watchable:
        query = query.where(or_(
            CriteriaMatch.relevant.is_(True),
            CriteriaMatch.score >= settings.watch_min_score,
        ))
    return {product_id for (product_id,) in session.execute(query.distinct())}


def watched_product_ids(session: Session) -> set[int]:
    """Id produktů, jejichž cenu má smysl obcházet."""
    return _scored_ids(session, only_watchable=True)


def known_product_ids(session: Session) -> set[int]:
    """Id produktů, které aspoň jedna aktivní past zná (i pod prahem).
    Co v tomhle není, je sirotek."""
    return _scored_ids(session, only_watchable=False)


def watched_offers(session: Session, offers: list) -> tuple[list, int]:
    """(nabídky ke zpracování, kolik jich nemá smysl obcházet)."""
    wanted = watched_product_ids(session)
    keep = [o for o in offers if o.product_id in wanted]
    return keep, len(offers) - len(keep)


def orphan_ids(session: Session) -> set[int]:
    """Produkty, které nezná žádná aktivní past."""
    all_ids = {pid for (pid,) in session.execute(select(Product.id))}
    return all_ids - known_product_ids(session)


def overview(session: Session) -> dict:
    """Čísla pro Administraci — co katalog obsahuje a co z toho žije."""
    total = session.scalar(select(func.count()).select_from(Product)) or 0
    watched = watched_product_ids(session)
    known = known_product_ids(session)
    offers = session.scalar(
        select(func.count()).select_from(Offer).where(Offer.active)
    ) or 0
    watched_offers_n = session.scalar(
        select(func.count()).select_from(Offer)
        .where(Offer.active, Offer.product_id.in_(watched or {0}))
    ) or 0
    return {
        "products": total,
        "watched": len(watched),
        # zná past, ale pod prahem — zůstává v katalogu, jen se neobchází
        "below_threshold": len(known) - len(watched),
        "orphans": total - len(known),
        "active_offers": offers,
        "watched_offers": watched_offers_n,
        "min_score": settings.watch_min_score,
    }


def purge_orphans(session: Session) -> dict:
    """Smaž produkty, které nezná žádná aktivní past, i s jejich daty.

    Nevratné. Vazby se mažou ručně a v pořadí od nejhlubší — MariaDB by
    jinak spadla na cizí klíč (SQLite v testech taky, s pragma fixture).
    """
    orphans = orphan_ids(session)
    if not orphans:
        return {"products": 0, "offers": 0, "prices": 0}

    offer_ids = {
        oid for (oid,) in session.execute(
            select(Offer.id).where(Offer.product_id.in_(orphans))
        )
    }
    prices = 0
    if offer_ids:
        prices = session.scalar(
            select(func.count()).select_from(PriceHistory)
            .where(PriceHistory.offer_id.in_(offer_ids))
        ) or 0
        # alerty ukazují na nabídku i produkt — obojí mizí
        session.execute(delete(Alert).where(Alert.offer_id.in_(offer_ids)))
        session.execute(
            delete(PriceHistory).where(PriceHistory.offer_id.in_(offer_ids))
        )
    session.execute(delete(Alert).where(Alert.product_id.in_(orphans)))
    session.execute(delete(UserFeedback).where(UserFeedback.product_id.in_(orphans)))
    session.execute(
        delete(PriceReference).where(PriceReference.product_id.in_(orphans))
    )
    # matche na vypnuté pasti (aktivní past by z produktu sirotka nedělala)
    session.execute(
        delete(CriteriaMatch).where(CriteriaMatch.product_id.in_(orphans))
    )
    session.execute(delete(Offer).where(Offer.product_id.in_(orphans)))
    session.execute(delete(Product).where(Product.id.in_(orphans)))
    session.commit()
    log.info(
        "katalog: uklizeno %d produktů, %d nabídek, %d cen",
        len(orphans), len(offer_ids), prices,
    )
    return {"products": len(orphans), "offers": len(offer_ids), "prices": prices}


def trap_stats(session: Session) -> dict[int, dict]:
    """{criteria_id: čísla} — kolik past oskórovala, kolik je relevantních
    a kolik stránek se kvůli ní reálně obchází."""
    rows = session.execute(
        select(
            CriteriaMatch.criteria_id,
            func.count().label("scored"),
            func.sum(func.cast(CriteriaMatch.relevant, Integer)).label("relevant"),
        ).group_by(CriteriaMatch.criteria_id)
    ).all()
    out = {
        cid: {"scored": scored, "relevant": int(relevant or 0), "watched_offers": 0}
        for cid, scored, relevant in rows
    }
    # hlídané nabídky per past: produkt musí projít prahem u té konkrétní pasti
    watched_rows = session.execute(
        select(CriteriaMatch.criteria_id, func.count(func.distinct(Offer.id)))
        .join(Offer, Offer.product_id == CriteriaMatch.product_id)
        .where(
            Offer.active,
            or_(
                CriteriaMatch.relevant.is_(True),
                CriteriaMatch.score >= settings.watch_min_score,
            ),
        )
        .group_by(CriteriaMatch.criteria_id)
    ).all()
    for cid, count in watched_rows:
        out.setdefault(
            cid, {"scored": 0, "relevant": 0, "watched_offers": 0}
        )["watched_offers"] = count
    return out
