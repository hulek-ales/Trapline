"""Odhad tržní ceny.

Ústřední problém: bazary neposkytují transakční ceny, jen *nabídkové*. Prostý
medián aktivních inzerátů je systematicky nadsazený, protože předražené
inzeráty v datech visí měsíce, zatímco dobře naceněné zmizí za den — měřil bys
hlavně neprodejné zboží.

Řešení: váha klesající s dobou přežití inzerátu. Rychle zmizelý inzerát byl
blízko tržní ceny → vysoká váha.

Celý modul je čistý (bez DB, bez I/O), aby šel testovat na syntetických datech.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "PriceSample",
    "ReferenceResult",
    "survival_weight",
    "weighted_median",
    "winsorize",
    "percentile",
    "depreciation",
    "estimate_used_reference",
]

#: Poločas váhy v dnech. Inzerát živý 14 dní má poloviční váhu.
DEFAULT_HALFLIFE_DAYS = 14.0

#: Počet vzorků, při kterém empirii věříš z poloviny (shrinkage konstanta).
DEFAULT_SHRINKAGE_K = 8.0

#: Ztráta hodnoty hned po rozbalení, než začne roční odpis.
FIRST_YEAR_RETENTION = 0.75

#: Roční odpis po prvním roce.
ANNUAL_DECAY = 0.12

#: Podíl vzorků oříznutý z každého konce před výpočtem.
DEFAULT_WINSOR_FRAC = 0.10


@dataclass(slots=True)
class PriceSample:
    """Jeden datový bod pro odhad. ``days_alive`` je doba, po kterou byl
    inzerát viditelný; u dosud aktivních použij dobu od ``first_seen``.

    ``still_active`` odlišuje censored vzorky — inzerát, který visí 3 dny a
    ještě nezmizel, není důkaz o přeceněnosti.
    """

    price: float
    days_alive: float
    still_active: bool = False


@dataclass(slots=True)
class ReferenceResult:
    used_reference: float
    used_median: float | None
    used_n: int
    confidence: float
    prior: float


def survival_weight(
    days_alive: float,
    *,
    halflife: float = DEFAULT_HALFLIFE_DAYS,
    still_active: bool = False,
) -> float:
    """Váha vzorku podle doby, jak dlouho inzerát vydržel.

    Aktivní inzeráty dostanou váhu shora omezenou na 1.0 — ještě nevíme, jestli
    se prodají, takže je nesmíme odměňovat za krátký věk.
    """
    if days_alive < 0:
        raise ValueError("days_alive nesmí být záporné")
    # Poločas, ne časová konstanta: při days_alive == halflife musí vyjít 0.5.
    w = math.exp(-math.log(2.0) * days_alive / halflife)
    if still_active:
        # Censored vzorek: neumíme rozhodnout, tlumíme jeho vliv.
        w = min(w, 0.5)
    return w


def weighted_median(values: list[float], weights: list[float]) -> float:
    """Vážený medián — hodnota, kde kumulativní váha protne polovinu.

    Robustnější než vážený průměr; jeden šílený inzerát za 50 000 výsledek
    neposune.
    """
    if not values:
        raise ValueError("prázdný vstup")
    if len(values) != len(weights):
        raise ValueError("values a weights musí mít stejnou délku")

    pairs = sorted(zip(values, weights), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        raise ValueError("součet vah musí být kladný")

    half = total / 2.0
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def winsorize(
    values: list[float], frac: float = DEFAULT_WINSOR_FRAC
) -> list[float]:
    """Ořízne ``frac`` z každého konce.

    Cílí na šedé dovozce nahoře a na překlepy / příslušenství prodávané pod
    názvem produktu dole. Neodstraňuje, ale nahrazuje krajní hodnoty — zachová
    počet vzorků.
    """
    if not values:
        return []
    if not 0.0 <= frac < 0.5:
        raise ValueError("frac musí být v <0, 0.5)")

    ordered = sorted(values)
    n = len(ordered)
    k = int(n * frac)
    if k == 0:
        return ordered
    lo, hi = ordered[k], ordered[n - k - 1]
    return [min(max(v, lo), hi) for v in ordered]


def percentile(values: list[float], p: float) -> float:
    """Lineárně interpolovaný percentil, ``p`` v <0, 1>."""
    if not values:
        raise ValueError("prázdný vstup")
    if not 0.0 <= p <= 1.0:
        raise ValueError("p musí být v <0, 1>")

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    pos = p * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[int(pos)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def depreciation(age_years: float) -> float:
    """Podíl původní ceny, který si zboží drží po ``age_years`` letech.

    První rok spadne na 75 %, pak -12 % ročně. Kompresorové ledničky drží líp
    než průměr elektroniky — časem nahraď kalibrací z vlastních dat.
    """
    if age_years <= 0:
        return 1.0
    if age_years <= 1:
        return 1.0 - (1.0 - FIRST_YEAR_RETENTION) * age_years
    return FIRST_YEAR_RETENTION * (1.0 - ANNUAL_DECAY) ** (age_years - 1.0)


def estimate_used_reference(
    samples: list[PriceSample],
    *,
    retail_best: float | None,
    typical_age_years: float = 2.0,
    halflife: float = DEFAULT_HALFLIFE_DAYS,
    k: float = DEFAULT_SHRINKAGE_K,
    winsor_frac: float = DEFAULT_WINSOR_FRAC,
) -> ReferenceResult:
    """Odhad ceny použitého kusu — blend empirie a retailového prioru.

    Řeší cold start: při nula vzorcích jede čistě na odpisové křivce z
    ``retail_best``, s rostoucím ``n`` se prior plynule zapomíná. Žádné prahy,
    spojitý přechod.

    Vrací ``confidence`` = váha empirie (0..1), kterou používej jako podmínku
    pro alerting — nealertuj proti odhadu, jen proti datům.
    """
    prior = (
        retail_best * depreciation(typical_age_years)
        if retail_best is not None
        else 0.0
    )

    n = len(samples)
    if n == 0:
        if retail_best is None:
            raise ValueError("bez vzorků i bez retail_best nelze odhadnout")
        return ReferenceResult(
            used_reference=prior,
            used_median=None,
            used_n=0,
            confidence=0.0,
            prior=prior,
        )

    prices = winsorize([s.price for s in samples], winsor_frac)
    # winsorize vrací seřazené — váhy musíme spárovat podle stejného pořadí
    ordered = sorted(samples, key=lambda s: s.price)
    weights = [
        survival_weight(s.days_alive, halflife=halflife, still_active=s.still_active)
        for s in ordered
    ]

    median = weighted_median(prices, weights)

    if retail_best is None:
        return ReferenceResult(
            used_reference=median,
            used_median=median,
            used_n=n,
            confidence=1.0,
            prior=0.0,
        )

    alpha = n / (n + k)
    blended = alpha * median + (1.0 - alpha) * prior

    return ReferenceResult(
        used_reference=blended,
        used_median=median,
        used_n=n,
        confidence=alpha,
        prior=prior,
    )
