"""Práh hlídání, úklid sirotků a dopad pasti (ADR-0011)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import catalog, db
from trapline.api.main import app
from trapline.config import settings
from trapline.models import (
    Alert,
    Base,
    Criteria,
    CriteriaMatch,
    Offer,
    PriceHistory,
    Product,
    Source,
    UserFeedback,
)


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
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(session):
    return TestClient(app)


def _produkt(session, title) -> Product:
    product = Product(
        brand="X", model=title, model_norm=title.lower(), title=title,
    )
    session.add(product)
    session.flush()
    session.add(Offer(
        product_id=product.id, source=Source.JSONLD, shop="e.cz",
        url=f"https://e.cz/{product.id}", sku=title, active=True,
    ))
    session.commit()
    return product


def _past(session, name="Past", active=True) -> Criteria:
    trap = Criteria(name=name, query_terms=["x"], prefilter="x", active=active)
    session.add(trap)
    session.commit()
    return trap


def _match(session, trap, product, score, relevant=False) -> None:
    session.add(CriteriaMatch(
        criteria_id=trap.id, product_id=product.id,
        score=score, relevant=relevant,
    ))
    session.commit()


# --- práh hlídání ------------------------------------------------------------

def test_prah_pousti_relevantni_a_dost_dobre(session):
    trap = _past(session)
    relevantni = _produkt(session, "Relevantní, ale nízké skóre")
    nad = _produkt(session, "Nerelevantní nad prahem")
    pod = _produkt(session, "Nerelevantní pod prahem")
    _match(session, trap, relevantni, 20.0, relevant=True)  # relevantní vždy
    _match(session, trap, nad, 55.0)
    _match(session, trap, pod, 15.0)

    assert catalog.watched_product_ids(session) == {relevantni.id, nad.id}
    # pod prahem past pořád zná — není to sirotek
    assert pod.id in catalog.known_product_ids(session)
    assert catalog.orphan_ids(session) == set()


def test_prah_jde_zmenit_za_behu(session, monkeypatch):
    trap = _past(session)
    produkt = _produkt(session, "Skóre 30")
    _match(session, trap, produkt, 30.0)
    assert catalog.watched_product_ids(session) == set()

    monkeypatch.setattr(settings, "watch_min_score", 25.0)
    assert catalog.watched_product_ids(session) == {produkt.id}


def test_prah_se_uklada_z_administrace(client, session):
    r = client.put("/api/admin/settings", json={"watch_min_score": 25})
    assert r.status_code == 200
    assert r.json()["llm"]["watch_min_score"] == 25.0
    assert settings.watch_min_score == 25.0
    assert client.put(
        "/api/admin/settings", json={"watch_min_score": 101}
    ).status_code == 422


# --- přehled a úklid ---------------------------------------------------------

def test_overview_rozlisuje_sirotky_a_podprahove(client, session):
    trap = _past(session)
    hlidany = _produkt(session, "Hlídaný")
    podprahovy = _produkt(session, "Pod prahem")
    _produkt(session, "Sirotek")
    _match(session, trap, hlidany, 90.0, relevant=True)
    _match(session, trap, podprahovy, 10.0)

    d = client.get("/api/admin/catalog").json()
    assert d == {
        "products": 3, "watched": 1, "below_threshold": 1, "orphans": 1,
        "active_offers": 3, "watched_offers": 1, "min_score": 40.0,
    }


def test_purge_smaze_jen_sirotky_i_s_daty(client, session):
    trap = _past(session)
    hlidany = _produkt(session, "Hlídaný")
    podprahovy = _produkt(session, "Pod prahem")
    sirotek = _produkt(session, "Sirotek")
    _match(session, trap, hlidany, 90.0, relevant=True)
    _match(session, trap, podprahovy, 10.0)

    # sirotek má navěšená data ze všech stran — musí zmizet s ním
    offer = session.scalars(
        select(Offer).where(Offer.product_id == sirotek.id)
    ).one()
    session.add(PriceHistory(offer_id=offer.id, price=100.0, in_stock=True))
    session.add(UserFeedback(product_id=sirotek.id))
    session.add(Alert(
        dedup_key="x", product_id=sirotek.id, offer_id=offer.id, score=1.0,
    ))
    session.commit()

    r = client.post("/api/admin/catalog/purge").json()
    assert r == {"products": 1, "offers": 1, "prices": 1}

    session.expire_all()
    zbyle = {p.title for p in session.scalars(select(Product))}
    assert zbyle == {"Hlídaný", "Pod prahem"}
    assert session.scalars(select(PriceHistory)).all() == []
    assert session.scalars(select(Alert)).all() == []
    assert session.scalars(select(UserFeedback)).all() == []


def test_purge_bez_sirotku_nic_nedela(client, session):
    trap = _past(session)
    _match(session, trap, _produkt(session, "Hlídaný"), 90.0, relevant=True)
    assert client.post("/api/admin/catalog/purge").json() == {
        "products": 0, "offers": 0, "prices": 0,
    }


def test_vypnuta_past_dela_ze_svych_produktu_sirotky(client, session):
    """Vypnutá past se nehlídá — ale její produkty jde uklidit teprve
    tehdy, když se s tím uživatel smíří vědomě."""
    trap = _past(session, active=False)
    produkt = _produkt(session, "Po vypnuté pasti")
    _match(session, trap, produkt, 90.0, relevant=True)

    assert catalog.watched_product_ids(session) == set()
    assert catalog.orphan_ids(session) == {produkt.id}
    assert client.post("/api/admin/catalog/purge").json()["products"] == 1


# --- dopad pasti -------------------------------------------------------------

def test_seznam_pasti_nese_dopad(client, session):
    trap = _past(session, "Camping lednička")
    for score, relevant in ((90.0, True), (55.0, False), (10.0, False)):
        _match(session, trap, _produkt(session, f"P{score}"), score, relevant)

    row = client.get("/api/criteria").json()[0]
    assert row["scored"] == 3
    assert row["relevant"] == 1
    assert row["watched_offers"] == 2      # relevantní + nad prahem


def test_dopad_pasti_bez_vysledku(client, session):
    _past(session, "Nová past")
    row = client.get("/api/criteria").json()[0]
    assert (row["scored"], row["relevant"], row["watched_offers"]) == (0, 0, 0)
