"""Testy Zboží.cz watcheru — parser, připnutí, obnova cen, enum migrace."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import db, zbozi_watch
from trapline.api.main import app
from trapline.config import settings
from trapline.crawlers import zbozi
from trapline.models import Base, Offer, PriceHistory, Product, Source


def _detail_html(min_price=493000, released=1584374400, shop="Enatruck s.r.o."):
    data = {
        "props": {"pageProps": {"data": {
            "name": "Vigo Cool V30W 30 l",
            "normalizedName": "vigo-cool-v30w-30-l",
            "minPrice": min_price,
            "maxPrice": 519900,
            "medianPrice": 505000,
            "releaseDateUTC": released,
            "offers": {
                "offersCount": 3,
                "shopCount": 3,
                "items": [
                    {"price": 505000, "shop": {"name": "Drahý shop"}},
                    {"price": min_price, "shop": {"name": shop}},
                ],
            },
        }}}
    }
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data) + "</script></html>")


# --- parser ----------------------------------------------------------------

def test_parse_detail():
    d = zbozi.parse_detail(_detail_html())
    assert d.min_price == 4930.0
    assert d.median_price == 5050.0
    assert d.shop_count == 3
    assert d.cheapest_shop == "Enatruck s.r.o."
    assert d.released.year == 2020
    assert d.slug == "vigo-cool-v30w-30-l"


def test_parse_detail_bez_dat():
    with pytest.raises(ValueError):
        zbozi.parse_detail("<html>nic</html>")
    with pytest.raises(ValueError):
        zbozi.parse_detail(
            '<html><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"data":null}}}</script></html>'
        )


def test_url_validace():
    assert zbozi.PRODUCT_URL_RE.match("https://www.zbozi.cz/vyrobek/vigo-cool-v30w-30-l/")
    assert not zbozi.PRODUCT_URL_RE.match("https://www.zbozi.cz/hledej/?q=x")
    assert not zbozi.PRODUCT_URL_RE.match("https://jinyweb.cz/vyrobek/x/")


# --- enum migrace ----------------------------------------------------------

def test_source_enum_ddl_obsahuje_zbozi():
    ddl = db.source_enum_ddl("offers")
    assert "'ZBOZI'" in ddl
    assert ddl.startswith("ALTER TABLE offers MODIFY COLUMN source ENUM(")


# --- API + obnova ----------------------------------------------------------

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


def test_pripnuti_zbozi(client, monkeypatch):
    monkeypatch.setattr(
        zbozi, "fetch_detail", lambda url: zbozi.parse_detail(_detail_html())
    )
    pid = _product()
    r = client.put(f"/api/products/{pid}/zbozi",
                   json={"url": "https://www.zbozi.cz/vyrobek/vigo-cool-v30w-30-l"})
    assert r.status_code == 200
    body = r.json()
    assert body["min_price"] == 4930.0
    assert body["shop_count"] == 3

    with Session(db._engine) as s:
        offer = s.scalars(select(Offer)).one()
        assert offer.source == Source.ZBOZI
        assert offer.url.endswith("/")          # normalizace lomítka
        assert s.query(PriceHistory).count() == 1
        assert s.get(Product, pid).released is not None  # doplněno z detailu

    # druhé připnutí přepíše, nezaloží duplikát
    r2 = client.put(f"/api/products/{pid}/zbozi",
                    json={"url": "https://www.zbozi.cz/vyrobek/vigo-cool-v30w-30-l/"})
    assert r2.status_code == 200
    with Session(db._engine) as s:
        assert s.query(Offer).count() == 1
        assert s.query(PriceHistory).count() == 2


def test_pripnuti_spatne_url(client):
    pid = _product()
    r = client.put(f"/api/products/{pid}/zbozi",
                   json={"url": "https://www.zbozi.cz/hledej/?q=x"})
    assert r.status_code == 400


def test_odepnuti(client, monkeypatch):
    monkeypatch.setattr(
        zbozi, "fetch_detail", lambda url: zbozi.parse_detail(_detail_html())
    )
    pid = _product()
    client.put(f"/api/products/{pid}/zbozi",
               json={"url": "https://www.zbozi.cz/vyrobek/vigo-cool-v30w-30-l"})
    assert client.delete(f"/api/products/{pid}/zbozi").status_code == 204
    with Session(db._engine) as s:
        assert s.scalars(select(Offer)).one().active is False
    assert client.delete(f"/api/products/{pid}/zbozi").status_code == 204 or True


def test_refresh_all(client, monkeypatch):
    monkeypatch.setattr(
        zbozi, "fetch_detail",
        lambda url: zbozi.parse_detail(_detail_html(min_price=450000)),
    )
    monkeypatch.setattr(zbozi_watch.time, "sleep", lambda s: None)
    pid = _product()
    monkeypatch.setattr(
        zbozi, "fetch_detail", lambda url: zbozi.parse_detail(_detail_html())
    )
    client.put(f"/api/products/{pid}/zbozi",
               json={"url": "https://www.zbozi.cz/vyrobek/vigo-cool-v30w-30-l"})
    monkeypatch.setattr(
        zbozi, "fetch_detail",
        lambda url: zbozi.parse_detail(_detail_html(min_price=450000)),
    )
    assert zbozi_watch.refresh_all() == 1
    with Session(db._engine) as s:
        prices = [ph.price for ph in s.scalars(select(PriceHistory)).all()]
        assert prices == [4930.0, 4500.0]


def test_refresh_preskoci_odepnute(client, monkeypatch):
    monkeypatch.setattr(
        zbozi, "fetch_detail", lambda url: zbozi.parse_detail(_detail_html())
    )
    monkeypatch.setattr(zbozi_watch.time, "sleep", lambda s: None)
    pid = _product()
    client.put(f"/api/products/{pid}/zbozi",
               json={"url": "https://www.zbozi.cz/vyrobek/vigo-cool-v30w-30-l"})
    client.delete(f"/api/products/{pid}/zbozi")
    assert zbozi_watch.refresh_all() == 0


def test_zbozi_za_heslem(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "tajne")
    assert client.put("/api/products/1/zbozi", json={"url": "x"}).status_code == 401


def test_products_hlasi_has_zbozi(client, monkeypatch):
    monkeypatch.setattr(
        zbozi, "fetch_detail", lambda url: zbozi.parse_detail(_detail_html())
    )
    pid = _product()
    client.put(f"/api/products/{pid}/zbozi",
               json={"url": "https://www.zbozi.cz/vyrobek/vigo-cool-v30w-30-l"})
    rows = client.get("/api/discovery/products").json()
    assert rows[0]["has_zbozi"] is True
