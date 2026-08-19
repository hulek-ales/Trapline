"""Testy transportní vrstvy (ADR-0006) a JSON-LD extraktoru."""

from __future__ import annotations

import httpx
import pytest

from trapline import transport
from trapline.config import settings
from trapline.crawlers import jsonld

# --- detekce blokace -------------------------------------------------------

def test_blokace_podle_stavu():
    assert transport.looks_blocked(403, "")
    assert transport.looks_blocked(429, "")
    assert not transport.looks_blocked(200, "<html>ok</html>")
    assert not transport.looks_blocked(404, "<html>not found</html>")


def test_blokace_podle_tela():
    assert transport.looks_blocked(200, "<title>Just a moment...</title>")
    assert transport.looks_blocked(200, "vyplňte CAPTCHA prosím")
    assert not transport.looks_blocked(200, "<h1>Lednička</h1>")


# --- žebřík http → browser -------------------------------------------------

def test_fetch_bez_browseru_hlasi_blokaci(monkeypatch):
    monkeypatch.setattr(settings, "browser_url", "")
    monkeypatch.setattr(
        transport, "fetch_http",
        lambda url, timeout=30.0: transport.Page(url, url, 403, "denied", "http"),
    )
    with pytest.raises(transport.TransportError, match="browser vypnutý"):
        transport.fetch("https://eshop.example/p")


def test_fetch_eskaluje_na_browser(monkeypatch):
    monkeypatch.setattr(settings, "browser_url", "http://browser:3000")
    monkeypatch.setattr(
        transport, "fetch_http",
        lambda url, timeout=30.0: transport.Page(url, url, 403, "denied", "http"),
    )
    monkeypatch.setattr(
        transport, "fetch_browser",
        lambda url, timeout=60.0: transport.Page(url, url, 200, "<b>x</b>", "browser"),
    )
    page = transport.fetch("https://eshop.example/p")
    assert page.via == "browser"
    assert page.status == 200


def test_fetch_ok_browser_nevola(monkeypatch):
    monkeypatch.setattr(settings, "browser_url", "http://browser:3000")
    monkeypatch.setattr(
        transport, "fetch_http",
        lambda url, timeout=30.0: transport.Page(url, url, 200, "<b>ok</b>", "http"),
    )
    monkeypatch.setattr(
        transport, "fetch_browser",
        lambda url, timeout=60.0: pytest.fail("browser se nemá volat"),
    )
    assert transport.fetch("https://eshop.example/p").via == "http"


def test_fetch_sitova_chyba_eskaluje(monkeypatch):
    # TLS-fingerprint blokace = reset spojení ještě před HTTP odpovědí.
    def _reset(url, timeout=30.0):
        raise httpx.ConnectError("connection reset")

    monkeypatch.setattr(settings, "browser_url", "http://browser:3000")
    monkeypatch.setattr(transport, "fetch_http", _reset)
    monkeypatch.setattr(
        transport, "fetch_browser",
        lambda url, timeout=60.0: transport.Page(url, url, 200, "x", "browser"),
    )
    assert transport.fetch("https://alza.example/p").via == "browser"


def test_browser_fetch_posila_token_a_stealth(monkeypatch):
    import json as jsonmod

    captured = {}

    def _post(url, params=None, json=None, timeout=None):
        captured.update(url=url, params=params, json=json)
        return httpx.Response(200, text="<html>z browseru</html>")

    monkeypatch.setattr(settings, "browser_url", "http://browser:3000")
    monkeypatch.setattr(settings, "browser_token", "tajny")
    monkeypatch.setattr(transport.httpx, "post", _post)
    page = transport.fetch_browser("https://eshop.example/p")
    assert page.text == "<html>z browseru</html>"
    assert captured["url"] == "http://browser:3000/content"
    assert captured["params"]["token"] == "tajny"
    assert jsonmod.loads(captured["params"]["launch"]) == {"stealth": True}
    assert captured["json"]["url"] == "https://eshop.example/p"
    assert captured["json"]["blockConsentModals"] is True


def test_browser_fetch_degraduje_na_minimalni_telo(monkeypatch):
    # Starší browserless odmítne neznámá volitelná pole celou validací těla
    # — druhý pokus jede bez nich.
    calls = []

    def _post(url, params=None, json=None, timeout=None):
        calls.append(json)
        if "blockConsentModals" in json:
            return httpx.Response(
                400, text="POST Body validation failed: "
                          "must NOT have additional properties"
            )
        return httpx.Response(200, text="<b>obsah</b>")

    monkeypatch.setattr(settings, "browser_url", "http://browser:3000")
    monkeypatch.setattr(transport.httpx, "post", _post)
    page = transport.fetch_browser("https://eshop.example/p")
    assert page.text == "<b>obsah</b>"
    assert len(calls) == 2
    assert "waitForSelector" not in calls[1]
    assert calls[1]["bestAttempt"] is True


def test_browser_fetch_pozna_challenge(monkeypatch, tmp_path):
    def _post(url, params=None, json=None, timeout=None):
        return httpx.Response(200, text="<title>Just a moment...</title>")

    monkeypatch.setattr(settings, "browser_url", "http://browser:3000")
    monkeypatch.setattr(settings, "snapshot_dir", str(tmp_path))
    monkeypatch.setattr(transport.httpx, "post", _post)
    with pytest.raises(transport.TransportError, match="blokuje i skutečný Chrome"):
        transport.fetch_browser("https://alza.example/p")
    assert list((tmp_path / "failures").glob("*-browser.html.gz"))


def test_save_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "snapshot_dir", str(tmp_path))
    path = transport.save_failure("alza.cz", "<html>divné</html>")
    assert path and path.endswith(".html.gz")
    monkeypatch.setattr(settings, "snapshot_dir", "")
    assert transport.save_failure("alza.cz", "x") is None


def test_domain_of():
    assert transport.domain_of("https://www.alza.cz/led/d123.htm") == "alza.cz"
    assert transport.domain_of("https://shop.example.org/p?x=1") == "shop.example.org"


# --- JSON-LD extraktor -----------------------------------------------------

def _page(block: str) -> str:
    return ('<html><head><script type="application/ld+json">'
            + block + "</script></head></html>")


def test_jsonld_zakladni_product():
    html = _page("""{
      "@context": "https://schema.org", "@type": "Product",
      "name": "Lednička X 40 l", "brand": {"@type": "Brand", "name": "Chladix"},
      "gtin13": "8591234567890", "sku": "LX-40",
      "image": ["https://cdn.example/lx40.jpg"],
      "description": "Kompresorová autochladnička.",
      "offers": {"@type": "Offer", "price": "5490", "priceCurrency": "CZK",
                 "availability": "https://schema.org/InStock",
                 "url": "https://eshop.example/lx40"}
    }""")
    p = jsonld.best(html)
    assert p is not None
    assert p.name == "Lednička X 40 l"
    assert p.brand == "Chladix"
    assert p.ean == "8591234567890"
    assert p.price == 5490.0
    assert p.currency == "CZK"
    assert p.in_stock is True
    assert p.image == "https://cdn.example/lx40.jpg"


def test_jsonld_graph_a_carka_v_cene():
    html = _page("""{
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "BreadcrumbList", "itemListElement": []},
        {"@type": ["Product"], "name": "Mrazák Y",
         "offers": [{"@type": "Offer", "price": "6 999,50",
                     "priceCurrency": "CZK",
                     "availability": "http://schema.org/OutOfStock"}]}
      ]
    }""")
    p = jsonld.best(html)
    assert p is not None
    assert p.price == 6999.50
    assert p.in_stock is False


def test_jsonld_aggregate_offer():
    html = _page("""{
      "@type": "Product", "name": "Box Z",
      "offers": {"@type": "AggregateOffer", "lowPrice": 1290,
                 "highPrice": 1590, "priceCurrency": "CZK"}
    }""")
    p = jsonld.best(html)
    assert p is not None and p.price == 1290.0


def test_jsonld_vice_bloku_preferuje_cenu():
    html = (
        _page('{"@type": "Product", "name": "Bez ceny"}')
        + _page('{"@type": "Product", "name": "S cenou",'
                ' "offers": {"price": 999, "priceCurrency": "CZK"}}')
    )
    p = jsonld.best(html)
    assert p is not None and p.name == "S cenou"


def test_jsonld_bez_productu():
    assert jsonld.best(_page('{"@type": "WebSite", "name": "Eshop"}')) is None
    assert jsonld.best("<html>žádný jsonld</html>") is None


def test_jsonld_nevalidni_blok_nepada():
    html = _page("{tohle není json") + _page(
        '{"@type": "Product", "name": "OK", "offers": {"price": 10}}'
    )
    p = jsonld.best(html)
    assert p is not None and p.name == "OK"


# --- inspekce tichých selhání (API) -----------------------------------------

def test_failures_endpointy(monkeypatch, tmp_path):
    import gzip

    from fastapi.testclient import TestClient

    from trapline.api.main import app

    monkeypatch.setattr(settings, "app_password", "")
    monkeypatch.setattr(settings, "snapshot_dir", str(tmp_path))
    client = TestClient(app)

    assert client.get("/api/system/failures").json() == {"items": []}

    folder = tmp_path / "failures"
    folder.mkdir()
    (folder / "20260819-070200-alza-cz.html.gz").write_bytes(
        gzip.compress(b"<html>challenge</html>")
    )
    items = client.get("/api/system/failures").json()["items"]
    assert items[0]["name"] == "20260819-070200-alza-cz.html.gz"

    r = client.get("/api/system/failures/20260819-070200-alza-cz.html.gz")
    assert r.status_code == 200
    assert "challenge" in r.text

    assert client.get("/api/system/failures/neexistuje.html.gz").status_code == 404
    # jméno mimo povolený tvar (traversal apod.) odmítne regex ještě před diskem
    assert client.get("/api/system/failures/x..y.html.gz.evil").status_code == 404
    from trapline.api.system import _FAILURE_NAME
    assert not _FAILURE_NAME.fullmatch("../x.html.gz")
    assert not _FAILURE_NAME.fullmatch("a/b.html.gz")
