"""Zboží.cz — hlídání ceny konkrétního produktu napříč obchody.

Jediná robots-povolená cesta k datům Zboží.cz jsou detailové stránky
``/vyrobek/<slug>/`` (vyhledávání je v robots.txt zakázané, kategorie se
renderují až klientsky). Detail nese SSR JSON (__NEXT_DATA__) s minimální,
maximální i mediánovou cenou napříč VŠEMI obchody včetně velkých řetězců,
počty obchodů a datem uvedení na trh (vstup pro odpisovou křivku ADR-0002).

K produktu v katalogu se URL detailu připne jednorázově (GUI/API); obchůzka
pak cenu obnovuje. Etiketa: vlastní User-Agent, jeden request na produkt
za cyklus, pauzy mezi requesty.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from ..config import settings

log = logging.getLogger("trapline.crawlers.zbozi")

PRODUCT_URL_RE = re.compile(r"^https://www\.zbozi\.cz/vyrobek/[a-z0-9-]+/?$")

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


@dataclass(slots=True)
class ZboziDetail:
    name: str
    slug: str
    min_price: float
    max_price: float | None
    median_price: float | None
    offers_count: int
    shop_count: int
    cheapest_shop: str | None
    released: datetime | None


def _kc(halere) -> float | None:
    return round(halere / 100.0, 2) if isinstance(halere, (int, float)) else None


def parse_detail(html: str) -> ZboziDetail:
    """Vytáhne data produktu z SSR JSON. ValueError, když stránka nenese
    detail (přesměrování, změna struktury) — volající loguje a jede dál."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("stránka nenese __NEXT_DATA__")
    try:
        data = json.loads(m.group(1))["props"]["pageProps"]["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"nečekaná struktura dat: {exc}") from None
    if not data or "minPrice" not in data:
        raise ValueError("data neobsahují detail produktu")

    items = (data.get("offers") or {}).get("items") or []
    cheapest = None
    if items:
        best = min(items, key=lambda i: i.get("price", float("inf")))
        cheapest = (best.get("shop") or {}).get("name")

    released = None
    ts = data.get("releaseDateUTC")
    if isinstance(ts, (int, float)) and ts > 0:
        released = datetime.fromtimestamp(ts, UTC)

    min_price = _kc(data.get("minPrice"))
    if min_price is None:
        raise ValueError("detail bez ceny")
    return ZboziDetail(
        name=data.get("name") or "?",
        slug=data.get("normalizedName") or "",
        min_price=min_price,
        max_price=_kc(data.get("maxPrice")),
        median_price=_kc(data.get("medianPrice")),
        offers_count=(data.get("offers") or {}).get("offersCount") or 0,
        shop_count=(data.get("offers") or {}).get("shopCount") or 0,
        cheapest_shop=cheapest,
        released=released,
    )


def fetch_detail(url: str) -> ZboziDetail:
    if not PRODUCT_URL_RE.match(url):
        raise ValueError("URL není detail produktu na www.zbozi.cz/vyrobek/…")
    resp = httpx.get(
        url,
        headers={"User-Agent": settings.user_agent},
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return parse_detail(resp.text)
