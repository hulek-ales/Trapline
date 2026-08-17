"""Seskupování barevných variant produktů do rodin.

Eshopy vedou každou barvu jako samostatný produkt s vlastním EAN
(BBFR-95XB / XS / XW = jedna lednička ve třech barvách). Pro hlídání cen
i skóring je to jeden model — v GUI se rodina zobrazuje jako jeden řádek.

Heuristika, ne LLM: z normalizovaného názvu se vyhodí slova barev a u tokenů
s číslicí se usekne poslední písmeno (95xb → 95x, 48w → 48). Velikostní řady
zůstávají oddělené, protože se liší číslem (30a vs 40a → 30 vs 40) nebo
dalšími slovy názvu (Dvouzónová…).
"""

from __future__ import annotations

import re

from .crawlers.heureka_feed import normalize

#: Slova barev (po normalizaci, tj. bez diakritiky) — vyhazují se z klíče.
_COLOUR_WORDS = frozenset([
    "cerna", "cerny", "bila", "bily", "stribrna", "stribrny", "zelena",
    "zeleny", "modra", "modry", "cervena", "cerveny", "seda", "sedy",
    "zluta", "zluty", "oranzova", "oranzovy", "hneda", "hnedy", "ruzova",
    "ruzovy", "fialova", "fialovy", "bezova", "bezovy", "grafitova",
    "grafitovy", "antracit", "antracitova", "black", "white", "silver",
    "green", "blue", "red", "grey", "gray",
])

#: Token s číslicí končící písmenem: usekni poslední písmeno (varianta barvy).
_TRAILING_LETTER = re.compile(r"^(\w*\d\w*?)[a-z]$")


def family_key(brand: str, title: str) -> str:
    """Klíč rodiny — stejný pro barevné varianty téhož modelu."""
    tokens = []
    for token in normalize(title).split():
        if token in _COLOUR_WORDS:
            continue
        m = _TRAILING_LETTER.match(token)
        tokens.append(m.group(1) if m else token)
    return f"{normalize(brand)}|{' '.join(tokens)}"


def family_title(titles: list[str]) -> str:
    """Zobrazovaný název rodiny: nejdelší společný prefix názvů variant,
    oříznutý na hranici slova."""
    if len(titles) == 1:
        return titles[0]
    prefix = titles[0]
    for title in titles[1:]:
        while not title.startswith(prefix):
            prefix = prefix[:-1]
    prefix = prefix.rstrip(" /-–")
    # neusekávat uprostřed slova
    if prefix and not prefix[-1].isspace():
        cut = prefix.rfind(" ")
        head = prefix[:cut] if cut > 0 else prefix
        # ale jen když by se neztratil kód modelu (např. BBFR-95X)
        tail = prefix[cut + 1:] if cut > 0 else ""
        if tail and not any(ch.isdigit() for ch in tail):
            prefix = head
    return prefix.rstrip(" /-–") or titles[0]
