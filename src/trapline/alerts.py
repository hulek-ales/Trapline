"""Vyhodnocení alertů a ntfy notifikace.

Fáze retail, pravidlo v1: relevantní produkt pasti (automaticky relevantní
nebo ručně 👍; ruční 👎 vyřazuje vždy) spadl nejlevnější nabídkou pod budget
pasti → jedna notifikace. Deduplikace přes ``alerts.dedup_key`` — bez ní by
každá obchůzka posílala totéž (přesně na to je tabulka, viz models.Alert).

Pravidla „pokles ceny proti baseline" přijdou s bazary — a s poučeními
z dodatku ADR-0002 (práh citelnosti, baseline = min z mediánů).
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Alert, Criteria, CriteriaMatch, Product, UserFeedback, Verdict
from .references import latest_prices

log = logging.getLogger("trapline.alerts")


def ntfy_enabled() -> bool:
    return bool(settings.ntfy_url and settings.ntfy_topic)


def send_ntfy(title: str, message: str, click_url: str | None = None) -> bool:
    """Pošli notifikaci. JSON publish — hlavičky ntfy neumí diakritiku."""
    if not ntfy_enabled():
        return False
    payload: dict = {
        "topic": settings.ntfy_topic,
        "title": title,
        "message": message,
        "tags": ["moneybag"],
    }
    if click_url:
        payload["click"] = click_url
    try:
        resp = httpx.post(
            settings.ntfy_url.rstrip("/"),
            json=payload,
            headers={"User-Agent": settings.user_agent},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("ntfy selhalo: %s", exc)
        return False


def _watched_product_ids(session: Session, trap: Criteria) -> set[int]:
    """Produkty, kterým se pro past hlídá cena: relevantní ze skóringu,
    ručně potvrzené navíc, ručně vyřazené nikdy."""
    relevant = {
        m.product_id
        for m in session.scalars(
            select(CriteriaMatch).where(
                CriteriaMatch.criteria_id == trap.id,
                CriteriaMatch.relevant,
            )
        )
    }
    for fb in session.scalars(select(UserFeedback)):
        if fb.verdict == Verdict.LIKE:
            relevant.add(fb.product_id)
        elif fb.verdict in (Verdict.DISLIKE, Verdict.OWNED):
            relevant.discard(fb.product_id)
    return relevant


def evaluate_all(session: Session) -> int:
    """Projdi aktivní pasti s budgetem a pošli alerty. Vrací počet nových."""
    sent = 0
    traps = session.scalars(
        select(Criteria).where(Criteria.active, Criteria.budget_max.is_not(None))
    ).all()
    for trap in traps:
        for product_id in _watched_product_ids(session, trap):
            product = session.get(Product, product_id)
            if product is None:
                continue
            prices = latest_prices(session, product)
            if not prices:
                continue
            best = min(prices)
            if best > trap.budget_max:
                continue
            dedup = f"budget:{trap.id}:{product.id}"
            exists = session.scalars(
                select(Alert).where(Alert.dedup_key == dedup)
            ).first()
            if exists:
                continue
            url = next(
                (o.url for o in product.offers if o.active), None
            )
            title = f"{trap.name}: {best:.0f} Kč"
            message = f"{product.title} — pod budgetem {trap.budget_max:.0f} Kč"
            delivered = send_ntfy(title, message, url)
            session.add(Alert(
                dedup_key=dedup,
                product_id=product.id,
                score=float(best),
                payload={
                    "trap": trap.name,
                    "product": product.title,
                    "price": best,
                    "budget": trap.budget_max,
                    "url": url,
                    "ntfy": delivered,
                },
            ))
            session.commit()
            sent += 1
            log.info("alert: %s (%s Kč) pro past „%s“", product.title, best, trap.name)
    if not sent:
        log.info("alerty: nic nového pod budgetem")
    return sent
