"""Extrakce schema.org Product z JSON-LD bloků (ADR-0006, stupeň 2 žebříku
„co z ní vytáhnout": feed → JSON-LD → selektor → LLM).

Většina českých eshopů (Alza, Datart, Mall…) nese v každé produktové stránce
``<script type="application/ld+json">`` s typem Product včetně ceny — je to
jejich SEO kanál pro Google, takže je udržovaný a stabilní. Parsujeme čistě
stdlib: žádný extruct, žádný BeautifulSoup.
"""

from __future__ import annotations

import html as htmlmod
import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("trapline.crawlers.jsonld")

_SCRIPT_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.S | re.I,
)


@dataclass(slots=True)
class JsonLdProduct:
    name: str
    brand: str = ""
    ean: str = ""
    sku: str = ""
    image: str = ""
    description: str = ""
    price: float | None = None
    currency: str = ""
    in_stock: bool | None = None
    url: str = ""
    #: Alternativní ceny dalších Offer bloků (varianty, jiní prodejci).
    other_prices: list[float] = field(default_factory=list)


def _iter_json(html: str):
    for m in _SCRIPT_RE.finditer(html):
        raw = m.group(1).strip()
        # Občas bývá JSON zabalený v HTML komentáři nebo CDATA.
        raw = re.sub(r"^\s*(<!--|//<!\[CDATA\[)", "", raw)
        raw = re.sub(r"(-->|//\]\]>)\s*$", "", raw).strip()
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            # Neescapované entity (&quot;) v ručně skládaném JSON-LD.
            try:
                yield json.loads(htmlmod.unescape(raw))
            except json.JSONDecodeError:
                log.debug("jsonld: nevalidní blok (%d B), přeskočen", len(raw))


def _walk(node):
    """Projde JSON-LD strom (seznamy i @graph) a vrací všechny dicty."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for value in (node.get("@graph"), node.get("mainEntity")):
            if value is not None:
                yield from _walk(value)


def _is_product(node: dict) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list):
        return False
    return any(isinstance(t, str) and "product" in t.lower() for t in types)


def _text(value) -> str:
    """Jméno z hodnoty, která bývá string, dict s name/url, nebo seznam."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("url") or ""
    return str(value).strip() if value is not None else ""


def _price(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _offers(node: dict) -> list[dict]:
    offers = node.get("offers")
    if isinstance(offers, dict):
        # AggregateOffer může nést vnořené konkrétní nabídky.
        nested = offers.get("offers")
        return [offers] + (nested if isinstance(nested, list) else [])
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def _availability(offer: dict) -> bool | None:
    value = offer.get("availability")
    if not isinstance(value, str):
        return None
    tail = value.rsplit("/", 1)[-1].lower()
    if tail in ("instock", "instoreonly", "onlineonly", "limitedavailability",
                "presale", "preorder"):
        return True
    if tail in ("outofstock", "soldout", "discontinued"):
        return False
    return None


def _from_node(node: dict) -> JsonLdProduct:
    prices: list[tuple[float, bool | None]] = []
    currency = ""
    url = ""
    for offer in _offers(node):
        raw = offer.get("price")
        if raw is None:
            raw = offer.get("lowPrice")  # AggregateOffer
        price = _price(raw)
        if price is not None and price > 0:
            prices.append((price, _availability(offer)))
        currency = currency or _text(offer.get("priceCurrency"))
        url = url or _text(offer.get("url"))

    ean = ""
    for key in ("gtin13", "gtin", "gtin14", "gtin8", "gtin12"):
        ean = _text(node.get(key))
        if ean:
            break

    best = min(prices, key=lambda p: p[0]) if prices else (None, None)
    return JsonLdProduct(
        name=_text(node.get("name")),
        brand=_text(node.get("brand")),
        ean=re.sub(r"\D", "", ean)[:14],
        sku=_text(node.get("sku")) or _text(node.get("mpn")),
        image=_text(node.get("image")),
        description=_text(node.get("description"))[:1500],
        price=best[0],
        currency=currency,
        in_stock=best[1],
        url=url or _text(node.get("url")),
        other_prices=sorted(p for p, _ in prices[1:]) if len(prices) > 1 else [],
    )


def parse(html: str) -> list[JsonLdProduct]:
    out = []
    for doc in _iter_json(html):
        for node in _walk(doc):
            if _is_product(node):
                product = _from_node(node)
                if product.name:
                    out.append(product)
    return out


def item_urls(html: str) -> list[str]:
    """URL produktů z JSON-LD ItemList (kategorie/výpisy obchodů).

    Kategorie s ItemList je pro crawler mapou na detaily — levnější a
    spolehlivější než hádat produktové odkazy z HTML.
    """
    urls: list[str] = []
    for doc in _iter_json(html):
        for node in _walk(doc):
            types = node.get("@type")
            if isinstance(types, str):
                types = [types]
            if not isinstance(types, list) or not any(
                isinstance(t, str) and "itemlist" in t.lower() for t in types
            ):
                continue
            elements = node.get("itemListElement")
            if not isinstance(elements, list):
                continue
            for el in elements:
                if not isinstance(el, dict):
                    continue
                url = el.get("url")
                if not url:
                    item = el.get("item")
                    if isinstance(item, dict):
                        url = item.get("url") or item.get("@id")
                    elif isinstance(item, str):
                        url = item
                if isinstance(url, str) and url.startswith("http") and url not in urls:
                    urls.append(url)
    return urls


def best(html: str) -> JsonLdProduct | None:
    """Hlavní produkt stránky: první blok s cenou, jinak první vůbec.

    Stránka detailu mívá právě jeden Product; víc jich bývá u „doporučujeme
    také" bloků — ty ale typicky cenu v JSON-LD nenesou.
    """
    products = parse(html)
    for product in products:
        if product.price is not None:
            return product
    return products[0] if products else None
