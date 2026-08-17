"""Testy zabezpečení GUI heslem."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trapline.api import auth
from trapline.api.main import app
from trapline.config import settings

HESLO = "tajne-heslo"


@pytest.fixture(autouse=True)
def cisty_stav(monkeypatch):
    monkeypatch.setattr(settings, "auth_secret", "")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    auth._fails.clear()
    yield
    auth._fails.clear()


@pytest.fixture
def chranene(monkeypatch):
    monkeypatch.setattr(settings, "app_password", HESLO)
    return TestClient(app)


@pytest.fixture
def otevrene(monkeypatch):
    monkeypatch.setattr(settings, "app_password", "")
    return TestClient(app)


def test_bez_hesla_je_vse_otevrene(otevrene):
    assert otevrene.get("/api/auth/status").json() == {
        "required": False,
        "authenticated": True,
    }
    assert otevrene.get("/api/system/version").status_code == 200


def test_s_heslem_je_api_zavrene(chranene):
    assert chranene.get("/api/system/version").status_code == 401
    # Update umí restartovat proces — tenhle musí být zavřený především.
    assert chranene.post("/api/system/update").status_code == 401
    assert chranene.post("/api/system/check").status_code == 401


def test_health_a_status_jsou_verejne(chranene):
    """Bez nich by se GUI nemělo jak zeptat, jestli je potřeba přihlášení,
    a healthcheck v compose by hlásil mrtvou appku."""
    assert chranene.get("/api/health").status_code == 200
    assert chranene.get("/api/auth/status").json() == {
        "required": True,
        "authenticated": False,
    }


def test_gui_skorapka_je_verejna(chranene):
    """Statické HTML nic neprozrazuje — data si tahá až po přihlášení."""
    assert chranene.get("/").status_code == 200


def test_spatne_heslo_neprojde(chranene):
    r = chranene.post("/api/auth/login", json={"password": "blbost"})
    assert r.status_code == 401
    assert chranene.get("/api/system/version").status_code == 401


def test_prihlaseni_otevre_api(chranene):
    assert chranene.post("/api/auth/login", json={"password": HESLO}).status_code == 200
    assert chranene.get("/api/auth/status").json()["authenticated"] is True
    assert chranene.get("/api/system/version").status_code == 200


def test_odhlaseni_zavre_api(chranene):
    chranene.post("/api/auth/login", json={"password": HESLO})
    chranene.post("/api/auth/logout")
    assert chranene.get("/api/system/version").status_code == 401


def test_cookie_je_httponly(chranene):
    r = chranene.post("/api/auth/login", json={"password": HESLO})
    hlavicka = r.headers["set-cookie"].lower()
    assert "httponly" in hlavicka
    assert "samesite=lax" in hlavicka


def test_token_prezije_restart(chranene, monkeypatch):
    """Podpisový klíč se odvozuje z hesla, ne generuje náhodně. Jinak by tě
    self-update (který restartuje proces) pokaždé odhlásil."""
    token = auth.make_token()
    assert auth.valid_token(token)

    # Simuluj restart: nový proces, žádný sdílený stav v paměti.
    monkeypatch.setattr(settings, "app_password", HESLO)
    assert auth.valid_token(token)


def test_zmena_hesla_zneplatni_stare_tokeny(monkeypatch):
    monkeypatch.setattr(settings, "app_password", HESLO)
    token = auth.make_token()
    assert auth.valid_token(token)

    monkeypatch.setattr(settings, "app_password", "jine-heslo")
    assert not auth.valid_token(token)


def test_podvrzeny_token_neprojde(monkeypatch):
    monkeypatch.setattr(settings, "app_password", HESLO)
    assert not auth.valid_token("neco.podvrzeneho")
    payload = auth.make_token().split(".")[0]
    assert not auth.valid_token(f"{payload}.{'0' * 64}")


def test_expirovany_token_neprojde(monkeypatch):
    monkeypatch.setattr(settings, "app_password", HESLO)
    assert not auth.valid_token(auth.make_token(days=-1))


def test_brute_force_se_zamkne(chranene):
    for _ in range(auth.MAX_FAILS):
        chranene.post("/api/auth/login", json={"password": "blbost"})
    r = chranene.post("/api/auth/login", json={"password": "blbost"})
    assert r.status_code == 429
    # Zámek platí i pro správné heslo – jinak by šel obejít hádáním dál.
    assert chranene.post("/api/auth/login", json={"password": HESLO}).status_code == 429
