"""Testy Allegro konektoru (ADR-0008) a přepočtu měn přes ČNB."""

from __future__ import annotations

import httpx
import pytest

from trapline import fx
from trapline.config import settings
from trapline.crawlers import allegro

_REQ = httpx.Request("GET", "https://www.cnb.cz/denni_kurz.txt")

CNB = """05.09.2026 #172
země|měna|množství|kód|kurz
Polsko|zlotý|1|PLN|5,432
Japonsko|jen|100|JPY|14,210
EMU|euro|1|EUR|24,300
"""


@pytest.fixture(autouse=True)
def _cisty_stav(monkeypatch):
    """Token ani kurzy se nesmí přelévat mezi testy."""
    allegro.reset_token()
    monkeypatch.setattr(fx, "_rates", {})
    monkeypatch.setattr(fx, "_rates_day", None)
    monkeypatch.setattr(settings, "allegro_client_id", "id")
    monkeypatch.setattr(settings, "allegro_client_secret", "secret")
    monkeypatch.setattr(settings, "_allegro_user_agent", "Trapline/test")
    yield
    allegro.reset_token()


# --- kurzy ČNB -------------------------------------------------------------

def test_cnb_kurzy_vcetne_mnozstvi():
    rates = fx._parse(CNB)
    assert rates["PLN"] == pytest.approx(5.432)
    # Jen se uvádí za 100 kusů — kurz musí být za jednotku.
    assert rates["JPY"] == pytest.approx(0.1421)


def test_prevod_na_koruny(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda *a, **kw: httpx.Response(200, text=CNB, request=_REQ)
    )
    assert fx.to_czk(100.0, "PLN") == 543
    assert fx.to_czk(100.0, "CZK") == 100


def test_bez_kurzu_radeji_nic(monkeypatch):
    """Neznámý kurz nesmí skončit tím, že se zlotý bere jako koruna."""
    def _vypadek(*a, **kw):
        raise httpx.ConnectError("cnb nedostupna")

    monkeypatch.setattr(httpx, "get", _vypadek)
    assert fx.to_czk(100.0, "PLN") is None


def test_kurz_prezije_vypadek_cnb(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda *a, **kw: httpx.Response(200, text=CNB, request=_REQ)
    )
    assert fx.rate("PLN") == pytest.approx(5.432)

    def _vypadek(*a, **kw):
        raise httpx.ConnectError("cnb nedostupna")

    monkeypatch.setattr(httpx, "get", _vypadek)
    monkeypatch.setattr(fx, "_rates_day", None)  # vynuť pokus o obnovu
    assert fx.rate("PLN") == pytest.approx(5.432)


# --- token -----------------------------------------------------------------

def test_token_se_drzi_a_neposila_klic_v_url(monkeypatch):
    calls = []

    def _post(url, params=None, auth=None, headers=None, timeout=None):
        calls.append((url, params, auth))
        return httpx.Response(
            200, json={"access_token": "tok-123", "expires_in": 43200}
        )

    monkeypatch.setattr(httpx, "post", _post)
    assert allegro.token() == "tok-123"
    assert allegro.token() == "tok-123"
    assert len(calls) == 1
    url, params, auth = calls[0]
    assert params == {"grant_type": "client_credentials"}
    assert auth == ("id", "secret")
    assert "secret" not in url


def test_token_chyba_je_srozumitelna(monkeypatch):
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **kw: httpx.Response(401, text="invalid_client"),
    )
    with pytest.raises(allegro.AllegroError, match="401"):
        allegro.token()


def test_bez_klicu_token_nezkousi_sit(monkeypatch):
    monkeypatch.setattr(settings, "allegro_client_id", "")
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **kw: pytest.fail("bez klíčů se nemá volat Allegro"),
    )
    with pytest.raises(allegro.AllegroError, match="CLIENT_ID"):
        allegro.token()


# --- hledání ---------------------------------------------------------------

LISTING = {
    "items": {
        "promoted": [{
            "id": "111",
            "name": "Lodówka turystyczna 40 l",
            "sellingMode": {"price": {"amount": "1200.00", "currency": "PLN"}},
            "location": {"city": "Wrocław", "province": "dolnośląskie"},
            "parameters": [{"name": "Stan", "values": ["Nowy"]}],
            "delivery": {"lowestPrice": {"amount": "50.00", "currency": "PLN"}},
        }],
        "regular": [
            {
                "id": "222",
                "name": "Hamak dwuosobowy",
                "sellingMode": {"price": {"amount": "80.00", "currency": "PLN"}},
                "location": {"city": "Kraków"},
                "delivery": {"availableForFree": True},
            },
            {"name": "nabídka bez id"},
        ],
    }
}


def _fake_get(monkeypatch, cnb_ok: bool = True):
    """``fx`` i ``allegro`` sahají na tentýž modul httpx — jedna náhrada
    musí obsloužit obojí, jinak si testy patche přepíšou."""
    def _get(url, *a, **kw):
        if "cnb.cz" in url:
            if not cnb_ok:
                raise httpx.ConnectError("cnb nedostupna")
            return httpx.Response(200, text=CNB, request=_REQ)
        return httpx.Response(200, json=LISTING)

    monkeypatch.setattr(httpx, "get", _get)


def test_search_prevadi_ceny_a_sklada_popis(monkeypatch):
    _fake_get(monkeypatch)
    monkeypatch.setattr(allegro, "token", lambda: "tok")
    ads = allegro.search("lodówka")
    assert [a.ext_id for a in ads] == ["111", "222"]  # bez id se přeskočí
    first = ads[0]
    assert first.price == 6518  # 1200 PLN × 5,432
    assert first.url == "https://allegro.pl/oferta/111"
    assert first.locality == "Polsko, Wrocław, dolnośląskie"
    assert "Stan: Nowy" in first.description
    assert "doprava od 272 Kč" in first.description
    assert "doprava zdarma" in ads[1].description


def test_search_bez_kurzu_neuvede_cenu(monkeypatch):
    """Radši inzerát bez ceny než zlotý vydávaný za korunu."""
    _fake_get(monkeypatch, cnb_ok=False)
    monkeypatch.setattr(allegro, "token", lambda: "tok")
    assert [a.price for a in allegro.search("lodówka")] == [None, None]


def test_search_401_zahodi_token(monkeypatch):
    monkeypatch.setattr(allegro, "_token", "stary")
    monkeypatch.setattr(allegro, "_token_until", 1e12)
    monkeypatch.setattr(
        httpx, "get", lambda *a, **kw: httpx.Response(401, text="expired")
    )
    with pytest.raises(allegro.AllegroError, match="401"):
        allegro.search("hamak")
    assert allegro._token == ""


def test_alive_jen_404_znamena_pryc(monkeypatch):
    monkeypatch.setattr(
        httpx, "head", lambda *a, **kw: httpx.Response(404)
    )
    assert allegro.alive("111") is False

    monkeypatch.setattr(
        httpx, "head", lambda *a, **kw: httpx.Response(200)
    )
    assert allegro.alive("111") is True

    def _blokace(*a, **kw):
        raise httpx.ConnectError("blocked")

    # Nejistota nesmí inzerát pohřbít.
    monkeypatch.setattr(httpx, "head", _blokace)
    assert allegro.alive("111") is True
