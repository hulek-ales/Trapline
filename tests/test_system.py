"""Testy self-update endpointů.

Git se nevolá naostro proti skutečnému remote — ``_git`` se podvrhne, aby
testy nezávisely na tom, v jakém stavu je pracovní strom.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trapline.api import system
from trapline.api.main import app
from trapline.config import settings


@pytest.fixture
def client(monkeypatch):
    fake = {
        ("log", "-1", "--format=%h"): "abc1234",
        ("log", "-1", "--format=%ci"): "2026-08-17 10:00:00 +0200",
        ("log", "-1", "--format=%s"): "skeleton",
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("rev-list", "--count", "HEAD..origin/main"): "3",
        ("log", "-1", "--format=%s", "origin/main"): "novy commit",
    }
    monkeypatch.setattr(system, "_git", lambda *a: fake.get(tuple(a), ""))
    return TestClient(app)


def test_version_vzdy_dostupna(client, monkeypatch):
    """Verze jde přečíst i se zakázanými aktualizacemi — GUI podle ní pozná,
    že má panel skrýt."""
    monkeypatch.setattr(settings, "update_enabled", False)
    r = client.get("/api/system/version")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["commit"] == "abc1234"
    assert body["branch"] == "main"


def test_check_a_update_zakazane_bez_flagu(client, monkeypatch):
    monkeypatch.setattr(settings, "update_enabled", False)
    assert client.post("/api/system/check").status_code == 403
    assert client.post("/api/system/update").status_code == 403


def test_check_hlasi_pozadi(client, monkeypatch):
    monkeypatch.setattr(settings, "update_enabled", True)
    body = client.post("/api/system/check").json()
    assert body == {
        "behind": 3,
        "update_available": True,
        "remote_subject": "novy commit",
        "branch": "main",
    }


def test_check_neciselny_vystup_je_nula(client, monkeypatch):
    """`git rev-list` po selhání vrátí text chyby, ne číslo. To nesmí shodit
    endpoint ani ohlásit falešnou aktualizaci."""
    monkeypatch.setattr(settings, "update_enabled", True)
    monkeypatch.setattr(
        system, "_git", lambda *a: "main" if a[0] == "rev-parse" else "fatal: ..."
    )
    body = client.post("/api/system/check").json()
    assert body["behind"] == 0
    assert body["update_available"] is False


def test_update_mimo_docker_pullne(client, monkeypatch):
    monkeypatch.setattr(settings, "update_enabled", True)
    monkeypatch.setattr(system, "_supervised", lambda: False)
    monkeypatch.setattr(system, "_git", lambda *a: "Updating abc..def")
    body = client.post("/api/system/update").json()
    assert body["mode"] == "manual"
    assert "abc..def" in body["output"]


def test_update_pod_supervisorem_ukonci_proces(client, monkeypatch, tmp_path):
    """Pod Dockerem se nepullne tady — jen se položí značka a ukončí proces,
    zbytek udělá supervisor smyčka."""
    monkeypatch.setattr(settings, "update_enabled", True)
    monkeypatch.setattr(system, "_supervised", lambda: True)
    monkeypatch.setattr(system, "_repo_dir", lambda: str(tmp_path))
    exited: list[int] = []
    monkeypatch.setattr(system.os, "_exit", exited.append)

    body = client.post("/api/system/update").json()
    assert body["mode"] == "docker"
    assert (tmp_path / ".needs-build").exists()

    for t in list(system.threading.enumerate()):
        if t is not system.threading.current_thread():
            t.join(timeout=3)
    assert exited == [0]


def test_gui_se_serviruje_na_korenu(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Aktualizovat z Gitu" in r.text
