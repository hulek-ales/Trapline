"""Stažení a parsování Heureka XML feedu eshopu (ADR-0003).

Feed je celý katalog obchodu — SHOPITEM s názvem, cenou, EAN, výrobcem
a parametry. Parsuje se do neutrálních FeedItem; o zakládání produktů
rozhoduje discovery, ne parser.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from ..config import settings

log = logging.getLogger("trapline.crawlers.heureka")

#: Strop velikosti feedu — pojistka proti omylem nakonfigurovanému URL.
MAX_BYTES = 100 * 1024 * 1024


@dataclass(slots=True)
class FeedItem:
    item_id: str
    name: str
    url: str
    price: float
    ean: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    description: str | None = None
    image: str | None = None
    params: dict = field(default_factory=dict)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _strip_html(raw: str, limit: int = 1500) -> str:
    """DESCRIPTION bývá HTML — pro LLM stačí čistý text, oříznutý."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _price(raw: str) -> float | None:
    """'5699,00' i '5 699.00' → 5699.0"""
    cleaned = raw.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize(text: str) -> str:
    """Bez diakritiky, malými písmeny, jen alfanumerika a mezery."""
    stripped = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def parse(xml_bytes: bytes) -> list[FeedItem]:
    items: list[FeedItem] = []
    root = ET.fromstring(xml_bytes)
    for el in root.iter("SHOPITEM"):
        name = _text(el.find("PRODUCTNAME")) or _text(el.find("PRODUCT"))
        url = _text(el.find("URL"))
        price = _price(_text(el.find("PRICE_VAT")))
        if not name or not url or price is None:
            continue

        params: dict = {}
        for par in el.iter("PARAM"):
            key = _text(par.find("PARAM_NAME"))
            val = _text(par.find("VAL"))
            if not key or not val:
                continue
            if key in params:  # opakované jméno (Barva, Funkce) → seznam
                prev = params[key]
                params[key] = prev + [val] if isinstance(prev, list) else [prev, val]
            else:
                params[key] = val

        ean = _text(el.find("EAN"))
        description = _strip_html(_text(el.find("DESCRIPTION")))
        items.append(
            FeedItem(
                item_id=_text(el.find("ITEM_ID")) or url,
                name=name,
                url=url,
                price=price,
                ean=ean if re.fullmatch(r"\d{8,14}", ean) else None,
                manufacturer=_text(el.find("MANUFACTURER")) or None,
                category=_text(el.find("CATEGORYTEXT")) or None,
                description=description or None,
                image=_text(el.find("IMGURL")) or None,
                params=params,
            )
        )
    return items


def fetch_raw(url: str) -> bytes:
    """Stáhne syrový feed. Chyby (síť) propadají volajícímu."""
    with httpx.Client(
        headers={"User-Agent": settings.user_agent},
        timeout=120,
        follow_redirects=True,
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()
        if len(resp.content) > MAX_BYTES:
            raise ValueError(f"feed přes {MAX_BYTES // 1024 // 1024} MB")
        return resp.content


def fetch(url: str) -> list[FeedItem]:
    """Stáhne a naparsuje feed. Chyby (síť, XML) propadají volajícímu."""
    return parse(fetch_raw(url))


def matches_filter(item: FeedItem, category_filter: str) -> bool:
    """Čárkou oddělené podřetězce; stačí shoda jednoho v kategorii či názvu."""
    terms = [normalize(t) for t in category_filter.split(",") if t.strip()]
    if not terms:
        return True
    haystack = normalize(f"{item.category or ''} {item.name}")
    return any(t in haystack for t in terms)
