"""Aukro: vyhledávání nabídek z veřejné stránky výsledků.

Robots.txt Aukra zakazuje jen účet a košík; vyhledávání dovoluje. Oficiální
Public API je jen pro prodejce (ADR-0008), ale stránka výsledků je Angular
aplikace renderovaná na serveru a nese v ``<script id="ng-state">`` celý
stav včetně odpovědi vyhledávacího backendu — pro každou nabídku id, název,
cenu, „kup teď", konec aukce, lokalitu, atributy (stav zboží!) a cestu
kategorií. Prakticky strukturované API bez volání API a bez parsování HTML.

Detail nabídky má JSON-LD ``Product`` s popisem prodejce a
``offers.availability``: ``InStock`` = běží, ``Discontinued`` = skončila.
To je jediný spolehlivý signál „žije ještě?" — slovo „ukončeno" je i na
aktivní stránce v textech rozhraní.

Aukce vs. „kup teď": u aukce je ``price`` jen aktuální příhoz, který může
do konce několikanásobně vyrůst. Proto se u čisté aukce do názvu dává
prefix „Aukce:", aby to bylo vidět v GUI i v alertu, a v popisu je konec
aukce. Kde jde koupit hned, bere se „kup teď" cena.
"""

from __future__ import annotations

import html as htmlmod
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

from .. import fx
from ..config import settings

log = logging.getLogger("trapline.aukro")

BASE = "https://aukro.cz"
SEARCH_URL = f"{BASE}/vysledky-vyhledavani"

_STATE = re.compile(r'<script id="ng-state"[^>]*>(.*?)</script>', re.S)
_LD = re.compile(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", re.S)
#: Angular TransferState escapuje krátkými entitami, ne HTML standardem.
_NG_ESCAPES = {"&a;": "&", "&q;": '"', "&s;": "'", "&l;": "<", "&g;": ">"}


@dataclass(slots=True)
class AukroAd:
    ext_id: str
    url: str
    title: str
    #: V korunách. Aukce = aktuální příhoz; kde je „kup teď", tak ten.
    price: float | None
    locality: str
    description: str = ""
    auction: bool = False
    shipping: float = 0.0
    ends_at: str = ""


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=30,
        headers={"User-Agent": settings.user_agent, "Accept-Language": "cs"},
        follow_redirects=True,
    )


def _unescape_state(raw: str) -> str:
    for short, char in _NG_ESCAPES.items():
        raw = raw.replace(short, char)
    return htmlmod.unescape(raw)


def parse_state(html: str) -> dict | None:
    """Stav Angular aplikace vložený do stránky, nebo None."""
    m = _STATE.search(html)
    if not m:
        return None
    try:
        return json.loads(_unescape_state(m.group(1)))
    except json.JSONDecodeError as exc:
        log.warning("aukro: ng-state nejde přečíst: %s", exc)
        return None


def _amount(price: dict | None) -> float | None:
    if not isinstance(price, dict):
        return None
    try:
        amount = float(price.get("amount") or 0)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return fx.to_czk(amount, price.get("currency") or "CZK")


def _when(iso: str) -> str:
    """„12. 9. 19:45" z ISO času — do popisu pro člověka i LLM."""
    try:
        dt = datetime.fromisoformat(iso)
        return f"{dt.day}. {dt.month}. {dt:%H:%M}"
    except (TypeError, ValueError):
        return ""


def _description(item: dict, auction: bool, buy_now: float | None) -> str:
    bits: list[str] = []
    for attr in item.get("attributes") or []:
        name = (attr.get("attributeName") or "").strip()
        value = (attr.get("attributeValue") or "").strip()
        if name and value:
            bits.append(f"{name}: {value}")
    path = [c.get("name") for c in item.get("categoryPath") or [] if c.get("name")]
    if path:
        bits.append("Kategorie: " + " > ".join(path))
    if auction:
        when = _when(item.get("endingTime") or "")
        current = _amount(item.get("price"))
        bits.append(
            "Aukce, aktuální příhoz"
            + (f" {current:.0f} Kč" if current else "")
            + (f", končí {when}" if when else "")
        )
        if buy_now:
            bits.append(f"kup teď {buy_now:.0f} Kč")
    if item.get("freeShipping"):
        bits.append("doprava zdarma")
    if item.get("personalPickup"):
        bits.append("osobní odběr")
    seller = item.get("sellerLogin")
    if seller:
        bits.append(f"prodejce {seller}")
    return " · ".join(bits)[:2000]


def _from_item(item: dict) -> AukroAd | None:
    ext_id = str(item.get("itemId") or "")
    name = (item.get("itemName") or "").strip()
    if not ext_id or not name:
        return None
    buy_now = _amount(item.get("buyNowPrice")) if item.get("buyNowActive") else None
    auction = bool(item.get("auction")) and not buy_now
    price = buy_now or _amount(item.get("price"))
    with_ship = _amount(item.get("priceWithShipping"))
    shipping = 0.0
    if with_ship and price and with_ship > price:
        shipping = float(round(with_ship - price))
    slug = item.get("seoUrl") or "nabidka"
    locality = ", ".join(
        p for p in (item.get("location"), item.get("postcode")) if p
    )[:120]
    return AukroAd(
        ext_id=ext_id,
        url=f"{BASE}/{slug}-{ext_id}",
        title=(f"Aukce: {name}" if auction else name)[:255],
        price=price,
        locality=locality,
        description=_description(item, auction, buy_now),
        auction=auction,
        shipping=float(shipping),
        ends_at=item.get("endingTime") or "",
    )


def parse_search(html: str) -> list[AukroAd]:
    """Nabídky z vložené odpovědi ``searchItemsCommon``. Prázdné = nic
    nenalezeno, nebo se změnil tvar stránky (pak to řekne log)."""
    state = parse_state(html)
    if not state:
        return []
    cache = state.get("aukCache") or {}
    for key, entry in cache.items():
        if "searchItemsCommon" not in key:
            continue
        body = entry.get("b") if isinstance(entry, dict) else None
        content = (body or {}).get("content") if isinstance(body, dict) else None
        if not isinstance(content, list):
            continue
        return [ad for ad in (_from_item(i) for i in content) if ad is not None]
    log.warning("aukro: ve stavu stránky chybí výsledky hledání — změna webu?")
    return []


def search(phrase: str) -> list[AukroAd]:
    """Jedna stránka výsledků (Aukro jich na stránku dává 15–60), jen
    aktivní nabídky."""
    with _client() as client:
        resp = client.get(SEARCH_URL, params={"text": phrase})
        resp.raise_for_status()
        return parse_search(resp.text)


def parse_detail(html: str) -> tuple[str | None, bool]:
    """(popis prodejce, žije?) z JSON-LD ``Product``. Bez JSON-LD se
    nabídka nechá žít — nejistota nesmí inzerát pohřbít."""
    for block in _LD.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "Product":
            continue
        offers = data.get("offers") or {}
        availability = str(offers.get("availability") or "")
        alive = "Discontinued" not in availability and "SoldOut" not in availability
        text = " ".join(str(data.get("description") or "").split())
        return (text[:4000] or None), alive
    return None, True


def detail(url: str) -> tuple[str | None, bool]:
    """Detail nabídky. 404 = pryč; skončená aukce má 200 s Discontinued."""
    with _client() as client:
        resp = client.get(url)
        if resp.status_code == 404:
            return None, False
        resp.raise_for_status()
        return parse_detail(resp.text)
