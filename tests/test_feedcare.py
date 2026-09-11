"""Zdroje feedů, které nikomu neslouží: vypnout, pak smazat (ADR-0011)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import db, feedcare
from trapline.api.main import app
from trapline.config import settings
from trapline.models import (
    Base,
    Criteria,
    CriteriaMatch,
    FeedSource,
    Offer,
    Product,
    Source,
)

NOW = datetime(2026, 9, 10, 12, 0)


@pytest.fixture
def session(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_ready", True)
    monkeypatch.setattr(settings, "app_password", "")
    monkeypatch.setattr(settings, "watch_min_score", 40.0)
    monkeypatch.setattr(settings, "feed_purge_days", 14)
    monkeypatch.setattr(feedcare, "_now", lambda: NOW)
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(session):
    return TestClient(app)


def _feed(session, name, *, ran=True, enabled=True, useless_since=None):
    source = FeedSource(
        name=name, url=f"https://{name}/feed.xml", enabled=enabled,
        last_run=NOW - timedelta(days=1) if ran else None,
        useless_since=useless_since,
    )
    session.add(source)
    session.commit()
    return source


def _produkt_z_feedu(session, feed_name, title, trap=None, score=90.0):
    """Produkt naimportovaný feedem; s pastí = někdo o něj stojí."""
    product = Product(
        brand="X", model=title, model_norm=title.lower(), title=title,
    )
    session.add(product)
    session.flush()
    session.add(Offer(
        product_id=product.id, source=Source.HEUREKA_FEED, shop=feed_name,
        url=f"https://e.cz/{product.id}", sku=title, active=True,
    ))
    if trap is not None:
        session.add(CriteriaMatch(
            criteria_id=trap.id, product_id=product.id,
            score=score, relevant=score >= 60,
        ))
    session.commit()
    return product


def _past(session, name="Past", active=True):
    trap = Criteria(name=name, query_terms=["x"], prefilter="x", active=active)
    session.add(trap)
    session.commit()
    return trap


# --- koho se to týká ---------------------------------------------------------

def test_slouzici_feed_zustane(session):
    trap = _past(session)
    feed = _feed(session, "gocamp.cz")
    _produkt_z_feedu(session, "gocamp.cz", "Chladnička", trap)

    assert feedcare.review(session) == {
        "disabled": [], "deleted": [], "serving": 1,
    }
    session.refresh(feed)
    assert feed.enabled is True
    assert feed.useless_since is None


def test_slouzi_i_pod_prahem_hlidani(session):
    """Past produkt zná, jen ho neobchází — feed pořád slouží."""
    trap = _past(session)
    feed = _feed(session, "kulina.cz")
    _produkt_z_feedu(session, "kulina.cz", "Něco", trap, score=10.0)

    feedcare.review(session)
    session.refresh(feed)
    assert feed.enabled is True


def test_nepotrebny_feed_se_vypne_a_popise(session):
    _past(session)
    feed = _feed(session, "porcelanovysvet.cz")
    _produkt_z_feedu(session, "porcelanovysvet.cz", "Talíře")  # bez pasti

    r = feedcare.review(session)
    assert r["disabled"] == ["porcelanovysvet.cz"]
    session.refresh(feed)
    assert feed.enabled is False
    assert feed.useless_since == NOW
    assert "vypnuto automaticky" in feed.last_status


def test_novy_feed_se_nesaha(session):
    """Přidaný, ale ještě neproběhlý feed nemá co dokázat."""
    feed = _feed(session, "novy.cz", ran=False)
    assert feedcare.review(session)["disabled"] == []
    session.refresh(feed)
    assert feed.enabled is True


def test_feed_bez_nabidek_se_nesaha(session):
    """Prázdný výsledek může být výpadek sítě — vypnout by byla chyba."""
    feed = _feed(session, "docasne-mimo.cz")
    assert feedcare.review(session)["disabled"] == []
    session.refresh(feed)
    assert feed.enabled is True


# --- druhý stupeň: smazání ---------------------------------------------------

def test_dlouho_vypnuty_feed_se_smaze(session):
    _past(session)
    _feed(session, "luis.cz", enabled=False,
          useless_since=NOW - timedelta(days=14))
    _produkt_z_feedu(session, "luis.cz", "Příbory")

    assert feedcare.review(session)["deleted"] == ["luis.cz"]
    assert session.scalars(select(FeedSource)).all() == []


def test_ve_lhute_se_jeste_nemaze(session):
    _past(session)
    _feed(session, "luis.cz", enabled=False,
          useless_since=NOW - timedelta(days=13))
    _produkt_z_feedu(session, "luis.cz", "Příbory")

    assert feedcare.review(session)["deleted"] == []
    assert len(session.scalars(select(FeedSource)).all()) == 1


def test_nula_dni_znamena_nemazat(session, monkeypatch):
    monkeypatch.setattr(settings, "feed_purge_days", 0)
    _past(session)
    _feed(session, "luis.cz", enabled=False,
          useless_since=NOW - timedelta(days=365))
    _produkt_z_feedu(session, "luis.cz", "Příbory")

    assert feedcare.review(session)["deleted"] == []
    assert len(session.scalars(select(FeedSource)).all()) == 1


def test_obnovena_uzitecnost_zrusi_odpocet(session):
    """Zapneš past zpátky → feed zase slouží a lhůta se zahodí."""
    trap = _past(session)
    feed = _feed(session, "gocamp.cz", useless_since=NOW - timedelta(days=10))
    _produkt_z_feedu(session, "gocamp.cz", "Chladnička", trap)

    feedcare.review(session)
    session.refresh(feed)
    assert feed.useless_since is None


# --- API ---------------------------------------------------------------------

def test_prehled_a_rucni_spusteni(client, session):
    trap = _past(session)
    _feed(session, "gocamp.cz")
    _produkt_z_feedu(session, "gocamp.cz", "Chladnička", trap)
    _feed(session, "porcelanovysvet.cz")
    _produkt_z_feedu(session, "porcelanovysvet.cz", "Talíře")

    d = client.get("/api/admin/feeds").json()
    assert d == {
        "sources": 2, "serving": 1, "useless": 1,
        "waiting_for_delete": 0, "purge_days": 14,
    }

    r = client.post("/api/admin/feeds/review").json()
    assert r["disabled"] == ["porcelanovysvet.cz"]
    assert client.get("/api/admin/feeds").json()["waiting_for_delete"] == 1


def test_lhuta_jde_nastavit_z_administrace(client, session):
    r = client.put("/api/admin/settings", json={"feed_purge_days": 30})
    assert r.status_code == 200
    assert settings.feed_purge_days == 30
    assert client.put(
        "/api/admin/settings", json={"feed_purge_days": -1}
    ).status_code == 422


def test_migrace_obsahuje_useless_since():
    assert any(
        table == "feed_sources" and column == "useless_since"
        for table, column, _ddl in db._MIGRATIONS
    )
