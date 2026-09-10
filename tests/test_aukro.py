"""Testy Aukro konektoru (ADR-0008): stav stránky, aukce vs. kup teď,
detail a signál „žije ještě?"."""

from __future__ import annotations

import json

import httpx
import pytest

from trapline import bazar, fx
from trapline.crawlers import aukro
from trapline.models import Source

# Zkrácený reálný stav ze stránky výsledků (9. 9. 2026): aukce od Alzy
# bez aktivního „kup teď" a nabídka „kup teď" ze Slovenska.
_ITEMS = [
    {
        "itemId": 7132769171,
        "itemName": "Autochladnička VayKold autochladnička 27l",
        "seoUrl": "autochladnicka-vaykold-autochladnicka-27l",
        "price": {"amount": 999, "currency": "CZK"},
        "buyNowPrice": {"amount": 1909, "currency": "CZK"},
        "buyNowActive": False,
        "auction": True,
        "endingTime": "2026-09-12T19:45:39+02:00",
        "location": "Pobočky Alza.cz - prodejny",
        "postcode": "17000",
        "freeShipping": False,
        "personalPickup": True,
        "itemState": "ACTIVE",
        "attributes": [
            {"attributeName": "Stav zboží", "attributeValue": "Poškozeno"},
            {"attributeName": "Kód zboží", "attributeValue": "AUP297024"},
        ],
        "sellerLogin": "Alza_prodej",
        "priceWithShipping": {"amount": 1398, "currency": "CZK"},
        "categoryPath": [
            {"name": "Elektro"}, {"name": "Lednice"}, {"name": "Cestovní chlazení"},
        ],
    },
    {
        "itemId": 7122798901,
        "itemName": "Autochladnička 5L",
        "seoUrl": "autochladnicka-5l",
        "price": {"amount": 1235, "currency": "CZK"},
        "buyNowPrice": {"amount": 1235, "currency": "CZK"},
        "buyNowActive": True,
        "auction": False,
        "endingTime": "2026-10-06T17:14:27+02:00",
        "location": "Humenné",
        "postcode": "06601",
        "freeShipping": False,
        "personalPickup": True,
        "attributes": [{"attributeName": "Stav zboží", "attributeValue": "Nové"}],
        "sellerLogin": "Miroslavsima",
        "priceWithShipping": {"amount": 1334, "currency": "CZK"},
        "categoryPath": [{"name": "Elektro"}],
    },
    {"itemName": "bez id — přeskočit"},
]

_KEY = (
    "POST\x1c/backend-web/api/offers/searchItemsCommon\x1cpage=0&size=60"
    '\x1c{"text":"autochladnicka"}'
)


def _page(items=_ITEMS, escape: bool = False) -> str:
    state = json.dumps(
        {"aukCache": {_KEY: {"t": 200, "b": {"content": items}}}},
        ensure_ascii=False,
    )
    if escape:  # krátké entity Angular TransferState
        state = state.replace("&", "&a;").replace('"', "&q;")
    return (
        "<html><head></head><body>"
        f'<script id="ng-state" type="application/json">{state}</script>'
        "</body></html>"
    )


def _ld_page(availability: str, description: str = "Popis prodejce.") -> str:
    ld = json.dumps({
        "@type": "Product", "name": "x", "description": description,
        "offers": {"@type": "Offer", "price": 49, "availability": availability},
    })
    return (
        '<html><head><script type="application/ld+json" id="jsonld-PRODUCT">'
        f"{ld}</script></head><body>Ukončeno v pondělí (text rozhraní)</body></html>"
    )


@pytest.fixture(autouse=True)
def _kurz(monkeypatch):
    monkeypatch.setattr(fx, "_rates", {"CZK": 1.0})
    monkeypatch.setattr(fx, "_rates_day", __import__("datetime").date.today())


# --- výpis ------------------------------------------------------------------

def test_parse_search_aukce_a_kup_ted():
    ads = aukro.parse_search(_page())
    assert [a.ext_id for a in ads] == ["7132769171", "7122798901"]

    aukce = ads[0]
    assert aukce.auction is True
    assert aukce.title.startswith("Aukce: Autochladnička VayKold")
    assert aukce.price == 999            # aktuální příhoz, ne „kup teď"
    assert aukce.shipping == 399         # 1398 − 999
    assert aukce.url == (
        "https://aukro.cz/autochladnicka-vaykold-autochladnicka-27l-7132769171"
    )
    assert "Stav zboží: Poškozeno" in aukce.description
    assert "Kategorie: Elektro > Lednice > Cestovní chlazení" in aukce.description
    assert "končí 12. 9. 19:45" in aukce.description
    assert "prodejce Alza_prodej" in aukce.description
    assert aukce.locality == "Pobočky Alza.cz - prodejny, 17000"

    kup = ads[1]
    assert kup.auction is False
    assert not kup.title.startswith("Aukce")
    assert kup.price == 1235
    assert kup.shipping == 99
    assert "Stav zboží: Nové" in kup.description


def test_parse_search_kup_ted_ma_prednost_pred_prihozem():
    item = dict(_ITEMS[0], buyNowActive=True)
    ad = aukro.parse_search(_page([item]))[0]
    assert ad.price == 1909
    assert ad.auction is False
    assert "kup teď 1909 Kč" not in ad.description  # není aukce, nic o ní
    assert ad.title.startswith("Autochladnička")


def test_parse_search_kratke_entity_angularu():
    assert len(aukro.parse_search(_page(escape=True))) == 2


def test_parse_search_bez_stavu_nebo_bez_vysledku():
    assert aukro.parse_search("<html>nic</html>") == []
    prazdny = json.dumps({"aukCache": {"jine": {"t": 200, "b": {}}}})
    assert aukro.parse_search(
        f'<script id="ng-state" type="application/json">{prazdny}</script>'
    ) == []


# --- detail -----------------------------------------------------------------

def test_parse_detail_zije_vs_skoncila():
    text, alive = aukro.parse_detail(_ld_page("https://schema.org/InStock"))
    assert alive is True and text == "Popis prodejce."
    text, alive = aukro.parse_detail(
        _ld_page("http://schema.org/Discontinued", "Vazba: Brožovaná")
    )
    assert alive is False and text == "Vazba: Brožovaná"


def test_parse_detail_bez_jsonld_necha_zit():
    """Slovo „Ukončeno" je i na aktivní stránce — bez JSON-LD se nehádá."""
    assert aukro.parse_detail("<html>Ukončeno v pondělí</html>") == (None, True)


def test_detail_404_je_pryc(monkeypatch):
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw):
            return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(aukro, "_client", lambda: _C())
    assert aukro.detail("https://aukro.cz/x-1") == (None, False)


# --- zapojení do pipeline ----------------------------------------------------

def test_pipeline_bere_aukro_kandidaty(monkeypatch):
    from trapline.models import Criteria

    monkeypatch.setattr(bazar.time, "sleep", lambda s: None)
    monkeypatch.setattr(bazar, "pick_sections", lambda trap: [])
    monkeypatch.setattr(bazar.sbazar, "search", lambda phrase, limit=40: [])
    monkeypatch.setattr(bazar.settings, "allegro_client_id", "")  # bez Allegra
    monkeypatch.setattr(
        aukro, "search", lambda phrase: aukro.parse_search(_page())
    )
    trap = Criteria(name="Chladnička", query_terms=["12V"], prefilter="autochladnička")
    cands = bazar._candidates(trap)
    assert {src for src, _ad in cands} == {Source.AUKRO}
    assert len(cands) == 2


def test_pipeline_popis_aukra_se_pridava(monkeypatch):
    from trapline.models import Listing

    monkeypatch.setattr(
        aukro, "detail", lambda url: ("Prodávám, málo používaná.", True)
    )
    listing = Listing(
        source=Source.AUKRO, ext_id="1", url="https://aukro.cz/x-1",
        title="x", description="Stav zboží: Použité · Kategorie: Elektro",
        price=999.0,
    )
    bazar._full_description(Source.AUKRO, listing)
    assert listing.description.startswith("Stav zboží: Použité")
    assert listing.description.endswith("Prodávám, málo používaná.")
