"""Testy LLM klienta přes Ollama proxy (ADR-0009): klíč, plánovač, útlum."""

from __future__ import annotations

import httpx
import pytest

from trapline import llm
from trapline.config import settings

CHAT_OK = {"message": {"content": '{"ok": true}'}}


@pytest.fixture(autouse=True)
def _proxy(monkeypatch):
    """Výchozí stav: proxy s klíčem, plánovač přítomný, nic nespí."""
    monkeypatch.setattr(settings, "ollama_url", "https://proxy.example")
    monkeypatch.setattr(settings, "ollama_key", "opx_tajny")
    monkeypatch.setattr(settings, "llm_load_wait_s", 5.0)
    monkeypatch.setattr(llm, "_scheduler_missing", False)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def _route(monkeypatch, handlers: dict, calls: list):
    """Jedna náhrada httpx pro post i get — rozhoduje podle cesty."""
    def _call(method):
        def _fn(url, *a, **kw):
            path = url.split("proxy.example", 1)[1]
            calls.append((method, path, kw.get("headers") or {}, kw.get("json")))
            h = handlers.get(path)
            resp = (
                httpx.Response(404, text="not found") if h is None
                else h(kw) if callable(h) else h
            )
            # bez requestu httpx nedovolí raise_for_status()
            resp.request = httpx.Request(method, url)
            return resp
        return _fn

    monkeypatch.setattr(httpx, "post", _call("POST"))
    monkeypatch.setattr(httpx, "get", _call("GET"))


# --- klíč -------------------------------------------------------------------

def test_kazde_volani_nese_bearer(monkeypatch):
    calls: list = []
    _route(monkeypatch, {
        "/mgmt/v1/models/load": httpx.Response(
            200, json={"loaded": True, "hold_s": 30}
        ),
        "/api/chat": httpx.Response(200, json=CHAT_OK),
        "/api/tags": httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]}),
    }, calls)
    assert llm.chat_json("s", "u", {"type": "object"}) == {"ok": True}
    assert llm.available_models() == ["qwen3:14b"]
    for _m, _p, headers, _j in calls:
        assert headers.get("Authorization") == "Bearer opx_tajny"


def test_bez_klice_je_to_hola_ollama(monkeypatch):
    """Bez klíče se /mgmt nevolá a nic nenese Authorization — zpětná
    kompatibilita s dosavadním nasazením."""
    monkeypatch.setattr(settings, "ollama_key", "")
    calls: list = []
    _route(monkeypatch, {"/api/chat": httpx.Response(200, json=CHAT_OK)}, calls)
    llm.chat_json("s", "u", {"type": "object"})
    assert [p for _m, p, _h, _j in calls] == ["/api/chat"]
    assert "Authorization" not in calls[0][2]


# --- plánovač ---------------------------------------------------------------

def test_planovac_pred_inferenci(monkeypatch):
    """Dotaz jde až po ``loaded: true`` — a plánovač dostane model i wait_s."""
    calls: list = []
    _route(monkeypatch, {
        "/mgmt/v1/models/load": httpx.Response(
            200, json={"loaded": True, "hold_s": 30}
        ),
        "/api/chat": httpx.Response(200, json=CHAT_OK),
    }, calls)
    llm.chat_json("s", "u", {"type": "object"}, model="qwen3:4b")
    paths = [p for _m, p, _h, _j in calls]
    assert paths == ["/mgmt/v1/models/load", "/api/chat"]
    load_body = calls[0][3]
    assert load_body["model"] == "qwen3:4b"
    assert 0 < load_body["wait_s"] <= llm.LOAD_POLL_S


def test_planovac_ceka_ve_fronte(monkeypatch):
    """queued → loading → loaded: opakuje se dotaz, dokud model nesedí na GPU."""
    seq = iter([
        {"loaded": False, "status": "queued"},
        {"loaded": False, "status": "loading"},
        {"loaded": True, "hold_s": 20},
    ])
    calls: list = []
    _route(monkeypatch, {
        "/mgmt/v1/models/load": lambda kw: httpx.Response(200, json=next(seq)),
    }, calls)
    assert llm.ensure_loaded("qwen3:14b") == 20.0
    assert len(calls) == 3


def test_planovac_vzda_po_lhute(monkeypatch):
    """GPU drží někdo jiný déle, než jsme ochotní čekat → LlmBusy, ne
    nekonečná smyčka."""
    ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0])
    monkeypatch.setattr(llm.time, "monotonic", lambda: next(ticks))
    _route(monkeypatch, {
        "/mgmt/v1/models/load": httpx.Response(
            200, json={"loaded": False, "status": "queued"}
        ),
    }, [])
    with pytest.raises(llm.LlmBusy, match="queued"):
        llm.ensure_loaded("qwen3:14b", budget_s=5.0)


def test_bez_planovace_se_jede_naslepo(monkeypatch):
    """URL s klíčem míří na proxy bez /mgmt (nebo starší verzi): první 404
    plánovač vypne do restartu a dotazy jedou jako dřív."""
    calls: list = []
    _route(monkeypatch, {"/api/chat": httpx.Response(200, json=CHAT_OK)}, calls)
    llm.chat_json("s", "u", {"type": "object"})
    llm.chat_json("s", "u", {"type": "object"})
    paths = [p for _m, p, _h, _j in calls]
    assert paths == ["/mgmt/v1/models/load", "/api/chat", "/api/chat"]
    assert llm._scheduler_missing is True


def test_429_je_srozumitelne(monkeypatch):
    _route(monkeypatch, {
        "/mgmt/v1/models/load": httpx.Response(200, json={"loaded": True}),
        "/api/chat": httpx.Response(429, text="rate limit"),
    }, [])
    with pytest.raises(llm.LlmBusy, match="429"):
        llm.chat_json("s", "u", {"type": "object"})


# --- diagnostika ------------------------------------------------------------

def test_proxy_status_pojmenuje_spatny_klic(monkeypatch):
    _route(monkeypatch, {
        "/mgmt/v1/health": httpx.Response(401, json={"detail": "bad key"}),
    }, [])
    d = llm.proxy_status()
    assert d["enabled"] is True and d["reachable"] is False
    assert "TRAPLINE_OLLAMA_KEY" in d["error"]


def test_proxy_status_vraci_planovac(monkeypatch):
    _route(monkeypatch, {
        "/mgmt/v1/health": httpx.Response(200, json={"ok": True}),
        "/mgmt/v1/models/status": httpx.Response(
            200, json={"loaded": ["qwen3:14b"], "queue": []}
        ),
    }, [])
    d = llm.proxy_status()
    assert d["reachable"] is True
    assert d["scheduler"]["loaded"] == ["qwen3:14b"]


def test_proxy_status_bez_klice(monkeypatch):
    monkeypatch.setattr(settings, "ollama_key", "")
    assert llm.proxy_status() == {"enabled": False}


def test_integrations_hlasi_proxy_bez_klice(monkeypatch):
    from fastapi.testclient import TestClient

    from trapline.api.main import app

    monkeypatch.setattr(settings, "app_password", "")
    d = TestClient(app).get("/api/system/integrations").json()
    assert d["ollama"]["proxy"] is True
    assert "opx_tajny" not in str(d)
