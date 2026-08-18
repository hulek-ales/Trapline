"""Testy hlídání produktových stránek eshopů (source=jsonld)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import db, jsonld_watch, transport
from trapline.api.main import app
from trapline.config import settings
from trapline.models import Base, Offer, PriceHistory, Product, Source


def _product_html(price=5490, name="Lednička X 40 l", availability="InStock"):
    return f"""<html><head><script type="application/ld+json">{{
      "@context": "https://schema.org", "@type": "Product",
      "name": "{name}", "gtin13": "8591234567890",
      "offers": {{"@type": "Offer", "price": {price}, "priceCurrency": "CZK",
                 "availability": "https://schema.org/{availability}"}}
    }}</script></head></html>"""


def _fake_fetch(html, via="http"):
    return lambda url, prefer_browser=False: transport.Page(url, url, 200, html, via)


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


def _product(title="Autochladnička X"):
    with Session(db._engine) as s:
        p = Product(brand="X", model=title, model_norm=title.lower(), title=title)
        s.add(p)
        s.commit()
        return p.id


def test_pripnuti_stranky(client, monkeypatch):
    monkeypatch.setattr(jsonld_watch.transport, "fetch", _fake_fetch(_product_html()))
    pid = _product()
    r = client.put(f"/api/products/{pid}/watch",
                   json={"url": "https://www.alza.cz/lednicka-x-d123.htm"})
    assert r.status_code == 200
    body = r.json()
    assert body["shop"] == "alza.cz"
    assert body["price"] == 5490.0
    assert body["in_stock"] is True
    assert body["via"] == "http"

    with Session(db._engine) as s:
        offer = s.scalars(select(Offer)).one()
        assert offer.source == Source.JSONLD
        assert offer.shop == "alza.cz"
        assert s.query(PriceHistory).count() == 1
        assert s.get(Product, pid).ean == "8591234567890"  # doplněno ze stránky

    # stejná URL podruhé → přepis, ne duplikát; jiná URL → druhá nabídka
    assert client.put(f"/api/products/{pid}/watch",
                      json={"url": "https://www.alza.cz/lednicka-x-d123.htm"}
                      ).status_code == 200
    assert client.put(f"/api/products/{pid}/watch",
                      json={"url": "https://www.datart.cz/lednicka-x.html"}
                      ).status_code == 200
    with Session(db._engine) as s:
        assert s.query(Offer).count() == 2
        assert s.query(PriceHistory).count() == 3


def test_pripnuti_spatne_url(client):
    pid = _product()
    assert client.put(f"/api/products/{pid}/watch",
                      json={"url": "alza.cz/x"}).status_code == 400


def test_pripnuti_stranka_bez_jsonld(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "snapshot_dir", str(tmp_path))
    monkeypatch.setattr(
        jsonld_watch.transport, "fetch", _fake_fetch("<html>žádná data</html>")
    )
    pid = _product()
    r = client.put(f"/api/products/{pid}/watch",
                   json={"url": "https://www.alza.cz/x.htm"})
    assert r.status_code == 422
    # tiché selhání se uložilo k inspekci (ADR-0007)
    assert list((tmp_path / "failures").glob("*.html.gz"))


def test_pripnuti_blokovana_stranka(client, monkeypatch):
    def _blocked(url, prefer_browser=False):
        raise transport.TransportError("blokace (HTTP 403), browser vypnutý")

    monkeypatch.setattr(jsonld_watch.transport, "fetch", _blocked)
    pid = _product()
    r = client.put(f"/api/products/{pid}/watch",
                   json={"url": "https://www.alza.cz/x.htm"})
    assert r.status_code == 502
    assert "browser vypnutý" in r.json()["detail"]


def test_odepnuti(client, monkeypatch):
    monkeypatch.setattr(jsonld_watch.transport, "fetch", _fake_fetch(_product_html()))
    pid = _product()
    client.put(f"/api/products/{pid}/watch",
               json={"url": "https://www.alza.cz/x.htm"})
    assert client.delete(f"/api/products/{pid}/watch").status_code == 204
    with Session(db._engine) as s:
        assert s.scalars(select(Offer)).one().active is False
    assert client.delete(f"/api/products/{pid}/watch").status_code == 404


def test_refresh_all(client, monkeypatch):
    monkeypatch.setattr(jsonld_watch.transport, "fetch", _fake_fetch(_product_html()))
    monkeypatch.setattr(jsonld_watch.time, "sleep", lambda s: None)
    pid = _product()
    client.put(f"/api/products/{pid}/watch",
               json={"url": "https://www.alza.cz/x.htm"})
    monkeypatch.setattr(
        jsonld_watch.transport, "fetch",
        _fake_fetch(_product_html(price=4990, availability="OutOfStock")),
    )
    assert jsonld_watch.refresh_all() == 1
    with Session(db._engine) as s:
        rows = s.scalars(select(PriceHistory)).all()
        assert [r.price for r in rows] == [5490.0, 4990.0]
        assert rows[-1].in_stock is False


def test_refresh_preskoci_odepnute(client, monkeypatch):
    monkeypatch.setattr(jsonld_watch.transport, "fetch", _fake_fetch(_product_html()))
    monkeypatch.setattr(jsonld_watch.time, "sleep", lambda s: None)
    pid = _product()
    client.put(f"/api/products/{pid}/watch",
               json={"url": "https://www.alza.cz/x.htm"})
    client.delete(f"/api/products/{pid}/watch")
    assert jsonld_watch.refresh_all() == 0


def test_watch_za_heslem(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "tajne")
    assert client.put("/api/products/1/watch",
                      json={"url": "https://x.cz/"}).status_code == 401


def test_products_hlasi_has_watch(client, monkeypatch):
    monkeypatch.setattr(jsonld_watch.transport, "fetch", _fake_fetch(_product_html()))
    pid = _product()
    client.put(f"/api/products/{pid}/watch",
               json={"url": "https://www.alza.cz/x.htm"})
    rows = client.get("/api/discovery/products").json()
    assert rows[0]["has_watch"] is True
    assert rows[0]["has_zbozi"] is False


def test_browser_endpoint(client, monkeypatch):
    monkeypatch.setattr(settings, "browser_url", "")
    assert client.get("/api/system/browser").json() == {
        "configured": False, "reachable": False,
    }
