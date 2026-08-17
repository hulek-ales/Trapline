"""Připojení k databázi a inicializace schématu.

Schéma se zatím vytváří přes ``create_all`` — Alembic přijde na řadu, až
bude potřeba první skutečná migrace existujících dat (viz TODO v README).

Engine vzniká líně a start appky na databázi nečeká: ``/api/system/*`` musí
fungovat i s lehlou DB, jinak by nešel spustit self-update, který by ji
třeba opravil. Doménové endpointy si dostupnost vynutí přes ``ensure_ready``
a při výpadku vrací 503 — a při dalším requestu to zkusí znovu, takže se
appka po náběhu DB sama chytne.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import settings
from .models import Base

log = logging.getLogger("trapline.db")

_engine: Engine | None = None
_ready: bool = False


def is_ready() -> bool:
    """Poslední známý stav bez dotyku DB — pro health, který nesmí čekat
    na connection timeout."""
    return _ready


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings.db_url, pool_pre_ping=True)
    return _engine


#: Aditivní migrace: create_all nové sloupce do existujících tabulek
#: nepřidá. Alembic přijde s první destruktivní změnou; do té doby stačí
#: idempotentní ALTERy (přeskočí se, když sloupec existuje).
_MIGRATIONS: list[tuple[str, str, str]] = [
    (
        "criteria",
        "prefilter",
        "ALTER TABLE criteria ADD COLUMN prefilter VARCHAR(500) NOT NULL DEFAULT ''",
    ),
]


def _migrate(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, column, ddl in _MIGRATIONS:
        if table not in tables:
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if column in columns:
            continue
        with engine.begin() as conn:
            conn.execute(text(ddl))
        log.info("migrace: %s.%s přidán", table, column)


def ensure_ready() -> bool:
    """Vytvoř schéma, pokud ještě není. True = DB je použitelná."""
    global _ready
    if _ready:
        return True
    try:
        engine = get_engine()
        Base.metadata.create_all(engine)
        _migrate(engine)
        _ready = True
        log.info("Databáze připravená (%s tabulek).", len(Base.metadata.tables))
    except Exception as exc:  # noqa: BLE001
        log.warning("Databáze není dostupná: %s", exc)
    return _ready


def open_session() -> Session:
    return Session(get_engine())
