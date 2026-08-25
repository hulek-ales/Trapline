"""Bazary: Bazoš + Sbazar → inzeráty → LLM vyhodnocení → alerty.

Jiná disciplína než eshopy: inzeráty žijí krátce, nemají EAN ani parametry
a cena se nemění — hodnota je v rychlém zachycení nového kusu pod cenou.
Proto per past:

  1. kandidáti — Bazoš: výpis 1–2 sekcí od nejnovějších (sekce vybírá LLM
     z pevného seznamu, hledat robots nedovoluje); Sbazar: hledání frází
     z předfiltru pasti,
  2. levný filtr předfiltrem (název + krátký popis),
  3. nové kusy: detail → LLM verdikt proti požadavkům pasti + stav zboží
     + varovné signály (platba předem…) → ``ListingMatch``,
  4. relevantní pod budgetem → alert (dedup ``listing:{source}:{ext_id}``),
  5. u dřívějších relevantních kusů kontrola, jestli inzerát ještě žije
     (``gone_at`` = signál reálné prodejní ceny pro budoucí reference).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db, llm
from .alerts import send_ntfy
from .config import settings
from .crawlers import bazos, sbazar
from .crawlers.heureka_feed import normalize
from .models import Alert, Condition, Criteria, Listing, ListingMatch, Source

log = logging.getLogger("trapline.bazar")

#: Kolik stránek výpisu sekce Bazoše projít (řazené od nejnovějších).
BAZOS_PAGES = 3
#: Strop LLM vyhodnocení nových inzerátů na past a běh (GPU čas).
MAX_EVAL_PER_TRAP = 15
#: Strop kontrol „žije ještě?" na běh.
MAX_ALIVE_CHECKS = 20

_SECTION_SCHEMA = {
    "type": "object",
    "properties": {"sekce": {"type": "array", "items": {"type": "string"}}},
    "required": ["sekce"],
}

_EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "pozadavky": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pozadavek": {"type": "string"},
                    "verdikt": {
                        "type": "string",
                        "enum": ["splneno", "nesplneno", "nelze_urcit"],
                    },
                },
                "required": ["pozadavek", "verdikt"],
            },
        },
        "stav": {
            "type": "string",
            "enum": ["nove", "jako_nove", "pouzite", "poskozene", "neznamy"],
        },
        "varovani": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["pozadavky", "stav", "varovani"],
}

_CONDITION = {
    "nove": Condition.NEW,
    "jako_nove": Condition.LIKE_NEW,
    "pouzite": Condition.USED,
    "poskozene": Condition.DAMAGED,
    "neznamy": Condition.UNKNOWN,
}

_EVAL_SYSTEM = (
    "Hodnotíš bazarový inzerát proti požadavkům uživatele. Inzeráty bývají "
    "stručné a bez parametrů — verdikt splneno dávej jen s oporou v textu, "
    "jinak nelze_urcit; nesplneno jen při jasném rozporu (včetně špatného "
    "typu produktu). Odhadni stav zboží z textu (nove/jako_nove/pouzite/"
    "poskozene/neznamy). Do varovani dej rizikové signály: platba předem, "
    "jen osobní odběr daleko, chybějící části, podezřele nízká cena. "
    "Poznámky nepiš, žádné uvozovky."
)

_SECTION_SYSTEM = (
    "Vyber 1 až 2 sekce Bazoše, kde se daný typ zboží nejčastěji prodává. "
    "Odpověz přesně názvy z nabídky, nic jiného: "
    + ", ".join(sorted(bazos.SECTIONS))
)


def _prefilter_terms(trap: Criteria) -> list[str]:
    return [
        normalize(t) for t in (trap.prefilter or "").split(",") if t.strip()
    ]


def _passes(trap: Criteria, title: str, description: str) -> bool:
    terms = _prefilter_terms(trap)
    if not terms:
        return False
    hay = normalize(f"{title} {description}")
    return any(t in hay for t in terms)


def pick_sections(trap: Criteria) -> list[str]:
    try:
        out = llm.chat_json(
            _SECTION_SYSTEM,
            f"Zboží: {trap.name}. Kategorie: {trap.prefilter}",
            _SECTION_SCHEMA,
        )
        sections = [s for s in out.get("sekce", []) if s in bazos.SECTIONS]
        if sections:
            return sections[:2]
    except Exception as exc:  # noqa: BLE001
        log.warning("bazar: výběr sekcí selhal (%s), jedu 'ostatni'", exc)
    return ["ostatni"]


def evaluate_listing(trap: Criteria, listing: Listing) -> dict:
    user = (
        f"Požadavky: {', '.join(trap.query_terms)}\n"
        f"Inzerát: {listing.title}\n"
        f"Cena: {listing.price or 'dohodou'} Kč, lokalita: {listing.locality}\n"
        f"Text: {listing.description or ''}"
    )
    return llm.chat_json(_EVAL_SYSTEM, user, _EVAL_SCHEMA)


def _score(verdicts: list[dict]) -> tuple[float, bool]:
    if not verdicts:
        return 0.0, False
    weights = {"splneno": 1.0, "nelze_urcit": 0.5, "nesplneno": 0.0}
    vals = [weights.get(v.get("verdikt"), 0.5) for v in verdicts]
    score = round(100.0 * sum(vals) / len(vals), 1)
    failed = any(v.get("verdikt") == "nesplneno" for v in verdicts)
    return score, (not failed and score >= 60.0)


def _upsert_listing(session: Session, source: Source, ad) -> tuple[Listing, bool]:
    """(řádek, je nový?). Existující inzerát jen dostane čerstvé last_seen."""
    row = session.scalars(
        select(Listing).where(
            Listing.source == source, Listing.ext_id == ad.ext_id
        )
    ).first()
    now = datetime.now(UTC).replace(tzinfo=None)
    if row is not None:
        row.last_seen = now
        return row, False
    row = Listing(
        source=source,
        ext_id=ad.ext_id,
        url=ad.url,
        title=ad.title,
        description=getattr(ad, "description", "") or None,
        price=ad.price if ad.price is not None else 0.0,
        locality=ad.locality or None,
    )
    session.add(row)
    session.flush()
    return row, True


def _candidates(trap: Criteria) -> list[tuple[Source, object]]:
    out: list[tuple[Source, object]] = []
    for section in pick_sections(trap):
        for page in range(BAZOS_PAGES):
            try:
                ads = bazos.fetch_listing(section, page * bazos.PAGE_SIZE)
            except Exception as exc:  # noqa: BLE001
                log.warning("bazar: bazos %s/%d selhal: %s", section, page, exc)
                break
            out.extend((Source.BAZOS, ad) for ad in ads)
            time.sleep(settings.request_delay_s)
    phrases = [t.strip() for t in (trap.prefilter or "").split(",") if t.strip()]
    for phrase in phrases[:3] or [trap.name]:
        try:
            out.extend((Source.SBAZAR, ad) for ad in sbazar.search(phrase))
        except Exception as exc:  # noqa: BLE001
            log.warning("bazar: sbazar „%s“ selhal: %s", phrase, exc)
        time.sleep(settings.request_delay_s)
    return out


def _full_description(source: Source, listing: Listing) -> None:
    """Doplň plný popis z detailu (Bazoš má ve výpisu jen zkrácený)."""
    try:
        if source == Source.BAZOS:
            text = bazos.fetch_detail(listing.url)
        else:
            text, _alive = sbazar.detail(listing.ext_id)
        if text:
            listing.description = text
    except Exception as exc:  # noqa: BLE001
        log.warning("bazar: detail %s selhal: %s", listing.url, exc)


def _alert(session: Session, trap: Criteria, listing: Listing, score: float) -> None:
    if trap.budget_max is None or listing.price <= 0:
        return
    if listing.price > trap.budget_max:
        return
    dedup = f"listing:{listing.source.value}:{listing.ext_id}"
    if session.scalars(select(Alert).where(Alert.dedup_key == dedup)).first():
        return
    delivered = send_ntfy(
        f"{trap.name}: bazar {listing.price:.0f} Kč",
        f"{listing.title} — {listing.locality or '?'}",
        listing.url,
    )
    session.add(Alert(
        dedup_key=dedup,
        listing_id=listing.id,
        score=score,
        payload={
            "trap": trap.name,
            "listing": listing.title,
            "price": listing.price,
            "locality": listing.locality,
            "url": listing.url,
            "bazar": listing.source.value,
            "ntfy": delivered,
        },
    ))
    log.info(
        "bazar alert: %s (%s Kč, %s) pro past „%s“",
        listing.title, listing.price, listing.locality, trap.name,
    )


def _check_alive(session: Session) -> int:
    """Ověř, jestli relevantní inzeráty ještě žijí; zmizelé oznámkuj."""
    rows = session.scalars(
        select(Listing)
        .join(ListingMatch, ListingMatch.listing_id == Listing.id)
        .where(Listing.gone_at.is_(None), ListingMatch.confidence >= 0.6)
        .limit(MAX_ALIVE_CHECKS)
    ).all()
    gone = 0
    for listing in rows:
        try:
            if listing.source == Source.BAZOS:
                alive = bazos.fetch_detail(listing.url) is not None
            else:
                _text, alive = sbazar.detail(listing.ext_id)
        except Exception:  # noqa: BLE001 — nejistota = nechat žít
            continue
        if not alive:
            listing.gone_at = datetime.now(UTC).replace(tzinfo=None)
            gone += 1
        time.sleep(settings.request_delay_s)
    return gone


def run_all() -> tuple[int, int]:
    """Jeden průchod bazarů pro všechny aktivní pasti s předfiltrem.
    Vrací (nových inzerátů, z toho relevantních)."""
    if not db.ensure_ready():
        log.warning("bazar: databáze nedostupná")
        return 0, 0
    new_total = relevant_total = 0
    with db.open_session() as session:
        traps = [
            t for t in session.scalars(
                select(Criteria).where(Criteria.active)
            )
            if t.prefilter and t.query_terms
        ]
        for trap in traps:
            evaluated = 0
            for source, ad in _candidates(trap):
                if not _passes(trap, ad.title, getattr(ad, "description", "")):
                    continue
                listing, is_new = _upsert_listing(session, source, ad)
                if not is_new or evaluated >= MAX_EVAL_PER_TRAP:
                    session.commit()
                    continue
                new_total += 1
                evaluated += 1
                _full_description(source, listing)
                try:
                    verdict = evaluate_listing(trap, listing)
                except Exception as exc:  # noqa: BLE001
                    log.warning("bazar: LLM u %s selhalo: %s", listing.url, exc)
                    session.commit()
                    continue
                score, relevant = _score(verdict.get("pozadavky", []))
                session.add(ListingMatch(
                    listing_id=listing.id,
                    criteria_id=trap.id,
                    confidence=score / 100.0,
                    condition=_CONDITION.get(
                        verdict.get("stav"), Condition.UNKNOWN
                    ),
                    red_flags=verdict.get("varovani", []),
                    model_used=settings.llm_main,
                ))
                if relevant:
                    relevant_total += 1
                    _alert(session, trap, listing, score)
                session.commit()
                log.info(
                    "bazar: %s — %s Kč, skóre %.0f%s",
                    listing.title, listing.price or "dohodou", score,
                    " ✓" if relevant else "",
                )
        gone = _check_alive(session)
        session.commit()
    log.info(
        "bazar: hotovo — %d nových inzerátů, %d relevantních, %d zmizelo",
        new_total, relevant_total, gone,
    )
    return new_total, relevant_total
