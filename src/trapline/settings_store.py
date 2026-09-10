"""Nastavení upravitelné z GUI — vrstva nad proměnnými prostředí (ADR-0010).

Proměnné prostředí v YAML appky zůstávají výchozí hodnotou a záchranou,
když DB neběží. Co uživatel uloží v Administraci, se zapíše do tabulky
``app_settings`` a **má přednost**: při startu i po každém uložení se
promítne přímo do ``settings``, takže zbytek kódu čte pořád jeden objekt
a nic o vrstvě neví.

Rozsah je záměrně malý — LLM a práh hlídání. Tajné hodnoty (klíč proxy) se ukládají
jako každé jiné nastavení, ale ven jdou jen jako „je/není vyplněný".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .models import AppSetting

log = logging.getLogger("trapline.settings")


@dataclass(frozen=True, slots=True)
class Field:
    cast: Callable[[str], Any]
    secret: bool = False


#: Co smí GUI měnit. Klíč = atribut ``settings`` i klíč v DB.
FIELDS: dict[str, Field] = {
    "ollama_url": Field(str),
    "ollama_key": Field(str, secret=True),
    "llm_main": Field(str),
    "llm_bulk": Field(str),
    "llm_load_wait_s": Field(float),
    "watch_min_score": Field(float),
}

#: Hodnoty z prostředí zachycené při startu — kam se vrací „reset".
_ENV: dict[str, Any] = {k: getattr(settings, k) for k in FIELDS}
#: Které klíče právě přebíjí DB (kvůli hlášení „z GUI" / „z prostředí").
_from_db: set[str] = set()


def _apply(key: str, raw: str) -> None:
    try:
        value = FIELDS[key].cast(raw)
    except (TypeError, ValueError):
        log.warning("nastavení %s=%r nejde přečíst, nechávám prostředí", key, raw)
        return
    setattr(settings, key, value)
    _from_db.add(key)


def load_overrides(session: Session) -> set[str]:
    """Promítni uložené hodnoty do ``settings``. Vrací přebité klíče."""
    rows = session.scalars(
        select(AppSetting).where(AppSetting.key.in_(list(FIELDS)))
    ).all()
    for row in rows:
        _apply(row.key, row.value)
    if _from_db:
        log.info("nastavení z GUI: %s", ", ".join(sorted(_from_db)))
    return set(_from_db)


def save(session: Session, values: dict[str, Any]) -> None:
    """Ulož a hned použij. Neznámé klíče a ``None`` se ignorují (= beze
    změny); prázdný řetězec je platná hodnota (u klíče = smazat)."""
    from . import llm  # noqa: PLC0415 — cyklus přes config

    for key, value in values.items():
        if key not in FIELDS or value is None:
            continue
        raw = str(value).strip()
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=raw))
        else:
            row.value = raw
        _apply(key, raw)
    session.commit()
    # Nová URL může mít plánovač, který ta stará neměla — ať se zkusí znovu.
    llm.reset_state()


def reset(session: Session) -> None:
    """Zahoď hodnoty z GUI a vrať se k prostředí."""
    from . import llm  # noqa: PLC0415

    session.execute(delete(AppSetting).where(AppSetting.key.in_(list(FIELDS))))
    session.commit()
    for key, value in _ENV.items():
        setattr(settings, key, value)
    _from_db.clear()
    llm.reset_state()


def snapshot() -> dict[str, Any]:
    """Co GUI smí vidět: hodnoty, u tajných jen „vyplněno", a odkud jsou."""
    out: dict[str, Any] = {}
    for key, field in FIELDS.items():
        value = getattr(settings, key)
        if field.secret:
            out[f"{key}_set"] = bool(value)
        else:
            out[key] = value
    out["source"] = {
        key: ("gui" if key in _from_db else "env") for key in FIELDS
    }
    return out
