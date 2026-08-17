"""Vyhodnocení konkrétní nabídky proti referenci.

Zásada: neporovnávej holé ceny, ale *celkové náklady* proti referenci, a vrať
jediné seřaditelné číslo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["DealInput", "DealScore", "pickup_cost", "risk_penalty", "score_deal"]

#: Kč/km round-trip — palivo, opotřebení. Neřeší tvůj čas, na ten je FIXED.
COST_PER_KM = 8.0

#: Paušál za "zvednout se a jet" bez ohledu na vzdálenost.
PICKUP_FIXED = 150.0

#: Nad tímhle podílem retail_best se použité zboží nevyplatí vůbec.
MAX_RATIO_VS_NEW = 0.80

#: Minimální confidence reference, aby mělo smysl alertovat.
MIN_CONFIDENCE = 0.35

#: Penalizace za jednotlivé red flags (body ze skóre).
FLAG_WEIGHTS: dict[str, float] = {
    "platba předem": 40.0,
    "pouze poštou": 15.0,
    "nefunkční": 60.0,
    "nechladí": 60.0,
    "nemrazí": 25.0,
    "chybí adaptér": 12.0,
    "poškozeno": 20.0,
    "nový účet": 10.0,
}


@dataclass(slots=True)
class DealInput:
    price: float
    shipping: float = 0.0
    distance_km: float | None = None
    photo_count: int = 0
    red_flags: list[str] = field(default_factory=list)
    match_confidence: float = 1.0


@dataclass(slots=True)
class DealScore:
    score: float
    total_cost: float
    ratio_used: float
    ratio_new: float | None
    penalty: float
    should_alert: bool
    reason: str


def pickup_cost(distance_km: float | None) -> float:
    """Náklad na osobní odběr. Inzerát 200 km daleko není levnější nabídka,
    je to jiná nabídka."""
    if distance_km is None:
        return 0.0
    if distance_km < 0:
        raise ValueError("distance_km nesmí být záporné")
    return PICKUP_FIXED + 2.0 * distance_km * COST_PER_KM


def risk_penalty(red_flags: list[str], photo_count: int) -> float:
    """Bodová srážka za rizikové signály. Neškáluje s cenou — podvod za 3 000
    bolí stejně jako podvod za 8 000."""
    penalty = sum(FLAG_WEIGHTS.get(flag.lower(), 8.0) for flag in red_flags)
    if photo_count == 0:
        penalty += 25.0
    elif photo_count == 1:
        penalty += 8.0
    return penalty


def score_deal(
    deal: DealInput,
    *,
    used_reference: float,
    retail_best: float | None,
    confidence: float,
) -> DealScore:
    """Vrací skóre v bodech; kladné = pod trhem po odečtení rizika.

    Alert se spustí jen když projdou *všechny* tři podmínky: dost dat, pod
    trhem použitého, a zároveň dost pod cenou nového. Třetí podmínka je ta,
    na kterou se zapomíná — použitý kus 30 % pod used_median je pořád špatný
    nákup, když je nový v akci za srovnatelné peníze.
    """
    if used_reference <= 0:
        raise ValueError("used_reference musí být kladné")

    # Doprava a odběr se vylučují — bereš levnější z obou cest.
    delivery = min(
        deal.shipping if deal.shipping > 0 else float("inf"),
        pickup_cost(deal.distance_km) if deal.distance_km is not None else float("inf"),
    )
    if delivery == float("inf"):
        delivery = 0.0

    total = deal.price + delivery
    ratio_used = total / used_reference
    ratio_new = total / retail_best if retail_best else None

    penalty = risk_penalty(deal.red_flags, deal.photo_count)
    # Nejistý match = nejisté srovnání. Tlumíme, ne diskvalifikujeme.
    penalty += (1.0 - deal.match_confidence) * 30.0

    score = (1.0 - ratio_used) * 100.0 - penalty

    if confidence < MIN_CONFIDENCE:
        return DealScore(
            score, total, ratio_used, ratio_new, penalty, False,
            f"nedostatek dat (confidence {confidence:.2f})",
        )
    if ratio_new is not None and ratio_new > MAX_RATIO_VS_NEW:
        return DealScore(
            score, total, ratio_used, ratio_new, penalty, False,
            f"nový kus je dostupný za srovnatelnou cenu "
            f"({ratio_new:.0%} z retail_best)",
        )
    if score <= 0:
        return DealScore(
            score, total, ratio_used, ratio_new, penalty, False,
            "skóre po odečtení rizika není kladné",
        )

    return DealScore(
        score, total, ratio_used, ratio_new, penalty, True,
        f"{(1 - ratio_used):.0%} pod trhem",
    )
