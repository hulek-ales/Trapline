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


def test_run_silny_nalez_auto_zapne(session, monkeypatch):
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
    assert src.enabled is True                # silný nález → rovnou aktivní
    assert src.category_filter == "chladni"   # zdědí předfiltr pasti
    assert "auto-zapnuto" in src.last_status
    assert "12/200" in src.last_status
    session.refresh(trap)
    assert trap.last_hunt is not None         # razítko pro throttle obchůzky


def test_run_slaby_nalez_zustava_navrhem(session, monkeypatch):
    trap = _trap(session)
    monkeypatch.setattr(feedhunt, "derive_queries", lambda t: ["x"])
    monkeypatch.setattr(feedhunt, "search_domains", lambda q: ["okraj.cz"])
    monkeypatch.setattr(feedhunt, "probe_domain", lambda d: f"https://{d}/heureka.xml")
    monkeypatch.setattr(feedhunt, "evaluate_feed", lambda url, t: (300, 2))
    monkeypatch.setattr(feedhunt.time, "sleep", lambda s: None)
    feedhunt._run(trap.id)
    src = session.scalars(
        select(FeedSource).where(FeedSource.name == "okraj.cz")
    ).one()
    assert src.enabled is False
    assert "návrh" in src.last_status


def test_run_bez_prefiltru_se_nezapina(session, monkeypatch):
    trap = _trap(session, prefilter="")
    monkeypatch.setattr(feedhunt, "derive_queries", lambda t: ["x"])
    monkeypatch.setattr(feedhunt, "search_domains", lambda q: ["hamakove.cz"])
    monkeypatch.setattr(feedhunt, "probe_domain", lambda d: f"https://{d}/heureka.xml")
    monkeypatch.setattr(feedhunt, "evaluate_feed", lambda url, t: (400, 400))
    monkeypatch.setattr(feedhunt.time, "sleep", lambda s: None)
    feedhunt._run(trap.id)
    src = session.scalars(
        select(FeedSource).where(FeedSource.name == "hamakove.cz")
    ).one()
    assert src.enabled is False               # celý sortiment bez filtru ne
    assert "předfiltr" in src.last_status


def test_run_strop_auto_zapnuti(session, monkeypatch):
    trap = _trap(session)
    monkeypatch.setattr(feedhunt, "AUTO_ENABLE_MAX_PER_RUN", 1)
    monkeypatch.setattr(feedhunt, "derive_queries", lambda t: ["x"])
    monkeypatch.setattr(feedhunt, "search_domains", lambda q: ["a.cz", "b.cz"])
    monkeypatch.setattr(feedhunt, "probe_domain", lambda d: f"https://{d}/heureka.xml")
    monkeypatch.setattr(feedhunt, "evaluate_feed", lambda url, t: (100, 50))
    monkeypatch.setattr(feedhunt.time, "sleep", lambda s: None)
    feedhunt._run(trap.id)
    enabled = [
        s.name for s in session.scalars(select(FeedSource)).all() if s.enabled
    ]
    assert enabled == ["a.cz"]                # druhý zůstal návrhem


def test_run_pending_throttle(session, monkeypatch):
    from datetime import UTC, datetime

    fresh = _trap(session)
    fresh.last_hunt = datetime.now(UTC).replace(tzinfo=None)
    stale = Criteria(name="Stará", query_terms=["y"], prefilter="p")
    vypnuta = Criteria(name="Vypnutá", query_terms=["z"], active=False)
    session.add_all([stale, vypnuta])
    session.commit()

    hunted = []
    monkeypatch.setattr(settings, "searxng_url", "http://searx:1")
    monkeypatch.setattr(settings, "hunt_hours", 24.0)
    monkeypatch.setattr(
        feedhunt, "_hunt_trap",
        lambda s, t: hunted.append(t.name) or (1, 1),
    )
    assert feedhunt.run_pending() == (1, 1)
    assert hunted == ["Stará"]                # čerstvá i vypnutá se přeskočí

    monkeypatch.setattr(settings, "hunt_hours", 0.0)
    assert feedhunt.run_pending() == (0, 0)   # vypnutý auto-hunt


def test_migrace_obsahuje_last_hunt():
    assert any(
        t == "criteria" and c == "last_hunt" for t, c, _ in db._MIGRATIONS
    )


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


def test_extract_domains_from_html():
    html = """
    <a href="/preferences">nastavení</a>
    <article><h3><a href="https://www.dobryshop.cz/autochladnicky">X</a></h3></article>
    <article><a href="https://eshop.jiny.cz/produkt/1">Y</a></article>
    <a href="https://www.dobryshop.cz/kontakt">dup</a>
    """
    assert feedhunt.extract_domains_from_html(html) == ["dobryshop.cz", "eshop.jiny.cz"]


def test_search_html_fallback_pri_403(monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://searx:1")
    calls = []

    class Resp:
        def __init__(self, code, text=""):
            self.status_code = code
            self.text = text
        def raise_for_status(self):
            if self.status_code != 200:
                raise feedhunt.httpx.HTTPStatusError(
                    "x", request=None, response=self)
        def json(self): return {}

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, params=None):
            calls.append(params)
            if params and params.get("format") == "json":
                return Resp(403)
            return Resp(200, '<a href="https://www.novy-eshop.cz/x">r</a>')

    monkeypatch.setattr(feedhunt.httpx, "Client", FakeClient)
    monkeypatch.setattr(feedhunt.time, "sleep", lambda s: None)
    assert feedhunt.search_domains(["lednička"]) == ["novy-eshop.cz"]
    assert calls[0].get("format") == "json"      # nejdřív JSON
    assert "format" not in calls[1]              # pak HTML fallback
