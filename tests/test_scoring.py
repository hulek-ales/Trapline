"""Testy LLM skóringu — model je mockovaný, testuje se logika okolo."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import db, scoring
from trapline.api.main import app
from trapline.config import settings
from trapline.models import Base, Criteria, CriteriaMatch, Product


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
    with Session(engine) as s:
        yield s


def _trap(session, terms=None) -> Criteria:
    trap = Criteria(
        name="Camping lednička",
        query_terms=terms or ["provoz na 12V", "provoz na 230V", "mrazení"],
    )
    session.add(trap)
    session.flush()
    return trap


def _product(session, title="Autochladnička BBPF-30A", **specs) -> Product:
    product = Product(
        brand="BestBerg", model=title, model_norm=title.lower(), title=title,
        specs=specs,
    )
    session.add(product)
    session.flush()
    return product


# --- výpočet skóre ---------------------------------------------------------

def test_skore_z_verdiktu():
    b = [{"verdikt": "splneno"}, {"verdikt": "splneno"}, {"verdikt": "nelze_urcit"}]
    score, relevant = scoring.compute_score(b)
    assert score == round(100 * 2.5 / 3, 1)
    assert relevant is True


def test_jedno_nesplneno_znamena_nerelevantni():
    """I s vysokým skóre: explicitně nesplněný požadavek produkt vyřazuje."""
    b = [{"verdikt": "splneno"}] * 4 + [{"verdikt": "nesplneno"}]
    score, relevant = scoring.compute_score(b)
    assert score == 80.0
    assert relevant is False


def test_same_neznama_data_nejsou_relevantni():
    b = [{"verdikt": "nelze_urcit"}] * 4
    score, relevant = scoring.compute_score(b)
    assert score == 50.0
    assert relevant is False  # pod prahem RELEVANT_MIN


def test_prazdny_breakdown():
    assert scoring.compute_score([]) == (0.0, False)


# --- criteria_rev ----------------------------------------------------------

def test_rev_se_meni_se_zadanim(session):
    trap = _trap(session)
    rev1 = scoring.criteria_rev(trap)
    trap.query_terms = [*trap.query_terms, "provoz na plyn"]
    assert scoring.criteria_rev(trap) != rev1


# --- score_pair a přeskakování ---------------------------------------------

def _fake_llm(verdikt="splneno"):
    def fake(trap, product):
        return [
            {"pozadavek": t, "verdikt": verdikt, "pozn": ""}
            for t in trap.query_terms
        ]
    return fake


def test_score_pair_uklada_match(session, monkeypatch):
    monkeypatch.setattr(scoring, "_ask_llm", _fake_llm())
    trap, product = _trap(session), _product(session)
    match = scoring.score_pair(session, trap, product)
    session.commit()
    assert match.score == 100.0
    assert match.relevant is True
    assert match.criteria_rev == scoring.criteria_rev(trap)
    assert len(match.breakdown) == 3


def test_prehodnoceni_prepise_stejny_radek(session, monkeypatch):
    trap, product = _trap(session), _product(session)
    monkeypatch.setattr(scoring, "_ask_llm", _fake_llm("splneno"))
    scoring.score_pair(session, trap, product)
    monkeypatch.setattr(scoring, "_ask_llm", _fake_llm("nesplneno"))
    scoring.score_pair(session, trap, product)
    session.commit()
    rows = session.scalars(select(CriteriaMatch)).all()
    assert len(rows) == 1
    assert rows[0].score == 0.0


def test_popis_jde_do_llm_ale_ne_do_parametru(session, monkeypatch):
    """_popis se posílá modelu zvlášť, v parametrech být nesmí."""
    seen = {}

    def spy(trap, product):
        seen.update(scoring._product_payload(product))
        return [{"pozadavek": "x", "verdikt": "splneno"}]

    monkeypatch.setattr(scoring, "_ask_llm", spy)
    trap = _trap(session, ["x"])
    product = _product(session, Objem="30 l", _popis="Kompresorová chladnička")
    scoring.score_pair(session, trap, product)
    assert seen["popis"] == "Kompresorová chladnička"
    assert "_popis" not in seen["parametry"]
    assert seen["parametry"] == {"Objem": "30 l"}


# --- API -------------------------------------------------------------------

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
    return TestClient(app)


def test_run_bez_pasti_je_400(client):
    assert client.post("/api/scoring/run").status_code == 400


def test_run_bez_katalogu_je_400(client):
    client.post("/api/criteria", json={"name": "x", "query_terms": ["a"]})
    r = client.post("/api/scoring/run")
    assert r.status_code == 400
    assert "discovery" in r.json()["detail"]


def test_ollama_diagnostika_bez_url(client, monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "")
    body = client.get("/api/scoring/ollama").json()
    assert body["reachable"] is False


def test_products_vraci_matches(client, monkeypatch):
    with Session(db._engine) as s:
        trap = _trap(s)
        product = _product(s)
        monkeypatch.setattr(scoring, "_ask_llm", _fake_llm())
        scoring.score_pair(s, trap, product)
        s.commit()

    rows = client.get("/api/discovery/products").json()
    assert len(rows[0]["matches"]) == 1
    m = rows[0]["matches"][0]
    assert m["criteria_name"] == "Camping lednička"
    assert m["score"] == 100.0
    assert m["relevant"] is True


def test_scoring_je_za_heslem(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "tajne")
    assert client.post("/api/scoring/run").status_code == 401
    assert client.get("/api/scoring/ollama").status_code == 401


def test_prefilter_omezuje_zaber(session, monkeypatch):
    trap = _trap(session)
    trap.prefilter = "chladni, lednic"
    fridge = _product(session, title="Autochladnička BBPF-30A")
    knife = _product(session, title="Zavírací nůž MAM Douro")
    assert scoring.passes_prefilter(trap, fridge)
    assert not scoring.passes_prefilter(trap, knife)


def test_prazdny_prefilter_pousti_vse(session):
    trap = _trap(session)
    trap.prefilter = ""
    assert scoring.passes_prefilter(trap, _product(session))


def test_zmena_promptu_zneplatni_skore(session, monkeypatch):
    trap = _trap(session)
    rev = scoring.criteria_rev(trap)
    monkeypatch.setattr(scoring, "PROMPT_REV", "test-jina-verze")
    assert scoring.criteria_rev(trap) != rev


def test_rucni_verdikt(client, monkeypatch):
    with Session(db._engine) as s:
        product = _product(s)
        s.commit()
        pid = product.id
    r = client.put(f"/api/products/{pid}/verdict", json={"verdict": "dislike"})
    assert r.status_code == 200
    rows = client.get("/api/discovery/products").json()
    assert rows[0]["verdict"] == "dislike"
    # návrat na neutral verdikt z výpisu zmizí
    client.put(f"/api/products/{pid}/verdict", json={"verdict": "neutral"})
    assert client.get("/api/discovery/products").json()[0]["verdict"] is None
    assert client.put("/api/products/999/verdict",
                      json={"verdict": "like"}).status_code == 404


def test_migrace_prida_prefilter(monkeypatch, tmp_path):
    """Simulace staré DB bez sloupce prefilter — ensure_ready ho doplní."""
    import sqlalchemy as sa

    url = f"sqlite:///{tmp_path}/old.db"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE criteria (id INTEGER PRIMARY KEY, name VARCHAR(120))"
        ))
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_ready", False)
    assert db.ensure_ready()
    cols = {c["name"] for c in sa.inspect(engine).get_columns("criteria")}
    assert "prefilter" in cols


def _seed_scored(client, monkeypatch, prefilter=""):
    """Past + 2 produkty + skóre pro oba; vrací (trap_id, product_ids)."""
    r = client.post("/api/criteria", json={
        "name": "T", "query_terms": ["provoz na 12V"], "prefilter": prefilter,
    })
    tid = r.json()["id"]
    with Session(db._engine) as s:
        fridge = _product(s, title="Autochladnička BBPF-30A")
        knife = _product(s, title="Zavírací nůž MAM")
        trap = s.get(Criteria, tid)
        monkeypatch.setattr(scoring, "_ask_llm", _fake_llm())
        scoring.score_pair(s, trap, fridge)
        scoring.score_pair(s, trap, knife)
        s.commit()
        return tid, (fridge.id, knife.id)


def _match_count(tid):
    with Session(db._engine) as s:
        return s.query(CriteriaMatch).filter_by(criteria_id=tid).count()


def test_zmena_zadani_vyhazi_vsechna_skore(client, monkeypatch):
    monkeypatch.setattr(scoring, "start", lambda: True)  # nech běh na pokoji
    tid, _ = _seed_scored(client, monkeypatch)
    assert _match_count(tid) == 2
    client.patch(f"/api/criteria/{tid}", json={"query_terms": ["provoz na 230V"]})
    assert _match_count(tid) == 0


def test_zuzeni_prefiltru_vyhazi_jen_nevyhovujici(client, monkeypatch):
    monkeypatch.setattr(scoring, "start", lambda: True)
    tid, (fridge_id, knife_id) = _seed_scored(client, monkeypatch)
    client.patch(f"/api/criteria/{tid}", json={"prefilter": "chladni"})
    with Session(db._engine) as s:
        rows = s.query(CriteriaMatch).filter_by(criteria_id=tid).all()
        assert [m.product_id for m in rows] == [fridge_id]  # nůž vypadl


def test_pauza_skore_nemaze(client, monkeypatch):
    monkeypatch.setattr(scoring, "start", lambda: True)
    tid, _ = _seed_scored(client, monkeypatch)
    client.patch(f"/api/criteria/{tid}", json={"active": False})
    assert _match_count(tid) == 2  # historie zůstává (vypnutí = uložení)


def test_zmena_zadani_spousti_preskorovani(client, monkeypatch):
    calls = []
    monkeypatch.setattr(scoring, "start", lambda: calls.append(1) or True)
    monkeypatch.setattr(settings, "ollama_url", "http://test:1")
    tid, _ = _seed_scored(client, monkeypatch)
    client.patch(f"/api/criteria/{tid}", json={"query_terms": ["jiné"]})
    assert calls  # skóring se nastartoval sám
