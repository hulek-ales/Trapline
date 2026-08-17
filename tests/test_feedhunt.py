"""Testy automatického hledání zdrojů (SearXNG → domény → feedy)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import db, feedhunt
from trapline.api.main import app
from trapline.config import settings
from trapline.crawlers.heureka_feed import FeedItem
from trapline.models import Base, Criteria, FeedSource


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


def _trap(session, prefilter="chladni") -> Criteria:
    trap = Criteria(
        name="Camping lednička", query_terms=["provoz na 12V"], prefilter=prefilter,
    )
    session.add(trap)
    session.commit()
    return trap


def test_domain_normalizace():
    assert feedhunt._domain("https://www.eshop.cz/produkt/x") == "eshop.cz"
    assert feedhunt._domain("http://sub.eshop.cz/") == "sub.eshop.cz"


def test_blacklist_filtruje(monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://searx:1")

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"url": "https://www.heureka.cz/x"},
                {"url": "https://www.dobryshop.cz/a"},
                {"url": "https://www.dobryshop.cz/b"},
                {"url": "https://bazos.cz/inzerat"},
            ]}

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(feedhunt.httpx, "Client", FakeClient)
    monkeypatch.setattr(feedhunt.time, "sleep", lambda s: None)
    domains = feedhunt.search_domains(["autochladnička"])
    assert domains == ["dobryshop.cz"]


def test_derive_queries_fallback(session, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("LLM mimo")
    monkeypatch.setattr(feedhunt.llm, "chat_json", boom)
    trap = _trap(session)
    assert feedhunt.derive_queries(trap) == ["Camping lednička"]


def test_run_zaklada_vypnute_navrhy(session, monkeypatch):
    trap = _trap(session)
    # existující zdroj se přeskočí
    session.add(FeedSource(name="Bestberg", url="https://www.bestberg.cz/heureka/export/products.xml"))
    session.commit()

    monkeypatch.setattr(feedhunt, "derive_queries", lambda t: ["autochladnička"])
    monkeypatch.setattr(
        feedhunt, "search_domains",
        lambda q: ["bestberg.cz", "novyshop.cz", "bezfeedu.cz"],
    )
    monkeypatch.setattr(
        feedhunt, "probe_domain",
        lambda d: f"https://{d}/heureka.xml" if d == "novyshop.cz" else None,
    )
    monkeypatch.setattr(feedhunt, "evaluate_feed", lambda url, t: (200, 12))
    monkeypatch.setattr(feedhunt.time, "sleep", lambda s: None)

    feedhunt._run(trap.id)

    rows = session.scalars(
        select(FeedSource).where(FeedSource.name == "novyshop.cz")
    ).all()
    assert len(rows) == 1
    src = rows[0]
    assert src.enabled is False               # návrh, ne aktivní zdroj
    assert src.category_filter == "chladni"   # zdědí předfiltr pasti
    assert "12/200" in src.last_status


def test_run_preskoci_feed_bez_shody(session, monkeypatch):
    trap = _trap(session)
    monkeypatch.setattr(feedhunt, "derive_queries", lambda t: ["x"])
    monkeypatch.setattr(feedhunt, "search_domains", lambda q: ["irelevantni.cz"])
    monkeypatch.setattr(feedhunt, "probe_domain", lambda d: f"https://{d}/heureka.xml")
    monkeypatch.setattr(feedhunt, "evaluate_feed", lambda url, t: (500, 0))
    monkeypatch.setattr(feedhunt.time, "sleep", lambda s: None)
    feedhunt._run(trap.id)
    assert session.scalars(
        select(FeedSource).where(FeedSource.name == "irelevantni.cz")
    ).first() is None


def test_evaluate_feed_pocita_shodu(monkeypatch, session):
    trap = _trap(session, prefilter="chladni")
    items = [
        FeedItem(item_id="1", name="Autochladnička X", url="u", price=1.0,
                 category="Chladicí boxy"),
        FeedItem(item_id="2", name="Stan", url="u", price=1.0, category="Stany"),
    ]
    monkeypatch.setattr(feedhunt.heureka_feed, "fetch", lambda url: items)
    assert feedhunt.evaluate_feed("https://x/f.xml", trap) == (2, 1)


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


def test_hunt_bez_searxng_je_400(client, monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "")
    client.post("/api/criteria", json={"name": "x", "query_terms": ["a"]})
    assert client.post("/api/discovery/hunt/1").status_code == 400


def test_hunt_neexistujici_past_404(client, monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://searx:1")
    assert client.post("/api/discovery/hunt/999").status_code == 404


def test_hunt_start(client, monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://searx:1")
    monkeypatch.setattr(feedhunt, "start", lambda cid: True)
    client.post("/api/criteria", json={"name": "Lednička", "query_terms": ["a"]})
    r = client.post("/api/discovery/hunt/1")
    assert r.status_code == 202
    assert "Lednička" in r.json()["message"]


def test_hunt_je_za_heslem(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "tajne")
    assert client.post("/api/discovery/hunt/1").status_code == 401
    assert client.get("/api/discovery/hunt/status").status_code == 401
