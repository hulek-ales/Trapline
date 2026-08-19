"""Testy crawleru produktových stránek (ADR-0007)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import db, pagehunt, transport
from trapline.crawlers import jsonld
from trapline.models import Base, Criteria, Offer, PriceHistory, Product, Source


def _product_page(name="Autochladnička Vevor 40 l", price=8999, currency="CZK",
                  brand="Vevor", ean="8591112223334",
                  description="Kompresorová chladnička do auta."):
    return f"""<html><head><script type="application/ld+json">{{
      "@type": "Product", "name": "{name}",
      "brand": {{"@type": "Brand", "name": "{brand}"}},
      "gtin13": "{ean}", "description": "{description}",
      "offers": {{"price": {price}, "priceCurrency": "{currency}",
                 "availability": "https://schema.org/InStock"}}
    }}</script></head></html>"""


def _category_page(urls):
    items = ",".join(
        f'{{"@type": "ListItem", "position": {i+1}, "url": "{u}"}}'
        for i, u in enumerate(urls)
    )
    return ('<html><script type="application/ld+json">'
            f'{{"@type": "ItemList", "itemListElement": [{items}]}}'
            "</script></html>")


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
    monkeypatch.setattr(pagehunt, "allowed_by_robots", lambda url: True)
    monkeypatch.setattr(pagehunt.time, "sleep", lambda s: None)
    with Session(engine) as s:
        yield s


def _trap(session, prefilter="chladni, lednic") -> Criteria:
    trap = Criteria(name="Lednička", query_terms=["12V"], prefilter=prefilter)
    session.add(trap)
    session.commit()
    return trap


def _fetch_map(pages: dict):
    def _fetch(url, prefer_browser=False):
        if url not in pages:
            raise transport.TransportError("mimo mapu")
        return transport.Page(url, url, 200, pages[url], "http")
    return _fetch


def test_produkt_z_vysledku_hledani(session, monkeypatch):
    trap = _trap(session)
    pages = {
        "https://www.alza.cz/vevor-d1.htm": _product_page(),
        "https://eshop.example/mimo": "<html>nic</html>",
    }
    monkeypatch.setattr(pagehunt.transport, "fetch", _fetch_map(pages))
    checked, found = pagehunt.hunt_trap(session, trap, list(pages))
    assert (checked, found) == (2, 1)

    product = session.scalars(select(Product)).one()
    assert product.brand == "Vevor"
    assert product.ean == "8591112223334"
    offer = session.scalars(select(Offer)).one()
    assert offer.source == Source.JSONLD
    assert offer.shop == "alza.cz"
    assert session.query(PriceHistory).count() == 1

    # druhý běh: URL už je známá nabídka → žádný nový fetch ani duplikát
    checked2, found2 = pagehunt.hunt_trap(
        session, trap, ["https://www.alza.cz/vevor-d1.htm"]
    )
    assert (checked2, found2) == (0, 0)
    assert session.query(Offer).count() == 1


def test_kategorie_itemlist_rozbali(session, monkeypatch):
    trap = _trap(session)
    detail = "https://shop.example/lednicka-x"
    pages = {
        "https://shop.example/kategorie": _category_page(
            [detail, "https://jinde.example/cizi"]
        ),
        detail: _product_page(name="Lednička X 30 l"),
    }
    monkeypatch.setattr(pagehunt.transport, "fetch", _fetch_map(pages))
    checked, found = pagehunt.hunt_trap(
        session, trap, ["https://shop.example/kategorie"]
    )
    assert found == 1
    assert session.scalars(select(Product)).one().title.startswith("Lednička X")


def test_prefiltr_a_mena_vyrazuji(session, monkeypatch):
    trap = _trap(session)
    pages = {
        "https://a.example/stan": _product_page(
            name="Stan pro 4 osoby", description="Rodinný stan s předsíní."
        ),
        "https://b.example/eur": _product_page(currency="EUR"),
    }
    monkeypatch.setattr(pagehunt.transport, "fetch", _fetch_map(pages))
    checked, found = pagehunt.hunt_trap(session, trap, list(pages))
    assert (checked, found) == (2, 0)
    assert session.query(Product).count() == 0


def test_strop_na_domenu(session, monkeypatch):
    trap = _trap(session)
    monkeypatch.setattr(pagehunt, "MAX_PER_DOMAIN", 1)
    pages = {
        f"https://moc.example/p{i}": _product_page(name=f"Lednička {i}")
        for i in range(3)
    }
    monkeypatch.setattr(pagehunt.transport, "fetch", _fetch_map(pages))
    checked, _found = pagehunt.hunt_trap(session, trap, list(pages))
    assert checked == 1


def test_blacklist_a_robots(session, monkeypatch):
    trap = _trap(session)
    monkeypatch.setattr(pagehunt.transport, "fetch", _fetch_map({}))
    checked, _ = pagehunt.hunt_trap(
        session, trap, ["https://www.heureka.cz/x", "https://bazos.cz/y"]
    )
    assert checked == 0                        # blacklist se vůbec nestahuje

    monkeypatch.setattr(pagehunt, "allowed_by_robots", lambda url: False)
    checked, _ = pagehunt.hunt_trap(
        session, trap, ["https://slusny.example/zakazano"]
    )
    assert checked == 0                        # robots.txt se respektuje


def test_bez_prefiltru_se_necrawluje(session, monkeypatch):
    trap = _trap(session, prefilter="")
    monkeypatch.setattr(
        pagehunt.transport, "fetch",
        lambda url, prefer_browser=False: pytest.fail("nemá se stahovat"),
    )
    assert pagehunt.hunt_trap(session, trap, ["https://x.example/a"]) == (0, 0)


def test_itemlist_parser():
    html = _category_page(["https://s.example/a", "https://s.example/b"])
    assert jsonld.item_urls(html) == ["https://s.example/a", "https://s.example/b"]
    # varianta s vnořeným item objektem
    html2 = ('<html><script type="application/ld+json">'
             '{"@type": "ItemList", "itemListElement": ['
             '{"@type": "ListItem", "item": {"@id": "https://s.example/c"}}]}'
             "</script></html>")
    assert jsonld.item_urls(html2) == ["https://s.example/c"]
    assert jsonld.item_urls("<html>nic</html>") == []


def test_kategorie_s_vlastnim_productem_se_neuklada(session, monkeypatch):
    """Stránka kategorie nese obecný Product („Hamaky — od 120 Kč") i větší
    ItemList — ukládat se mají jen detaily, ne souhrn."""
    trap = _trap(session, prefilter="hamak")
    details = [f"https://shop.example/hamaka-{i}" for i in range(3)]
    category = ('<html><script type="application/ld+json">'
                '{"@type": "Product", "name": "Hamaky",'
                ' "offers": {"price": 120, "priceCurrency": "CZK"}}'
                "</script>" + _category_page(details)[6:])
    pages = {"https://shop.example/kategorie": category}
    pages.update({u: _product_page(name=f"Hamaka {i}", ean=f"859000000000{i}",
                                   description="Turistická hamaka.")
                  for i, u in enumerate(details)})
    monkeypatch.setattr(pagehunt.transport, "fetch", _fetch_map(pages))
    _checked, found = pagehunt.hunt_trap(
        session, trap, ["https://shop.example/kategorie"]
    )
    assert found == 3
    titles = {p.title for p in session.scalars(select(Product))}
    assert "Hamaky" not in titles              # souhrn kategorie se neukládá


def test_product_bez_znacky_a_ean_se_neuklada(session, monkeypatch):
    """Souhrnný Product kategorie bez značky/EAN/SKU (4camping
    „Kompresorové chladničky — od 4229 Kč") se neukládá."""
    trap = _trap(session, prefilter="chladni")
    html = ('<html><script type="application/ld+json">'
            '{"@type": "Product", "name": "Kompresorové chladničky",'
            ' "offers": {"price": 4229, "priceCurrency": "CZK"}}'
            "</script></html>")
    monkeypatch.setattr(
        pagehunt.transport, "fetch",
        _fetch_map({"https://www.4camping.cz/c/kompresorove-chladnicky/": html}),
    )
    checked, found = pagehunt.hunt_trap(
        session, trap, ["https://www.4camping.cz/c/kompresorove-chladnicky/"]
    )
    assert (checked, found) == (1, 0)
    assert session.query(Product).count() == 0


def test_detail_links_a_next_page():
    html = '''<html><head>
      <link href="/sport/levne-houpaci-site/18856259-p2.htm" rel="next">
      </head><body>
      <a href="/sport/houpaci-sit-canvas-d7712711.htm">Canvas</a>
      <a href="https://www.alza.cz/sport/acra-houpaci-sit-d5544332.htm?o=1">Acra</a>
      <a href="https://www.alza.cz/sport/houpaci-sit-canvas-d7712711.htm">dup</a>
      <a href="https://jinyweb.cz/produkt/cizi-d123.htm">cizí doména</a>
      <a href="/kosik">košík</a>
      </body></html>'''
    base = "https://www.alza.cz/sport/levne-houpaci-site/18856259.htm"
    links = pagehunt.detail_links(html, base)
    assert links == [
        "https://www.alza.cz/sport/houpaci-sit-canvas-d7712711.htm",
        "https://www.alza.cz/sport/acra-houpaci-sit-d5544332.htm?o=1",
    ]
    assert pagehunt.next_page(html, base) == (
        "https://www.alza.cz/sport/levne-houpaci-site/18856259-p2.htm"
    )
    assert pagehunt.next_page("<html>bez next</html>", base) is None


def test_alza_kategorie_pres_html_odkazy(session, monkeypatch):
    """Kategorie bez JSON-LD ItemList (Alza) → detaily z HTML odkazů,
    pokračování přes rel=next."""
    trap = _trap(session, prefilter="houpací síť, hamak")
    cat1 = "https://www.alza.cz/sport/levne-houpaci-site/18856259.htm"
    cat2 = "https://www.alza.cz/sport/levne-houpaci-site/18856259-p2.htm"
    d1 = "https://www.alza.cz/sport/sit-canvas-d1.htm"
    d2 = "https://www.alza.cz/sport/sit-acra-d2.htm"
    d3 = "https://www.alza.cz/sport/sit-sedco-d3.htm"
    pages = {
        cat1: (f'<html><head><link rel="next" href="{cat2}"></head>'
               f'<a href="{d1}">x</a><a href="{d2}">y</a></html>'),
        cat2: f'<html><a href="{d3}">z</a></html>',
        d1: _product_page(name="Houpací síť Canvas 200x80", brand="Canvas",
                          ean="8590000000011", description="Houpací síť."),
        d2: _product_page(name="Acra houpací síť J02", brand="Acra",
                          ean="8590000000012", description="Houpací síť."),
        d3: _product_page(name="Sedco houpací síť 200x80", brand="Sedco",
                          ean="8590000000013", description="Houpací síť."),
    }
    monkeypatch.setattr(pagehunt.transport, "fetch", _fetch_map(pages))
    checked, found = pagehunt.hunt_trap(session, trap, [cat1])
    assert found == 3
    assert checked == 5                        # 2 kategorie + 3 detaily
    shops = {o.shop for o in session.scalars(select(Offer))}
    assert shops == {"alza.cz"}
