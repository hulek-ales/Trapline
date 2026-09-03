"""Testy bazarové větve: parsery Bazoš/Sbazar a pipeline s LLM verdikty."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trapline import bazar, db
from trapline.crawlers import bazos, sbazar
from trapline.models import (
    Alert,
    Base,
    Condition,
    Criteria,
    Listing,
    ListingMatch,
    Source,
)

# --- Bazoš parser ----------------------------------------------------------

_LISTING = """
<div class="inzeraty inzeratyflex">
<div class="inzeratynadpis"><a href="/inzerat/222915829/stan-husky.php"><img></a>
<h2 class=nadpis><a href="/inzerat/222915829/stan-husky.php">Hamaka DD
Frontline</a></h2>
<span class=velikost10> - [25.8. 2026]</span><br>
<div class=popis>Použitá 1x, top stav, doprava zdarma</div><br><br>
</div>
<div class="inzeratycena"><b><span translate="no">  4 900 Kč</span></b></div>
<div class="inzeratylok">Praha - západ<br>253 03</div>
<div class="inzeratyview">51 x</div>
</div>
<div class="inzeraty inzeratyflex">
<h2 class=nadpis><a href="/inzerat/111/sit.php">Houpací síť</a></h2>
<div class=popis>levně</div>
<div class="inzeratycena"><b><span translate="no">Dohodou</span></b></div>
<div class="inzeratylok">Brno<br>602 00</div>
</div>
"""


def test_bazos_parse_listing():
    ads = bazos.parse_listing(_LISTING, "https://sport.bazos.cz")
    assert len(ads) == 2
    ad = ads[0]
    assert ad.ext_id == "222915829"
    assert ad.url == "https://sport.bazos.cz/inzerat/222915829/stan-husky.php"
    assert ad.title == "Hamaka DD Frontline"
    assert ad.price == 4900.0
    assert "Praha" in ad.locality
    assert ad.listed == "25.8. 2026"
    assert ads[1].price is None               # Dohodou


def test_bazos_parse_price():
    assert bazos.parse_price("  4 900 Kč") == 4900.0
    assert bazos.parse_price("Dohodou") is None
    assert bazos.parse_price("Zdarma") is None


def test_bazos_section_url():
    assert bazos.section_url("sport") == "https://sport.bazos.cz/"
    assert bazos.section_url("sport", 40) == "https://sport.bazos.cz/40/"


def test_bazos_parse_detail():
    html = ('<meta property="og:description" content="Cena: 4 900 Kč, '
            'Lokalita: Praha. Popis: Prodám hamaku, málo používaná.">')
    assert bazos.parse_detail(html) == "Prodám hamaku, málo používaná."
    # obrácené pořadí atributů
    html2 = '<meta content="Popis: Text inzerátu" property="og:description">'
    assert bazos.parse_detail(html2) == "Text inzerátu"
    assert bazos.parse_detail("<html>smazáno</html>") is None


# --- Sbazar parser ---------------------------------------------------------

def test_sbazar_from_item():
    ad = sbazar._from_item({
        "id": 220263272,
        "seo_name": "220263272-hamaka",
        "name": "Hamaka pro dva",
        "price": 500,
        "price_by_agreement": False,
        "locality": {"municipality": "Nový Bor", "district": "Česká Lípa"},
        "sorting_date": "2026-08-08T11:40:20",
    })
    assert ad.ext_id == "220263272"
    assert ad.url == "https://www.sbazar.cz/inzerat/220263272-hamaka"
    assert ad.price == 500.0
    assert ad.locality == "Nový Bor, Česká Lípa"

    dohoda = sbazar._from_item({
        "id": 1, "name": "X", "price": 0, "price_by_agreement": True,
    })
    assert dohoda.price is None


# --- pipeline --------------------------------------------------------------

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
    monkeypatch.setattr(bazar.time, "sleep", lambda s: None)
    with Session(engine) as s:
        yield s


def _trap(session, budget=3000.0) -> Criteria:
    trap = Criteria(
        name="Hamaka", query_terms=["nosnost 120 kg"],
        prefilter="hamak, houpací síť", budget_max=budget,
    )
    session.add(trap)
    session.commit()
    return trap


def _fake_llm(sekce=("sport",), verdikt="splneno", stav="pouzite",
              varovani=()):
    def chat_json(system, user, schema):
        if "sekce" in schema["properties"]:
            return {"sekce": list(sekce)}
        return {
            "pozadavky": [{"pozadavek": "nosnost", "verdikt": verdikt}],
            "stav": stav,
            "varovani": list(varovani),
        }
    return chat_json


def _wire(monkeypatch, ads_bazos=(), ads_sbazar=(), **llm_kw):
    monkeypatch.setattr(
        bazar.bazos, "fetch_listing",
        lambda section, offset=0: list(ads_bazos) if offset == 0 else [],
    )
    monkeypatch.setattr(
        bazar.bazos, "fetch_detail", lambda url: "Plný popis hamaky.")
    monkeypatch.setattr(
        bazar.sbazar, "search", lambda phrase, limit=40: list(ads_sbazar))
    monkeypatch.setattr(bazar.sbazar, "detail", lambda ext: ("Popis.", True))
    monkeypatch.setattr(bazar.llm, "chat_json", _fake_llm(**llm_kw))
    monkeypatch.setattr(bazar, "send_ntfy", lambda *a, **kw: True)


def _ad(ext_id="1", title="Hamaka DD SuperLight", price=1500.0):
    return bazos.BazosAd(
        ext_id=ext_id, url=f"https://sport.bazos.cz/inzerat/{ext_id}/x.php",
        title=title, description="pěkná hamaka", price=price,
        locality="Praha", listed="25.8. 2026",
    )


def test_pipeline_relevantni_inzerat_alertuje(session, monkeypatch):
    _trap(session)
    _wire(monkeypatch, ads_bazos=[_ad()], varovani=["platba předem"])
    assert bazar.run_all() == (1, 1)

    listing = session.scalars(select(Listing)).one()
    assert listing.source == Source.BAZOS
    assert listing.description == "Plný popis hamaky."   # detail doplněn
    match = session.scalars(select(ListingMatch)).one()
    assert match.criteria_id is not None
    assert match.confidence == 1.0
    assert match.condition == Condition.USED
    assert match.red_flags == ["platba předem"]
    alert = session.scalars(select(Alert)).one()
    assert alert.dedup_key == "listing:bazos:1"

    # druhý průchod: nic nového, žádný duplikát
    assert bazar.run_all() == (0, 0)
    assert session.query(ListingMatch).count() == 1
    assert session.query(Alert).count() == 1


def test_pipeline_prefiltr_vyrazuje(session, monkeypatch):
    _trap(session)
    ad = _ad(title="Stan pro 4 osoby")
    ad.description = "rodinný stan s předsíní"
    _wire(monkeypatch, ads_bazos=[ad])
    assert bazar.run_all() == (0, 0)
    assert session.query(Listing).count() == 0


def test_pipeline_nesplneno_nealertuje(session, monkeypatch):
    _trap(session)
    _wire(monkeypatch, ads_bazos=[_ad()], verdikt="nesplneno")
    assert bazar.run_all() == (1, 0)
    assert session.query(Alert).count() == 0


def test_pipeline_nad_budgetem_nealertuje(session, monkeypatch):
    _trap(session, budget=1000.0)
    _wire(monkeypatch, ads_bazos=[_ad(price=1500.0)])
    assert bazar.run_all() == (1, 1)          # relevantní, ale drahý
    assert session.query(Alert).count() == 0


def test_pipeline_sbazar_kandidati(session, monkeypatch):
    _trap(session)
    sb = sbazar.SbazarAd(
        ext_id="9", url="https://www.sbazar.cz/inzerat/9-hamaka",
        title="Houpací síť pro dva", price=800.0,
        locality="Brno", created="2026-08-19",
    )
    _wire(monkeypatch, ads_sbazar=[sb])
    assert bazar.run_all() == (1, 1)
    assert session.scalars(select(Listing)).one().source == Source.SBAZAR


def test_vyber_sekci_fallback(session, monkeypatch):
    trap = _trap(session)
    def boom(*a, **kw):
        raise ValueError("LLM mimo")
    monkeypatch.setattr(bazar.llm, "chat_json", boom)
    assert bazar.pick_sections(trap) == ["ostatni"]


def test_migrace_obsahuje_lm_criteria_id():
    assert any(
        t == "listing_matches" and c == "criteria_id"
        for t, c, _ in db._MIGRATIONS
    )


# --- API -------------------------------------------------------------------

def test_trap_listings_endpoint(session, monkeypatch):
    from fastapi.testclient import TestClient

    from trapline.api.main import app
    from trapline.config import settings as cfg

    monkeypatch.setattr(cfg, "app_password", "")
    trap = _trap(session)
    _wire(monkeypatch, ads_bazos=[_ad()])
    bazar.run_all()

    client = TestClient(app)
    rows = client.get(f"/api/criteria/{trap.id}/listings").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Hamaka DD SuperLight"
    assert row["bazar"] == "bazos"
    assert row["relevant"] is True
    assert row["condition"] == "used"
    assert row["gone"] is False

    # smazání pasti uklidí i bazarové vyhodnocení (FK)
    assert client.delete(f"/api/criteria/{trap.id}").status_code == 204
    assert session.query(ListingMatch).count() == 0
