"""Testy discovery: parser feedu, deduplikace produktů, API zdrojů."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import db, discovery
from trapline.api.main import app
from trapline.config import settings
from trapline.crawlers import heureka_feed
from trapline.models import Base, FeedSource, Offer, PriceHistory, Product

FEED = """<?xml version="1.0" encoding="utf-8"?>
<SHOP>
  <SHOPITEM>
    <ITEM_ID>BBPF-30A</ITEM_ID>
    <PRODUCTNAME>Přenosná autochladnička BestBerg BBPF-30A / 30 l</PRODUCTNAME>
    <URL>https://shop-a.example/bbpf-30a/</URL>
    <PRICE_VAT>5699,00</PRICE_VAT>
    <EAN>8594208604938</EAN>
    <MANUFACTURER>BestBerg</MANUFACTURER>
    <CATEGORYTEXT>Heureka.cz | Sport | Chladící tašky a boxy</CATEGORYTEXT>
    <PARAM><PARAM_NAME>Objem</PARAM_NAME><VAL>30 l</VAL></PARAM>
    <PARAM><PARAM_NAME>Barva</PARAM_NAME><VAL>Černá</VAL></PARAM>
    <PARAM><PARAM_NAME>Barva</PARAM_NAME><VAL>Zelená</VAL></PARAM>
  </SHOPITEM>
  <SHOPITEM>
    <ITEM_ID>STAN-01</ITEM_ID>
    <PRODUCTNAME>Stan pro 4 osoby</PRODUCTNAME>
    <URL>https://shop-a.example/stan/</URL>
    <PRICE_VAT>2999,00</PRICE_VAT>
    <CATEGORYTEXT>Heureka.cz | Sport | Stany</CATEGORYTEXT>
  </SHOPITEM>
  <SHOPITEM>
    <PRODUCTNAME>Bez ceny — nesmí projít</PRODUCTNAME>
    <URL>https://shop-a.example/x/</URL>
    <PRICE_VAT>neni</PRICE_VAT>
  </SHOPITEM>
</SHOP>
""".encode()


# --- parser ----------------------------------------------------------------

def test_parse_feedu():
    items = heureka_feed.parse(FEED)
    assert len(items) == 2  # položka bez ceny vypadla
    it = items[0]
    assert it.ean == "8594208604938"
    assert it.price == 5699.0
    assert it.params["Objem"] == "30 l"
    assert it.params["Barva"] == ["Černá", "Zelená"]  # opakovaný PARAM → seznam


def test_filtr_kategorii():
    items = heureka_feed.parse(FEED)
    assert heureka_feed.matches_filter(items[0], "chlad, lednice")
    assert not heureka_feed.matches_filter(items[1], "chlad, lednice")
    assert heureka_feed.matches_filter(items[1], "")  # prázdný filtr pouští vše
    # diakritika nerozhoduje
    assert heureka_feed.matches_filter(items[0], "chladící")


def test_normalize():
    assert heureka_feed.normalize("Autochladnička BBPF-30A / 30 l") == (
        "autochladnicka bbpf 30a 30 l"
    )


# --- upsert / dedup --------------------------------------------------------

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


def _item(**kw) -> heureka_feed.FeedItem:
    base = dict(
        item_id="X1", name="Autochladnička BestBerg BBPF-30A", ean="8594208604938",
        url="https://a.example/x1/", price=5699.0, manufacturer="BestBerg",
        category="Chladící boxy", params={"Objem": "30 l"},
    )
    base.update(kw)
    return heureka_feed.FeedItem(**base)


def _src(session, name="Shop A", url="https://a.example/feed.xml") -> FeedSource:
    src = FeedSource(name=name, url=url, category_filter="")
    session.add(src)
    session.flush()
    return src


def test_dedup_pres_ean(session, monkeypatch):
    monkeypatch.setattr(discovery, "_fetch_items", lambda source: [_item()])
    discovery.run_source(session, _src(session))
    # stejný EAN z jiného obchodu s jiným názvem → týž produkt, druhá nabídka
    monkeypatch.setattr(
        discovery, "_fetch_items",
        lambda source: [_item(item_id="Y9", name="BestBerg BBPF-30A lednice do auta",
                              url="https://b.example/y9/")],
    )
    discovery.run_source(session, _src(session, "Shop B", "https://b.example/feed.xml"))

    assert session.scalar(select(Product).where(Product.ean == "8594208604938"))
    assert len(session.scalars(select(Product)).all()) == 1
    assert len(session.scalars(select(Offer)).all()) == 2


def test_dedup_bez_ean_pres_brand_model(session, monkeypatch):
    """Menší eshopy EAN nemají — druhý výskyt téhož názvu se nesmí založit znovu."""
    monkeypatch.setattr(discovery, "_fetch_items", lambda source: [_item(ean=None)])
    discovery.run_source(session, _src(session))
    monkeypatch.setattr(
        discovery, "_fetch_items",
        lambda source: [_item(ean=None, item_id="Z2", url="https://b.example/z2/")],
    )
    discovery.run_source(session, _src(session, "Shop B", "https://b.example/feed.xml"))
    assert len(session.scalars(select(Product)).all()) == 1


def test_opakovany_beh_pridava_jen_cenu(session, monkeypatch):
    """Druhý běh téhož zdroje: žádný nový produkt ani nabídka, jen záznam ceny
    (append-only PriceHistory, viz ADR-0002)."""
    src = _src(session)
    monkeypatch.setattr(discovery, "_fetch_items", lambda source: [_item()])
    discovery.run_source(session, src)
    monkeypatch.setattr(discovery, "_fetch_items", lambda source: [_item(price=5499.0)])
    discovery.run_source(session, src)

    assert len(session.scalars(select(Product)).all()) == 1
    assert len(session.scalars(select(Offer)).all()) == 1
    prices = [ph.price for ph in session.scalars(select(PriceHistory)).all()]
    assert prices == [5699.0, 5499.0]


def test_chyba_zdroje_se_zapise(session, monkeypatch):
    src = _src(session)

    def boom(source):
        raise ValueError("feed nedostupný")

    monkeypatch.setattr(discovery, "_fetch_items", boom)
    with pytest.raises(ValueError):
        discovery.run_source(session, src)


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


def test_sources_crud(client):
    r = client.post("/api/discovery/sources", json={
        "name": "Bestberg", "url": "https://www.bestberg.cz/heureka/export/products.xml",
        "category_filter": "chlad, lednice",
    })
    assert r.status_code == 201
    sid = r.json()["id"]

    # duplicitní URL
    assert client.post("/api/discovery/sources", json={
        "name": "Dup", "url": "https://www.bestberg.cz/heureka/export/products.xml",
    }).status_code == 409

    assert client.patch(f"/api/discovery/sources/{sid}",
                        json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/discovery/sources/{sid}").status_code == 204
    assert client.get("/api/discovery/sources").json() == []


def test_run_bez_zdroju_je_400(client):
    assert client.post("/api/discovery/run").status_code == 400


def test_products_endpoint(client, monkeypatch):
    client.post("/api/discovery/sources", json={
        "name": "Shop A", "url": "https://a.example/feed.xml",
    })
    with Session(db._engine) as s:
        src = s.scalars(select(FeedSource)).first()
        monkeypatch.setattr(discovery, "_fetch_items", lambda source: [_item()])
        discovery.run_source(s, src)

    rows = client.get("/api/discovery/products").json()
    assert len(rows) == 1
    p = rows[0]
    assert p["ean"] == "8594208604938"
    assert p["offers"] == 1
    assert p["price_min"] == 5699.0
    assert p["specs"]["Objem"] == "30 l"


def test_discovery_je_za_heslem(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "tajne")
    assert client.get("/api/discovery/products").status_code == 401
    assert client.post("/api/discovery/run").status_code == 401


def test_snapshot_se_uklada_a_promazava(session, monkeypatch, tmp_path):
    """Syrový feed se gzipne na disk (ADR-0004) a drží se jen posledních N."""
    import gzip

    monkeypatch.setattr(settings, "snapshot_dir", str(tmp_path))
    monkeypatch.setattr(heureka_feed, "fetch_raw", lambda url: FEED)
    monkeypatch.setattr(discovery, "SNAPSHOT_KEEP", 2)
    src = _src(session)

    items = discovery._fetch_items(src)
    assert len(items) == 2  # parse proběhl nad snapshotovanými daty

    snaps = list((tmp_path / str(src.id)).glob("*.xml.gz"))
    assert len(snaps) == 1
    assert gzip.decompress(snaps[0].read_bytes()) == FEED

    # retence: třetí snapshot smaže nejstarší
    for stamp in ("19990101T000000", "19990101T000001"):
        (tmp_path / str(src.id) / f"{stamp}.xml.gz").write_bytes(b"stary")
    discovery._fetch_items(src)
    snaps = sorted(p.name for p in (tmp_path / str(src.id)).glob("*.xml.gz"))
    assert len(snaps) == 2
    assert "19990101T000000.xml.gz" not in snaps


def test_vypnute_snapshoty_nic_nezapisou(session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "snapshot_dir", "")
    monkeypatch.setattr(heureka_feed, "fetch_raw", lambda url: FEED)
    discovery._fetch_items(_src(session))
    assert list(tmp_path.iterdir()) == []
