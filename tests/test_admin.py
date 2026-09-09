"""Testy administrace — nastavení LLM z GUI (ADR-0010)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from trapline import db, llm, settings_store
from trapline.api.main import app
from trapline.config import settings
from trapline.models import AppSetting, Base


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
    # známý „stav z prostředí", ať test nezávisí na .env stroje
    env = {
        "ollama_url": "http://ollama.env:11434",
        "ollama_key": "",
        "llm_main": "qwen3:14b",
        "llm_bulk": "qwen3:4b",
        "llm_load_wait_s": 900.0,
    }
    for k, v in env.items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(settings_store, "_ENV", dict(env))
    monkeypatch.setattr(settings_store, "_from_db", set())
    return TestClient(app)


def test_get_nevraci_klic_a_hlasi_zdroj(client, monkeypatch):
    monkeypatch.setattr(settings, "ollama_key", "opx_tajny")
    d = client.get("/api/admin/settings").json()["llm"]
    assert d["ollama_url"] == "http://ollama.env:11434"
    assert d["ollama_key_set"] is True
    assert "ollama_key" not in d
    assert "opx_tajny" not in str(d)
    assert d["source"]["ollama_url"] == "env"


def test_put_ulozi_a_hned_pouzije(client):
    r = client.put("/api/admin/settings", json={
        "ollama_url": "https://proxy.example/",
        "ollama_key": "opx_novy",
        "llm_main": "gemma4:12b",
        "llm_load_wait_s": 120,
    })
    assert r.status_code == 200
    d = r.json()["llm"]
    assert d["ollama_url"] == "https://proxy.example/"
    assert d["ollama_key_set"] is True
    assert d["source"]["ollama_url"] == "gui"
    assert d["source"]["llm_bulk"] == "env"  # nezměněno
    # ...a settings to vidí okamžitě, bez restartu
    assert settings.ollama_url == "https://proxy.example/"
    assert settings.ollama_key == "opx_novy"
    assert settings.llm_main == "gemma4:12b"
    assert settings.llm_load_wait_s == 120.0
    assert settings.llm_proxy_enabled is True
    assert "opx_novy" not in str(d)


def test_none_nemeni_prazdny_klic_maze(client):
    client.put("/api/admin/settings", json={"ollama_key": "opx_a"})
    # None (vynechané pole) = beze změny
    client.put("/api/admin/settings", json={"llm_main": "x"})
    assert settings.ollama_key == "opx_a"
    # prázdný řetězec = smazat
    d = client.put("/api/admin/settings", json={"ollama_key": ""}).json()["llm"]
    assert settings.ollama_key == ""
    assert d["ollama_key_set"] is False


def test_ulozene_prezije_restart(client):
    """Při startu se DB promítne do settings — simulace novým načtením."""
    client.put("/api/admin/settings", json={"llm_load_wait_s": 42})
    settings.llm_load_wait_s = 900.0  # „restart" — prostředí
    settings_store._from_db.clear()
    with db.open_session() as session:
        keys = settings_store.load_overrides(session)
    assert keys == {"llm_load_wait_s"}
    assert settings.llm_load_wait_s == 42.0  # a jako float, ne text


def test_reset_vrati_prostredi_a_smaze_radky(client):
    client.put("/api/admin/settings", json={
        "ollama_url": "https://proxy.example", "ollama_key": "opx_x",
    })
    d = client.post("/api/admin/settings/reset").json()["llm"]
    assert settings.ollama_url == "http://ollama.env:11434"
    assert settings.ollama_key == ""
    assert d["source"]["ollama_url"] == "env"
    with db.open_session() as session:
        assert session.scalars(select(AppSetting)).all() == []


def test_ulozeni_resetuje_utlum_planovace(client, monkeypatch):
    monkeypatch.setattr(llm, "_scheduler_missing", True)
    client.put("/api/admin/settings", json={"ollama_url": "https://p.example"})
    assert llm._scheduler_missing is False


def test_validace_cekani(client):
    assert client.put(
        "/api/admin/settings", json={"llm_load_wait_s": -1}
    ).status_code == 422


def test_test_spojeni_vola_diagnostiku(client, monkeypatch):
    monkeypatch.setattr(llm, "available_models", lambda: ["gemma4:12b"])
    monkeypatch.setattr(llm, "running_models", lambda: [])
    monkeypatch.setattr(settings, "llm_main", "gemma4:12b")
    d = client.post("/api/admin/settings/test").json()
    assert d["reachable"] is True
    assert d["model_available"] is True
    assert d["models"] == ["gemma4:12b"]


def test_admin_je_za_heslem(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "tajne")
    assert client.get("/api/admin/settings").status_code == 401
