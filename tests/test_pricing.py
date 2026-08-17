
import pytest

from trapline.pricing.reference import (
    PriceSample,
    depreciation,
    estimate_used_reference,
    percentile,
    survival_weight,
    weighted_median,
    winsorize,
)
from trapline.pricing.scoring import DealInput, pickup_cost, score_deal

# --------------------------------------------------------------------------- #
# Váhy a statistika
# --------------------------------------------------------------------------- #

def test_survival_weight_klesa_s_dobou():
    assert survival_weight(0) == pytest.approx(1.0)
    assert survival_weight(14) == pytest.approx(0.5, abs=0.01)
    # 60 dní ≈ 4,3 poločasu → zbytková váha řádu jednotek procent
    assert survival_weight(60) < 0.06


def test_aktivni_inzerat_ma_tlumenou_vahu():
    """Inzerát živý 1 den, který ještě nezmizel, není důkaz o dobré ceně."""
    assert survival_weight(1, still_active=True) <= 0.5
    assert survival_weight(1, still_active=False) > 0.9


def test_weighted_median_ignoruje_vylet():
    values = [5000, 5200, 5100, 50000]
    weights = [1.0, 1.0, 1.0, 1.0]
    assert weighted_median(values, weights) < 6000


def test_winsorize_zachova_pocet():
    values = [100, 5000, 5100, 5200, 99000]
    out = winsorize(values, 0.2)
    assert len(out) == len(values)
    assert max(out) < 99000
    assert min(out) > 100


def test_percentile_interpolace():
    assert percentile([10, 20, 30, 40], 0.0) == 10
    assert percentile([10, 20, 30, 40], 1.0) == 40
    assert percentile([10, 20, 30, 40], 0.5) == pytest.approx(25.0)


def test_depreciation_monotonni():
    hodnoty = [depreciation(r) for r in (0, 0.5, 1, 2, 3, 5)]
    assert hodnoty == sorted(hodnoty, reverse=True)
    assert depreciation(1) == pytest.approx(0.75)


# --------------------------------------------------------------------------- #
# Jádro: nabídková vs. transakční cena
# --------------------------------------------------------------------------- #

def test_prezivsi_inzeraty_tahnou_odhad_dolu():
    """Dva levné inzeráty zmizely za den, tři drahé visí dva měsíce.
    Prostý medián by řekl 7000; vážený musí být výrazně níž."""
    samples = [
        PriceSample(price=5000, days_alive=1),
        PriceSample(price=5200, days_alive=2),
        PriceSample(price=7000, days_alive=60),
        PriceSample(price=7500, days_alive=70),
        PriceSample(price=8000, days_alive=90),
    ]
    res = estimate_used_reference(samples, retail_best=None)
    assert res.used_median < 6000, "vážený medián neodfiltroval neprodejné inzeráty"


def test_cold_start_jede_na_prioru():
    res = estimate_used_reference([], retail_best=10000, typical_age_years=2)
    assert res.used_n == 0
    assert res.confidence == 0.0
    assert res.used_reference == pytest.approx(10000 * depreciation(2))


def test_shrinkage_plynule_prechazi_na_empirii():
    """S rostoucím počtem vzorků musí confidence růst a odhad se blížit
    empirii, ne prioru."""
    prev_conf = -1.0
    for n in (1, 4, 8, 20, 50):
        samples = [PriceSample(price=4000, days_alive=3) for _ in range(n)]
        res = estimate_used_reference(samples, retail_best=10000)
        assert res.confidence > prev_conf
        prev_conf = res.confidence
    assert res.used_reference == pytest.approx(4000, rel=0.15)


def test_bez_dat_i_bez_retailu_selze():
    with pytest.raises(ValueError):
        estimate_used_reference([], retail_best=None)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def test_pickup_cost_roste_se_vzdalenosti():
    assert pickup_cost(None) == 0.0
    assert pickup_cost(0) == pytest.approx(150.0)
    assert pickup_cost(100) > pickup_cost(10)


def test_vzdaleny_inzerat_neni_vyhodny():
    """5200 v Ostravě (250 km) vs 5800 v Hradci (40 km)."""
    blizky = score_deal(
        DealInput(price=5800, distance_km=40, photo_count=3),
        used_reference=7000, retail_best=9000, confidence=0.9,
    )
    daleky = score_deal(
        DealInput(price=5200, distance_km=250, photo_count=3),
        used_reference=7000, retail_best=9000, confidence=0.9,
    )
    assert blizky.total_cost < daleky.total_cost
    assert blizky.score > daleky.score


def test_novy_kus_v_akci_zablokuje_alert():
    """Použitý 30 % pod used_median, ale nový je za srovnatelné peníze."""
    res = score_deal(
        DealInput(price=4800, photo_count=4),
        used_reference=7000, retail_best=5400, confidence=0.9,
    )
    assert res.should_alert is False
    assert "nový" in res.reason


def test_nizka_confidence_zablokuje_alert():
    res = score_deal(
        DealInput(price=3000, photo_count=4),
        used_reference=7000, retail_best=12000, confidence=0.1,
    )
    assert res.should_alert is False
    assert "dat" in res.reason


def test_red_flags_srazi_skore():
    cisty = score_deal(
        DealInput(price=4000, photo_count=5),
        used_reference=7000, retail_best=12000, confidence=0.9,
    )
    podezrely = score_deal(
        DealInput(price=4000, photo_count=0, red_flags=["platba předem", "nechladí"]),
        used_reference=7000, retail_best=12000, confidence=0.9,
    )
    assert cisty.should_alert is True
    assert podezrely.score < cisty.score
    assert podezrely.should_alert is False


def test_dobry_deal_projde():
    res = score_deal(
        DealInput(price=4200, distance_km=25, photo_count=6),
        used_reference=7000, retail_best=12000, confidence=0.85,
    )
    assert res.should_alert is True
    assert res.score > 0
    assert res.ratio_used < 0.8
