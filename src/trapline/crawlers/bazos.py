"""Bazoš: výpisy rubrik a detaily inzerátů.

robots.txt Bazoše zakazuje vyhledávání (``/search.php``, ``*hledat=``),
procházení rubrik povoluje. Proto se nehledá: prochází se výpis sekce
(subdoména, např. ``sport.bazos.cz``) od nejnovějších inzerátů a filtruje
se lokálně proti předfiltru pasti. Výpisy jsou řazené od nejnovějších,
takže pravidelná obchůzka prvních pár stránek zachytí všechno nové.

Detail inzerátu nese plný popis v meta ``og:description`` — spolehlivější
zdroj než HTML tělo, které Bazoš mění.
"""

from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass

from .. import transport

#: Prodejní sekce Bazoše (subdomény). Ne služby/práce/reality.
SECTIONS = frozenset({
    "auto", "deti", "dum", "elektro", "foto", "hudba", "knihy", "mobil",
    "motorky", "nabytek", "obleceni", "pc", "sport", "stroje", "vstupenky",
    "zvirata", "ostatni",
})

#: Kolik inzerátů má jedna stránka výpisu.
PAGE_SIZE = 20


@dataclass(slots=True)
class BazosAd:
    ext_id: str
    url: str
    title: str
    description: str        # krátký popis z výpisu
    price: float | None     # None = dohodou / v textu / zdarma
    locality: str
    listed: str             # datum vložení, jak ho píše výpis (25.8. 2026)


def section_url(section: str, offset: int = 0) -> str:
    base = f"https://{section}.bazos.cz/"
    return f"{base}{offset}/" if offset else base


_ITEM_SPLIT = re.compile(r'<div class="inzeraty inzeratyflex">')
_LINK = re.compile(r'href="(/inzerat/(\d+)/[^"]+\.php)"')
_TITLE = re.compile(r"<h2 class=nadpis><a[^>]*>([^<]+)</a>")
_DATE = re.compile(r"\[(\d+\.\d+\.\s*\d{4})\]")
_POPIS = re.compile(r"<div class=popis>(.*?)</div>", re.S)
_CENA = re.compile(r'class="inzeratycena"[^>]*>.*?>([^<]+)</span>', re.S)
_LOK = re.compile(r'class="inzeratylok"[^>]*>(.*?)</div>', re.S)


def _clean(text: str) -> str:
    return " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def parse_price(text: str) -> float | None:
    """„4 900 Kč" → 4900.0; Dohodou/V textu/Zdarma/Nabídněte → None."""
    digits = re.sub(r"[^\d]", "", text)
    return float(digits) if digits else None


def parse_listing(html: str, base: str) -> list[BazosAd]:
    ads: list[BazosAd] = []
    for chunk in _ITEM_SPLIT.split(html)[1:]:
        chunk = chunk[:4000]
        link = _LINK.search(chunk)
        title = _TITLE.search(chunk)
        if not link or not title:
            continue
        popis = _POPIS.search(chunk)
        cena = _CENA.search(chunk)
        lok = _LOK.search(chunk)
        date = _DATE.search(chunk)
        ads.append(BazosAd(
            ext_id=link.group(2),
            url=base.rstrip("/") + link.group(1),
            title=_clean(title.group(1))[:255],
            description=_clean(popis.group(1))[:500] if popis else "",
            price=parse_price(cena.group(1)) if cena else None,
            locality=" ".join(_clean(lok.group(1)).split())[:120] if lok else "",
            listed=date.group(1) if date else "",
        ))
    return ads


def fetch_listing(section: str, offset: int = 0) -> list[BazosAd]:
    page = transport.fetch(section_url(section, offset))
    return parse_listing(page.text, f"https://{section}.bazos.cz")


_OG_DESC = re.compile(
    r'property="og:description"\s+content="([^"]*)"|'
    r'content="([^"]*)"\s+property="og:description"'
)


def parse_detail(html: str) -> str | None:
    """Plný popis z og:description; None = inzerát už neexistuje."""
    m = _OG_DESC.search(html)
    if not m:
        return None
    text = htmlmod.unescape(m.group(1) or m.group(2) or "")
    # meta má tvar "Cena: …, Lokalita: …. Popis: …"
    if "Popis:" in text:
        text = text.split("Popis:", 1)[1]
    return text.strip()[:4000] or None


def fetch_detail(url: str) -> str | None:
    return parse_detail(transport.fetch(url).text)
