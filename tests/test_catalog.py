"""Obchůzka nesmí obcházet stránky po smazaných pastech.

Produkty zůstávají v katalogu i po zrušení pasti — obnova cen je proto
filtruje přes ``catalog`` (viz jeho docstring).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import catalog, db, jsonld_watch, zbozi_watch
from trapline.crawlers import jsonld
from trapline.models import Base, Criteria, CriteriaMatch, Offer, Product, Source


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
    monkeypatch.setattr(jsonld_watch.time, "sleep", lambda s: None)
    monkeypatch.setattr(zbozi_watch.time, "sleep", lambda s: None)
    with Session(engine) as s:
        yield s


def _produkt(session, title, source=Source.JSONLD, url="https://e.cz/p"):
    product = Product(
        brand="X", model=title, model_norm=title.lower(), title=title,
    )
    session.add(product)
    session.flush()
    session.add(Offer(
        product_id=product.id, source=source, shop="e.cz",
        url=url, sku=title, active=True,
    ))
    session.commit()
    return product


def _past(session, name, active=True, product=None):
    trap = Criteria(name=name, query_terms=["x"], prefilter="x", active=active)
    session.add(trap)
    session.flush()
    if product is not None:
        session.add(CriteriaMatch(
            criteria_id=trap.id, product_id=product.id, score=80.0, relevant=True,
        ))
    session.commit()
    return trap


def test_hlidane_jen_produkty_aktivnich_pasti(session):
    chteny = _produkt(session, "Hlídaný")
    vypnuty = _produkt(session, "Po vypnuté pasti")
    _sirotek = _produkt(session, "Po smazané pasti")  # bez CriteriaMatch
    _past(session, "Aktivní", product=chteny)
    _past(session, "Vypnutá", active=False, product=vypnuty)

    assert catalog.watched_product_ids(session) == {chteny.id}


def test_watched_offers_spocita_sirotky(session):
    chteny = _produkt(session, "Hlídaný")
    _produkt(session, "Sirotek A")
    _produkt(session, "Sirotek B")
    _past(session, "Aktivní", product=chteny)

    offers = list(session.scalars(select(Offer)))
    assert len(offers) == 3

    keep, orphans = catalog.watched_offers(session, offers)
    assert [o.product_id for o in keep] == [chteny.id]
    assert orphans == 2


def test_jsonld_obchazi_jen_hlidane(session, monkeypatch):
    """Jádro stížnosti: po smazání pasti se její stránky přestanou stahovat."""
    chteny = _produkt(session, "Chladnička", url="https://e.cz/chladnicka")
    _produkt(session, "Příbory", url="https://e.cz/pribory")
    _past(session, "Camping lednička", product=chteny)

    stazene: list[str] = []

    def _snapshot(url):
        stazene.append(url)
        return jsonld.JsonLdProduct(name="X", price=100.0, in_stock=True), "http"

    monkeypatch.setattr(jsonld_watch, "snapshot_price", _snapshot)
    assert jsonld_watch.refresh_all() == 1
    assert stazene == ["https://e.cz/chladnicka"]


def test_zbozi_obchazi_jen_hlidane(session, monkeypatch):
    chteny = _produkt(session, "Chladnička", source=Source.ZBOZI,
                      url="https://zbozi.cz/vyrobek/1")
    _produkt(session, "Příbory", source=Source.ZBOZI,
             url="https://zbozi.cz/vyrobek/2")
    _past(session, "Camping lednička", product=chteny)

    volane: list[str] = []

    def _refresh(session_, offer):
        volane.append(offer.url)
        return zbozi_watch.zbozi.ZboziDetail(
            name="X", slug="x", min_price=100.0, max_price=None,
            median_price=None, offers_count=2, shop_count=2,
            cheapest_shop="e.cz", released=None,
        )

    monkeypatch.setattr(zbozi_watch, "refresh_offer", _refresh)
    assert zbozi_watch.refresh_all() == 1
    assert volane == ["https://zbozi.cz/vyrobek/1"]


def test_bez_hlidanych_se_nesaha_na_sit(session, monkeypatch):
    _produkt(session, "Sirotek")

    def _boom(url):
        raise AssertionError("nikdo to nehlídá, nemá se stahovat")

    monkeypatch.setattr(jsonld_watch, "snapshot_price", _boom)
    assert jsonld_watch.refresh_all() == 0


def test_nova_past_hlidani_obnovi(session, monkeypatch):
    """Založíš past znovu → skóring dá match → stránka se hlídá zas."""
    produkt = _produkt(session, "Příbory", url="https://e.cz/pribory")
    assert catalog.watched_product_ids(session) == set()

    _past(session, "Příbory znovu", product=produkt)
    assert catalog.watched_product_ids(session) == {produkt.id}
