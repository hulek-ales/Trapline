"""Testy CRUD kritérií a chování při nedostupné DB.

Místo MariaDB jede sqlite v paměti — schéma je stejné (create_all) a testy
nepotřebují běžící server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from trapline import db
from trapline.api.main import app
from trapline.config import settings
from trapline.models import Base


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite FK v defaultu nevynucuje — MariaDB ano; bez tohohle testy
    # nechytí mazání rodiče s navázanými řádky (reálný pád v produkci)
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_ready", True)
    monkeypatch.setattr(settings, "app_password", "")
    return TestClient(app)


PAST = {
    "name": "kompresorová autochladnička",
    "query_terms": ["autochladnička", "car fridge"],
    "hard": {"compressor": True, "capacity_l_min": 12},
    "soft": {"freezer": 3.0},
    "budget_max": 6000,
}


def test_prazdny_seznam(client):
    assert client.get("/api/criteria").json() == []


def test_create_a_list(client):
    r = client.post("/api/criteria", json=PAST)
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == 1
    assert body["active"] is True
    assert body["hard"]["compressor"] is True

    rows = client.get("/api/criteria").json()
    assert len(rows) == 1
    assert rows[0]["name"] == PAST["name"]


def test_validace_vstupu(client):
    assert client.post("/api/criteria", json={"name": ""}).status_code == 422
    assert (
        client.post("/api/criteria", json={"name": "x", "budget_max": -5}).status_code
        == 422
    )


def test_patch_meni_jen_poslane(client):
    cid = client.post("/api/criteria", json=PAST).json()["id"]
    r = client.patch(f"/api/criteria/{cid}", json={"active": False})
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False
    assert body["name"] == PAST["name"]  # nezměněno
    assert body["budget_max"] == PAST["budget_max"]


def test_delete(client):
    cid = client.post("/api/criteria", json=PAST).json()["id"]
    assert client.delete(f"/api/criteria/{cid}").status_code == 204
    assert client.get("/api/criteria").json() == []
    assert client.delete(f"/api/criteria/{cid}").status_code == 404


def test_neexistujici_id_je_404(client):
    assert client.patch("/api/criteria/999", json={"active": False}).status_code == 404


def test_lehla_db_vraci_503(client, monkeypatch):
    monkeypatch.setattr(db, "ensure_ready", lambda: False)
    assert client.get("/api/criteria").status_code == 503
    # Systémové endpointy na DB nezávisí — self-update musí jet i s lehlou DB.
    assert client.get("/api/system/version").status_code == 200


def test_health_hlasi_stav_db(client, monkeypatch):
    assert client.get("/api/health").json() == {"status": "ok", "db": True}
    monkeypatch.setattr(db, "_ready", False)
    assert client.get("/api/health").json()["db"] is False


def test_kriteria_jsou_za_heslem(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "tajne")
    assert client.get("/api/criteria").status_code == 401
    assert client.post("/api/criteria", json=PAST).status_code == 401


def test_patch_umi_zrusit_budget(client):
    cid = client.post("/api/criteria", json=PAST).json()["id"]
    r = client.patch(f"/api/criteria/{cid}", json={"budget_max": None})
    assert r.status_code == 200
    assert r.json()["budget_max"] is None


def test_delete_pasti_s_vysledky_skoringu(client, monkeypatch):
    """Past s uloženými výsledky skóringu musí jít smazat (FK na
    criteria_matches) — reálný pád z produkce."""
    from sqlalchemy.orm import Session

    from trapline import scoring
    from trapline.models import Criteria, CriteriaMatch, Product

    cid = client.post("/api/criteria", json=PAST).json()["id"]
    with Session(db._engine) as s:
        product = Product(brand="X", model="M", model_norm="m", title="M")
        s.add(product)
        s.flush()
        trap = s.get(Criteria, cid)
        monkeypatch.setattr(
            scoring, "_ask_llm",
            lambda t, p: [{"pozadavek": "x", "verdikt": "splneno"}],
        )
        scoring.score_pair(s, trap, product)
        s.commit()
        assert s.query(CriteriaMatch).count() == 1

    assert client.delete(f"/api/criteria/{cid}").status_code == 204
    with Session(db._engine) as s:
        assert s.query(CriteriaMatch).count() == 0
