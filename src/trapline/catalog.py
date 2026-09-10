"""Co z katalogu ještě někdo hlídá.

Produkty se do katalogu dostanou z feedů a z crawleru pastí (ADR-0007)
a už tam zůstanou — i když past, kvůli které se našly, zanikne. Obnova cen
proto potřebuje vědět, na co ještě sahat: každá hlídaná stránka je jeden
HTTP požadavek v každé obchůzce, u obchodů za Cloudflarem i spuštění
Chromu (ADR-0006). Bez tohohle filtru obchůzka donekonečna obchází
stránky, o které nikdo nestojí.

„Hlídá" = produkt má výsledek skóringu (``CriteriaMatch``) k aspoň jedné
**aktivní** pasti. Smazaná past si své ``CriteriaMatch`` bere s sebou
(``api.criteria.delete_criteria``), vypnutou vyřadí podmínka. Rozhodnutí
je samoopravné: skóring běží v obchůzce před obnovou cen, takže čerstvě
nalezený produkt dostane match dřív, než na něj dojde řada — a když past
znovu založíš, skóring produkty potká znovu a hlídání se samo obnoví.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Criteria, CriteriaMatch


def watched_product_ids(session: Session) -> set[int]:
    """Id produktů, které chce aspoň jedna aktivní past."""
    rows = session.execute(
        select(CriteriaMatch.product_id)
        .join(Criteria, Criteria.id == CriteriaMatch.criteria_id)
        .where(Criteria.active)
        .distinct()
    )
    return {product_id for (product_id,) in rows}


def watched_offers(session: Session, offers: list) -> tuple[list, int]:
    """(nabídky ke zpracování, kolik jich nikdo nehlídá)."""
    wanted = watched_product_ids(session)
    keep = [o for o in offers if o.product_id in wanted]
    return keep, len(offers) - len(keep)
