"""Přepočet cizích měn na koruny podle kurzu ČNB.

Allegro účtuje ve zlotých, ale rozpočet pasti, alerty i katalog jsou
v korunách — bez převodu by se polská nabídka porovnávala s českým
rozpočtem jako by zlotý byla koruna.

ČNB publikuje denní kurzovní lístek jako prostý text bez klíče a bez
limitů. Kurz se drží v paměti do konce dne; když ČNB nedosáhneme, jede se
na posledním známém kurzu. Dokud žádný nemáme, funkce vrátí ``None`` —
to je pro volajícího pokyn nabídku přeskočit, ne si cenu domyslet.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx

from .config import settings

log = logging.getLogger("trapline.fx")

CNB_URL = (
    "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/"
    "kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
)

#: {kód měny: kurz za jednotku v Kč} a den, ke kterému platí.
_rates: dict[str, float] = {}
_rates_day: date | None = None


def _parse(text: str) -> dict[str, float]:
    """Kurzovní lístek → {kód: kurz za 1 jednotku}.

    Formát: hlavička s datem, hlavička sloupců a pak řádky
    ``Polsko|zlotý|1|PLN|5,432`` — množství je u slabších měn 100.
    """
    out: dict[str, float] = {}
    for line in text.splitlines()[2:]:
        parts = line.split("|")
        if len(parts) != 5:
            continue
        try:
            amount = float(parts[2].replace(",", "."))
            rate = float(parts[4].replace(",", "."))
        except ValueError:
            continue
        if amount > 0:
            out[parts[3].strip().upper()] = rate / amount
    return out


def refresh() -> bool:
    """Stáhni kurzovní lístek. False = nepodařilo se (kurzy zůstávají)."""
    global _rates, _rates_day
    try:
        resp = httpx.get(
            CNB_URL, timeout=20, headers={"User-Agent": settings.user_agent}
        )
        resp.raise_for_status()
        rates = _parse(resp.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("fx: kurzy ČNB nedostupné (%s)", exc)
        return False
    if not rates:
        log.warning("fx: kurzovní lístek ČNB nešel přeparsovat")
        return False
    _rates, _rates_day = rates, date.today()
    return True


def rate(currency: str) -> float | None:
    """Kolik korun stojí jedna jednotka měny. None = kurz neznáme."""
    currency = currency.strip().upper()
    if currency in {"CZK", "KČ", "KC"}:
        return 1.0
    if _rates_day != date.today():
        refresh()
    return _rates.get(currency)


def to_czk(amount: float, currency: str) -> float | None:
    """Cena v korunách, zaokrouhlená na celé. None = kurz neznáme."""
    factor = rate(currency)
    return None if factor is None else round(amount * factor)
