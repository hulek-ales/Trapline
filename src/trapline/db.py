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
from .models import Base, Source

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
    (
        "criteria",
        "last_hunt",
        "ALTER TABLE criteria ADD COLUMN last_hunt DATETIME NULL",
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
    _migrate_source_enum(engine, inspector, tables)


def source_enum_ddl(table: str) -> str:
    """MODIFY podle aktuálního výčtu Source — SQLAlchemy ukládá JMÉNA členů."""
    names = ", ".join(f"'{m.name}'" for m in Source)
    return f"ALTER TABLE {table} MODIFY COLUMN source ENUM({names}) NOT NULL"


def _migrate_source_enum(engine: Engine, inspector, tables: set[str]) -> None:
    """Nový člen Source (např. ZBOZI) do nativního MySQL ENUM — create_all
    existující sloupec nezmění a INSERT by spadl. SQLite ukládá text, tam
    není co migrovat."""
    if engine.dialect.name not in ("mysql", "mariadb"):
        return
    for table in ("offers", "listings"):
        if table not in tables:
            continue
        col = next(
            (c for c in inspector.get_columns(table) if c["name"] == "source"), None
        )
        if col is None:
            continue
        have = str(col["type"]).upper()
        if all(m.name in have for m in Source):
            continue
        with engine.begin() as conn:
            conn.execute(text(source_enum_ddl(table)))
        log.info("migrace: %s.source rozšířen o nové zdroje", table)


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
