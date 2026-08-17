"""LLM skóring produktů proti pastem (fáze 2 z ADR-0003).

LLM dostane požadavky pasti (volný text uživatele) a data produktu a pro
každý požadavek vrátí verdikt splněno / nesplněno / nelze určit, včetně
dopočtů (4× 2l láhev → potřebný vnitřní objem). Číselné skóre se počítá
z verdiktů v kódu, ne v modelu — deterministicky a auditovatelně.

Přepočítává se jen to, co je potřeba: ke každému výsledku se ukládá hash
zadání pasti (criteria_rev); shoduje se → produkt se přeskočí.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db, llm
from .config import settings
from .models import Criteria, CriteriaMatch, Product

log = logging.getLogger("trapline.scoring")

#: Od jakého skóre je produkt relevantní (a bude se mu hlídat cena).
RELEVANT_MIN = 60.0

#: Váhy verdiktů pro výpočet skóre.
_WEIGHTS = {"splneno": 1.0, "nelze_urcit": 0.5, "nesplneno": 0.0}

_SCHEMA = {
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
                    "pozn": {"type": "string"},
                },
                "required": ["pozadavek", "verdikt"],
            },
        },
    },
    "required": ["pozadavky"],
}

_SYSTEM = (
    "Jsi přísný hodnotitel produktů pro nákupního agenta. Uživatel zadal "
    "požadavky volným textem; dostaneš název, značku, parametry a případně "
    "popis produktu. Pro KAŽDÝ požadavek rozhodni verdikt: splneno / "
    "nesplneno / nelze_urcit.\n"
    "Odvozuj, nepapouškuj: přepočítávej rozměry a objemy (např. „4× 2l "
    "láhev“ znamená vnitřní objem zhruba od 16 l a výšku na stojící "
    "láhev ~32 cm; není-li výška známa, posuzuj objem), interpretuj parametry "
    "psané různými formáty („12V/230V“ = „autozásuvka i síť“). "
    "Využívej obecné znalosti o typu produktu (pasivní chladicí box nemá "
    "kompresor ani napájení, takže napájecí požadavky nesplňuje). Když data "
    "chybí a nejde to odvodit ani z typu produktu, dej nelze_urcit.\n"
    "Vrať požadavky ve stejném pořadí. Poznámky piš česky, drž je pod 15 "
    "slov a nepoužívej v nich uvozovky ani apostrofy."
)

_lock = threading.Lock()
_state: dict = {"running": False, "started": None, "finished": None, "log": []}


def status() -> dict:
    with _lock:
        return dict(_state, log=list(_state["log"]))


def _note(msg: str) -> None:
    log.info(msg)
    with _lock:
        _state["log"].append(msg)
        _state["log"] = _state["log"][-100:]


def criteria_rev(trap: Criteria) -> str:
    """Hash zadání pasti — změna zadání zneplatní stará skóre."""
    payload = json.dumps(
        {
            "name": trap.name,
            "query_terms": trap.query_terms,
            "hard": trap.hard,
            "soft": trap.soft,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _product_payload(product: Product) -> dict:
    specs = dict(product.specs or {})
    popis = specs.pop("_popis", None)
    payload = {
        "nazev": product.title,
        "znacka": product.brand,
        "parametry": specs,
    }
    if popis:
        payload["popis"] = popis
    return payload


def _ask_llm(trap: Criteria, product: Product) -> list[dict]:
    """Vrátí breakdown z LLM. Výjimky propadají volajícímu."""
    user = json.dumps(
        {
            "pozadavky": trap.query_terms,
            "produkt": _product_payload(product),
        },
        ensure_ascii=False,
    )
    out = llm.chat_json(_SYSTEM, user, _SCHEMA)
    return out.get("pozadavky", [])


def compute_score(breakdown: list[dict]) -> tuple[float, bool]:
    """Skóre 0–100 z verdiktů; relevantní = žádné tvrdé selhání a skóre
    nad prahem. „Nelze určit" je půl bodu — chybějící data nejsou zamítnutí."""
    if not breakdown:
        return 0.0, False
    weights = [_WEIGHTS.get(b.get("verdikt"), 0.5) for b in breakdown]
    score = round(100.0 * sum(weights) / len(weights), 1)
    failed = any(b.get("verdikt") == "nesplneno" for b in breakdown)
    return score, (not failed and score >= RELEVANT_MIN)


def score_pair(session: Session, trap: Criteria, product: Product) -> CriteriaMatch:
    breakdown = _ask_llm(trap, product)
    score, relevant = compute_score(breakdown)
    match = session.scalars(
        select(CriteriaMatch).where(
            CriteriaMatch.criteria_id == trap.id,
            CriteriaMatch.product_id == product.id,
        )
    ).first()
    if match is None:
        match = CriteriaMatch(criteria_id=trap.id, product_id=product.id)
        session.add(match)
    match.score = score
    match.relevant = relevant
    match.breakdown = breakdown
    match.model_used = settings.llm_main
    match.criteria_rev = criteria_rev(trap)
    match.evaluated_at = datetime.now(UTC)
    return match


def _run_all() -> None:
    try:
        if not db.ensure_ready():
            _note("databáze není dostupná — běh se ruší")
            return
        with db.open_session() as session:
            traps = session.scalars(
                select(Criteria).where(Criteria.active).order_by(Criteria.id)
            ).all()
            traps = [t for t in traps if t.query_terms]
            products = session.scalars(select(Product).order_by(Product.id)).all()
            _note(f"start: {len(traps)} pastí × {len(products)} produktů")

            for trap in traps:
                rev = criteria_rev(trap)
                current = {
                    m.product_id: m
                    for m in session.scalars(
                        select(CriteriaMatch).where(
                            CriteriaMatch.criteria_id == trap.id
                        )
                    )
                }
                todo = [
                    p for p in products
                    if p.id not in current or current[p.id].criteria_rev != rev
                ]
                _note(f"past „{trap.name}“: {len(todo)} k vyhodnocení")
                done = relevant_n = 0
                for product in todo:
                    try:
                        match = score_pair(session, trap, product)
                        session.commit()
                        done += 1
                        relevant_n += bool(match.relevant)
                        if done % 5 == 0:
                            _note(f"past „{trap.name}“: {done}/{len(todo)} hotovo")
                    except Exception as exc:  # noqa: BLE001
                        session.rollback()
                        _note(f"produkt {product.id} selhal: {exc}")
                _note(
                    f"past „{trap.name}“ hotová: {done}/{len(todo)} vyhodnoceno, "
                    f"{relevant_n} nově relevantních"
                )
    finally:
        with _lock:
            _state["running"] = False
            _state["finished"] = datetime.now(UTC).isoformat()


def start() -> bool:
    """Spustí skóring na pozadí. False = už běží."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(
            running=True,
            started=datetime.now(UTC).isoformat(),
            finished=None,
            log=[],
        )
    threading.Thread(target=_run_all, daemon=True, name="scoring").start()
    return True
