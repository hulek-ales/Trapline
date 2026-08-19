"""Crawler produktových stránek (ADR-0007): katalog i z obchodů bez feedu.

Druhá fáze lovu pro past: z výsledků SearXNG se vezmou přímo URL stránek,
stáhnou se transportní vrstvou (HTTP → při blokaci skutečný Chrome — projde
i Alza) a z JSON-LD se vytáhne Product. Co projde předfiltrem pasti, uloží
se do katalogu jako nabídka ``source=jsonld`` — stejný tvar jako ruční 🌐+,
takže obchůzka cenu obnovuje a skóring produkt třídí úplně automaticky.

Stránky bez Productu se zkusí rozbalit jako kategorie: nejdřív JSON-LD
ItemList, a když chybí (Alza kreslí výpisy JS bez značek), heuristika nad
HTML — odkazy tvaru produktového detailu (…-d123.htm, /produkt/…) na téže
doméně + stránkování přes <link rel="next">. Stropy drží zátěž na osobní
úrovni: max stránek na běh, max na doménu, pauzy, respekt k robots.txt.
"""

from __future__ import annotations

import html as htmlmod
import logging
import re
import time
import urllib.robotparser
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import discovery, feedhunt, jsonld_watch, transport
from .config import settings
from .crawlers import jsonld
from .crawlers.heureka_feed import FeedItem, normalize
from .models import Criteria, Offer, PriceHistory, Source

log = logging.getLogger("trapline.pagehunt")

#: Agregátory a srovnávače (robots či duplicitní data), sociální sítě,
#: média. Na rozdíl od feedového blacklistu tu velcí prodejci CHYBÍ záměrně
#: — jejich stránky crawler přes browser zvládá a jsou hlavní cíl.
BLACKLIST = frozenset({
    "heureka.cz", "heureka.sk", "zbozi.cz", "glami.cz", "favi.cz", "biano.cz",
    "srovnanicen.cz", "arukereso.hu", "idealo.de", "pricemania.sk",
    "aukro.cz", "bazos.cz", "sbazar.cz", "allegro.cz", "allegro.pl",
    "facebook.com", "instagram.com", "youtube.com", "pinterest.com",
    "wikipedia.org", "seznam.cz", "google.com", "idnes.cz", "novinky.cz",
    "root.cz", "reddit.com", "aliexpress.com", "temu.com", "amazon.de",
    "amazon.com", "ebay.com", "ebay.de",
    # ponaučení z prvního ostrého běhu: archiv nese kopie stránek obchodů
    # s JSON-LD (falešné produkty s mrtvou cenou), github jen challenge
    "archive.org", "github.com", "gitlab.com", "stackoverflow.com",
    "medium.com",
})

#: Stropy jednoho běhu — osobní nasazení, ne plošný scraping. Vyšší limit
#: na doménu má smysl od chvíle, kdy umíme rozbalit kategorii velkého
#: řetězce na detaily (Alza kategorie = desítky produktů).
MAX_PAGES = 40
MAX_PER_DOMAIN = 12
MAX_ITEMLIST_URLS = 10
MAX_DETAIL_LINKS = 20
#: Od kolika položek ItemList je stránka kategorie (její vlastní Product
#: markup je jen obecný souhrn — neukládat).
CATEGORY_MIN_ITEMS = 3

#: URL cesty, které vypadají jako produktový detail. Záměrně konzervativní
#: — falešný kandidát stojí jen jeden (ohraničený) fetch, ale moc široký
#: vzor by budget domény spálil na článcích a filtrech.
_DETAIL_PATTERNS = (
    re.compile(r"-d\d+\.htm$"),      # alza.cz
    re.compile(r"/p/\d+"),           # mall.cz a spol.
    re.compile(r"/produkt[y]?/."),
    re.compile(r"/product[s]?/."),
    re.compile(r"/zbozi/."),
)

_HREF = re.compile(r'href="([^"]+)"')
_REL_NEXT_TAG = re.compile(r"<link\b[^>]*>", re.I)


def detail_links(html: str, base_url: str) -> list[str]:
    """Odkazy tvaru produktového detailu na téže doméně, v pořadí výskytu."""
    base_dom = transport.domain_of(base_url)
    out: list[str] = []
    for href in _HREF.findall(html):
        url = urljoin(base_url, htmlmod.unescape(href))
        if transport.domain_of(url) != base_dom:
            continue
        path = urlsplit(url).path
        if any(p.search(path) for p in _DETAIL_PATTERNS) and url not in out:
            out.append(url)
    return out


def next_page(html: str, base_url: str) -> str | None:
    """Další stránka výpisu z ``<link rel="next">`` (SEO standard výpisů)."""
    for tag in _REL_NEXT_TAG.findall(html):
        if 'rel="next"' not in tag and "rel='next'" not in tag:
            continue
        m = re.search(r'href=["\']([^"\']+)["\']', tag)
        if m:
            return urljoin(base_url, htmlmod.unescape(m.group(1)))
    return None

#: Cache robots.txt per doména na dobu života procesu.
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _robots_for(domain: str) -> urllib.robotparser.RobotFileParser | None:
    if domain in _robots:
        return _robots[domain]
    parser = urllib.robotparser.RobotFileParser()
    try:
        resp = httpx.get(
            f"https://{domain}/robots.txt",
            headers={"User-Agent": settings.user_agent},
            timeout=8,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            parser.parse(resp.text.splitlines())
        else:
            parser = None  # bez robots.txt platí „povoleno"
    except Exception:  # noqa: BLE001 — nedostupné robots neblokuje běh
        parser = None
    _robots[domain] = parser
    return parser


def allowed_by_robots(url: str) -> bool:
    parser = _robots_for(transport.domain_of(url))
    return parser is None or parser.can_fetch(settings.user_agent, url)


def _blacklisted(domain: str) -> bool:
    return any(domain == b or domain.endswith("." + b) for b in BLACKLIST)


def passes_prefilter(trap: Criteria, found: jsonld.JsonLdProduct) -> bool:
    terms = [normalize(t) for t in trap.prefilter.split(",") if t.strip()]
    if not terms:
        return False  # bez předfiltru se necrawluje — viz hunt_trap
    haystack = normalize(f"{found.name} {found.description}")
    return any(t in haystack for t in terms)


def _to_item(found: jsonld.JsonLdProduct, url: str) -> FeedItem:
    return FeedItem(
        item_id=jsonld_watch._sku_for(url),
        name=found.name,
        url=url,
        price=found.price or 0.0,
        ean=found.ean or None,
        manufacturer=found.brand or None,
        description=found.description or None,
        image=found.image or None,
    )


def _upsert_offer(
    session: Session, product_id: int, found: jsonld.JsonLdProduct, url: str
) -> None:
    offer = session.scalars(
        select(Offer).where(Offer.source == Source.JSONLD, Offer.url == url)
    ).first()
    if offer is None:
        offer = Offer(
            product_id=product_id,
            source=Source.JSONLD,
            shop=transport.domain_of(url),
            sku=jsonld_watch._sku_for(url),
        )
        session.add(offer)
    offer.url = url
    offer.active = True
    offer.last_checked = datetime.now(UTC)
    session.flush()
    session.add(PriceHistory(
        offer_id=offer.id,
        price=found.price,
        in_stock=found.in_stock if found.in_stock is not None else True,
    ))


def hunt_trap(session: Session, trap: Criteria, urls: list[str]) -> tuple[int, int]:
    """Projdi kandidátní URL pro past. Vrací (stránek staženo, produktů).

    ``urls`` jsou výsledky SearXNG z první fáze lovu — hledá se jednou,
    feedy i crawler jedou nad týmiž výsledky.
    """
    if not trap.prefilter:
        feedhunt._note(
            f"past „{trap.name}“ nemá předfiltr — crawler stránek se přeskakuje"
        )
        return 0, 0

    known = set(session.scalars(select(Offer.url)))
    queue: list[str] = []
    for url in urls:
        dom = transport.domain_of(url)
        if dom and not _blacklisted(dom) and url not in known:
            queue.append(url)

    pages = found = 0
    per_domain: dict[str, int] = {}
    seen = set(queue)

    def _enqueue(candidates: list[str], dom: str) -> None:
        for extra in candidates:
            if (
                transport.domain_of(extra) == dom
                and extra not in seen
                and extra not in known
            ):
                seen.add(extra)
                queue.append(extra)

    i = 0
    while i < len(queue) and pages < MAX_PAGES:
        url = queue[i]
        i += 1
        dom = transport.domain_of(url)
        if per_domain.get(dom, 0) >= MAX_PER_DOMAIN:
            continue
        if not allowed_by_robots(url):
            continue
        if pages:
            time.sleep(settings.request_delay_s)
        pages += 1
        per_domain[dom] = per_domain.get(dom, 0) + 1
        try:
            page = transport.fetch(url)
        except transport.TransportError as exc:
            feedhunt._note(f"{dom}: {exc}")
            continue

        product = jsonld.best(page.text)
        lists = jsonld.item_urls(page.text)
        # Stránka kategorie často nese i obecný Product („Hamaky — od 120 Kč",
        # „Kompresorové chladničky — od 4229 Kč"). Poznávací znamení: větší
        # ItemList, nebo chybějící značka/EAN/SKU (skutečný detail je má
        # prakticky vždy). Takovou stránku ber jako mapu, ne jako produkt.
        if product is not None and (
            len(lists) >= CATEGORY_MIN_ITEMS
            or not (product.brand or product.ean or product.sku)
        ):
            product = None
        if product is not None and product.price and product.name:
            if product.currency and product.currency.upper() not in ("CZK", "KČ"):
                continue
            if not passes_prefilter(trap, product):
                continue
            row = discovery._upsert_product(session, _to_item(product, url))
            _upsert_offer(session, row.id, product, url)
            session.commit()
            found += 1
            feedhunt._note(
                f"{dom}: {product.name} — {product.price:.0f} Kč"
                + (" (přes browser)" if page.via == "browser" else "")
            )
            continue

        # Bez Productu: rozbal stránku jako kategorii — nejdřív JSON-LD
        # ItemList, bez něj heuristika nad HTML (Alza a spol. kreslí výpisy
        # JS bez značek, ale odkazy na detaily v HTML jsou).
        if lists:
            _enqueue(lists[:MAX_ITEMLIST_URLS], dom)
        else:
            details = detail_links(page.text, page.final_url or url)
            _enqueue(details[:MAX_DETAIL_LINKS], dom)
            lists = details  # kvůli stránkování níže
        # Výpis s nálezy pokračuje na další stránce (<link rel="next">).
        if lists:
            nxt = next_page(page.text, page.final_url or url)
            if nxt:
                _enqueue([nxt], dom)

    feedhunt._note(
        f"crawler stránek: {pages} staženo, {found} produktů do katalogu"
    )
    return pages, found
