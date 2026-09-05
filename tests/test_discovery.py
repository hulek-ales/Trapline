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


def test_products_slucuje_barevne_varianty(client, monkeypatch):
    """Tři barvy téhož modelu = jeden řádek s variant_count=3."""
    with Session(db._engine) as s:
        src = _src(s)
        items = [
            _item(item_id=f"BBFR-95X{c}", ean=None,
                  name=f"Chladnička BestBerg BBFR-95X{c} / 84 l / {barva}",
                  url=f"https://a.example/95x{c.lower()}/", price=cena)
            for c, barva, cena in [("B", "černá", 5000.0), ("S", "stříbrná", 5200.0),
                                   ("W", "bílá", 5100.0)]
        ]
        monkeypatch.setattr(discovery, "_fetch_items", lambda source: items)
        discovery.run_source(s, src)

    rows = client.get("/api/discovery/products").json()
    assert len(rows) == 1
    fam = rows[0]
    assert fam["variant_count"] == 3
    assert len(fam["variant_titles"]) == 3
    assert fam["price_min"] == 5000.0
    assert fam["price_max"] == 5200.0
    assert fam["offers"] == 3
    assert fam["title"].startswith("Chladnička BestBerg BBFR-95X")


def test_products_vraci_obrazek(client, monkeypatch):
    with Session(db._engine) as s:
        src = _src(s)
        it = _item()
        it.image = "https://cdn.example/foto.jpg"
        monkeypatch.setattr(discovery, "_fetch_items", lambda source: [it])
        discovery.run_source(s, src)
    rows = client.get("/api/discovery/products").json()
    assert rows[0]["image"] == "https://cdn.example/foto.jpg"
    # interní klíče se do API specs nepropisují
    assert "_img" not in rows[0]["specs"]
    assert "_popis" not in rows[0]["specs"]


def test_system_log_endpoint(client):
    import logging

    from trapline import logbuffer
    logbuffer.install()
    logging.getLogger("trapline.test").info("testovací zpráva 42")
    body = client.get("/api/system/log?contains=42").json()
    assert any("testovací zpráva 42" in r["msg"] for r in body["items"])


def test_products_filtr_podle_pasti(client, monkeypatch):
    """Stránka pasti musí vidět své oskórované produkty, i když katalog
    mezitím naroste o stovky novějších položek."""
    from trapline.models import Criteria, CriteriaMatch

    with Session(db._engine) as s:
        src = _src(s)
        monkeypatch.setattr(discovery, "_fetch_items", lambda source: [_item()])
        discovery.run_source(s, src)
        scored = s.scalars(select(Product)).first()
        trap = Criteria(name="Lednička", query_terms=["x"])
        s.add(trap)
        s.flush()
        s.add(CriteriaMatch(
            criteria_id=trap.id, product_id=scored.id, score=80.0, relevant=True,
        ))
        # záplava novějších produktů bez skóre — vytlačí scored z okna limitu
        for i in range(5):
            s.add(Product(
                brand="Y", model=f"hamaka {i}", model_norm=f"hamaka {i}",
                title=f"Hamaka {i}",
            ))
        s.commit()
        trap_id, scored_id = trap.id, scored.id

    vsechny = client.get("/api/discovery/products?limit=3").json()
    assert all(not r["matches"] for r in vsechny)  # okno = jen nové bez skóre

    moje = client.get(
        f"/api/discovery/products?limit=3&criteria_id={trap_id}"
    ).json()
    assert [r["id"] for r in moje] == [scored_id]
    assert moje[0]["matches"][0]["criteria_id"] == trap_id


def test_model_codes():
    mc = discovery.model_codes
    assert mc("Přenosná autochladnička BestBerg BBPF-30A / 30 l") == {"30a"}
    assert mc("BestBerg BBPF-30A autochladnička do auta 30 litrů") == {"30a"}
    # čísla s jednotkou nejsou kód modelu
    assert mc("Ultralehký titanový hrnec 750ml") == frozenset()
    assert mc("Chladnička 12V 230V 30 l") == frozenset()
    assert mc("Chladnička BestBerg BBFR-95X") == {"95x"}


def test_upsert_sloucí_ruzne_nazvy_napric_obchody(client, monkeypatch):
    """Dva obchody, jiný slovosled názvu, žádný EAN — spojí značka + kód."""
    from trapline.crawlers.heureka_feed import FeedItem as FI

    a = FI(item_id="a1", name="Přenosná autochladnička BestBerg BBPF-30A / 30 l",
           url="https://a.example/p", price=5699.0, manufacturer="BestBerg")
    b = FI(item_id="b1", name="BESTBERG BBPF-30A autochladnička do auta 30 litrů",
           url="https://b.example/p", price=5490.0, manufacturer="BESTBERG")
    with Session(db._engine) as s:
        pa = discovery._upsert_product(s, a)
        s.commit()
        pb = discovery._upsert_product(s, b)
        s.commit()
        assert pa.id == pb.id                  # jeden produkt, dva obchody
        # jiný kód téže značky se nespojí
        c = FI(item_id="c1", name="BestBerg BBPF-40A autochladnička 40 l",
               url="https://c.example/p", price=5990.0, manufacturer="BestBerg")
        pc = discovery._upsert_product(s, c)
        assert pc.id != pa.id
        # bez spolehlivého kódu (jen jednotky) se nespojuje
        d = FI(item_id="d1", name="BestBerg termobox 30 l",
               url="https://d.example/p", price=990.0, manufacturer="BestBerg")
        e = FI(item_id="e1", name="BestBerg chladicí taška 30 l",
               url="https://e.example/p", price=590.0, manufacturer="BestBerg")
        pd_ = discovery._upsert_product(s, d)
        s.commit()
        pe = discovery._upsert_product(s, e)
        assert pd_.id != pe.id


def test_products_vraci_obchody_s_cenou_a_trendem(client, monkeypatch):
    """Každý produkt nese seznam nabídek: obchod, odkaz, cena a předchozí
    cena pro šipku trendu; nejlevnější první."""
    from trapline.models import Offer, PriceHistory, Source

    with Session(db._engine) as s:
        src = _src(s)
        monkeypatch.setattr(discovery, "_fetch_items", lambda source: [_item()])
        discovery.run_source(s, src)
        product = s.scalars(select(Product)).one()
        # druhá nabídka jinde, levnější, a s historií (dřív dražší)
        other = Offer(
            product_id=product.id, source=Source.JSONLD, shop="alza.cz",
            sku="A1", url="https://www.alza.cz/x-d1.htm",
        )
        s.add(other)
        s.flush()
        s.add(PriceHistory(offer_id=other.id, price=5999.0))
        s.add(PriceHistory(offer_id=other.id, price=4999.0, in_stock=False))
        s.commit()

    row = client.get("/api/discovery/products").json()[0]
    shops = row["shops"]
    assert [x["shop"] for x in shops] == ["alza.cz", "Shop A"]   # levnější první
    alza = shops[0]
    assert alza["price"] == 4999.0
    assert alza["prev_price"] == 5999.0        # podklad pro šipku dolů
    assert alza["in_stock"] is False
    assert alza["url"] == "https://www.alza.cz/x-d1.htm"
    assert alza["source"] == "jsonld"
    # feedová nabídka má jen jednu cenu → žádný trend
    assert shops[1]["prev_price"] is None
    assert row["price_min"] == 4999.0


def test_products_nabidky_bez_duplicit(client, monkeypatch):
    """Dvě položky feedu se stejnou URL (barevné varianty) = jeden řádek
    obchodu, ne dva stejné."""
    from trapline.models import Offer, PriceHistory, Source

    with Session(db._engine) as s:
        src = _src(s)
        monkeypatch.setattr(discovery, "_fetch_items", lambda source: [_item()])
        discovery.run_source(s, src)
        product = s.scalars(select(Product)).one()
        dup = Offer(
            product_id=product.id, source=Source.HEUREKA_FEED, shop="Shop A",
            sku="BBPF-30A-zelena", url=product.offers[0].url,
        )
        s.add(dup)
        s.flush()
        s.add(PriceHistory(offer_id=dup.id, price=5699.0))
        s.commit()

    shops = client.get("/api/discovery/products").json()[0]["shops"]
    assert len(shops) == 1
