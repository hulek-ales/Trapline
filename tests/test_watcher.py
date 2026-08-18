"""Testy referencí, alertů a obchůzky."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import alerts, db, references
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
    Verdict,
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
    with Session(engine) as s:
        yield s


def _product_with_prices(session, prices, title="Lednička X", shipping=0.0):
    product = Product(brand="X", model=title, model_norm=title.lower(), title=title)
    session.add(product)
    session.flush()
    for i, price in enumerate(prices):
        offer = Offer(
            product_id=product.id, source=Source.HEUREKA_FEED,
            shop=f"shop{i}", sku=f"sku{i}", url=f"https://s{i}.example/p",
        )
        session.add(offer)
        session.flush()
        # starší cena + novější — reference musí brát tu poslední
        session.add(PriceHistory(offer_id=offer.id, price=price + 500))
        session.add(PriceHistory(offer_id=offer.id, price=price, shipping=shipping))
    session.commit()
    return product


# --- reference -------------------------------------------------------------

def test_reference_bere_posledni_ceny(session):
    product = _product_with_prices(session, [5000, 6000, 7000])
    ref = references.recompute_product(session, product)
    session.commit()
    assert ref.retail_n == 3
    assert ref.retail_median == 6000
    assert ref.retail_best < 6000


def test_reference_pocita_dopravu(session):
    product = _product_with_prices(session, [1000], shipping=99.0)
    ref = references.recompute_product(session, product)
    assert ref.retail_median == 1099.0


def test_reference_ignoruje_neaktivni(session):
    product = _product_with_prices(session, [5000, 9999])
    product.offers[1].active = False
    session.commit()
    ref = references.recompute_product(session, product)
    assert ref.retail_n == 1


# --- alerty ----------------------------------------------------------------

def _trap_with_match(session, product, budget=6000.0, relevant=True):
    trap = Criteria(name="Lednička", query_terms=["x"], budget_max=budget)
    session.add(trap)
    session.flush()
    session.add(CriteriaMatch(
        criteria_id=trap.id, product_id=product.id, score=100.0, relevant=relevant,
    ))
    session.commit()
    return trap


def test_alert_pod_budgetem(session, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_ntfy", lambda *a, **kw: sent.append(a) or True)
    product = _product_with_prices(session, [5500])
    _trap_with_match(session, product, budget=6000)
    assert alerts.evaluate_all(session) == 1
    assert sent and "5500" in sent[0][0]
    # druhé kolo: deduplikace, nic se neposílá
    assert alerts.evaluate_all(session) == 0
    assert session.query(Alert).count() == 1


def test_alert_nad_budgetem_neni(session, monkeypatch):
    monkeypatch.setattr(alerts, "send_ntfy", lambda *a, **kw: True)
    product = _product_with_prices(session, [9000])
    _trap_with_match(session, product, budget=6000)
    assert alerts.evaluate_all(session) == 0


def test_dislike_alert_blokuje(session, monkeypatch):
    monkeypatch.setattr(alerts, "send_ntfy", lambda *a, **kw: True)
    product = _product_with_prices(session, [5500])
    _trap_with_match(session, product, budget=6000)
    session.add(UserFeedback(product_id=product.id, verdict=Verdict.DISLIKE))
    session.commit()
    assert alerts.evaluate_all(session) == 0


def test_like_prebiji_nerelevantni(session, monkeypatch):
    monkeypatch.setattr(alerts, "send_ntfy", lambda *a, **kw: True)
    product = _product_with_prices(session, [5500])
    _trap_with_match(session, product, budget=6000, relevant=False)
    session.add(UserFeedback(product_id=product.id, verdict=Verdict.LIKE))
    session.commit()
    assert alerts.evaluate_all(session) == 1


def test_neaktivni_past_nealertuje(session, monkeypatch):
    monkeypatch.setattr(alerts, "send_ntfy", lambda *a, **kw: True)
    product = _product_with_prices(session, [5500])
    trap = _trap_with_match(session, product, budget=6000)
    trap.active = False
    session.commit()
    assert alerts.evaluate_all(session) == 0


# --- API -------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_ready", True)
    monkeypatch.setattr(settings, "app_password", "")
    return TestClient(app)


def test_alerts_endpointy(client, monkeypatch):
    assert client.get("/api/alerts").json() == []
    monkeypatch.setattr(settings, "ntfy_topic", "")
    assert client.post("/api/alerts/test").status_code == 400
    monkeypatch.setattr(settings, "ntfy_topic", "moje-pasti")
    monkeypatch.setattr(alerts, "send_ntfy", lambda *a, **kw: True)
    assert client.post("/api/alerts/test").json() == {"ok": True}


def test_jobs_endpoint(client):
    body = client.get("/api/system/jobs").json()
    assert "jobs" in body and "watch_hours" in body


def test_alerts_za_heslem(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "tajne")
    assert client.get("/api/alerts").status_code == 401
    assert client.post("/api/alerts/test").status_code == 401
