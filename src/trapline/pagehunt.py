"""Crawler produktových stránek (ADR-0007): katalog i z obchodů bez feedu.

Druhá fáze lovu pro past: z výsledků SearXNG se vezmou přímo URL stránek,
stáhnou se transportní vrstvou (HTTP → při blokaci skutečný Chrome — projde
i Alza) a z JSON-LD se vytáhne Product. Co projde předfiltrem pasti, uloží
se do katalogu jako nabídka ``source=jsonld`` — stejný tvar jako ruční 🌐+,
takže obchůzka cenu obnovuje a skóring produkt třídí úplně automaticky.

Stránky bez Productu se zkusí rozbalit jako kategorie: JSON-LD ItemList
nese URL detailů (hloubka 1). Stropy drží zátěž na osobní úrovni: max
stránek na běh, max na doménu, pauzy mezi požadavky, respekt k robots.txt.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from datetime import UTC, datetime

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
    "heureka.cz", "zbozi.cz", "glami.cz", "favi.cz", "biano.cz",
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

#: Stropy jednoho běhu — osobní nasazení, ne plošný scraping.
MAX_PAGES = 30
MAX_PER_DOMAIN = 5
MAX_ITEMLIST_URLS = 10
#: Od kolika položek ItemList je stránka kategorie (její vlastní Product
#: markup je jen obecný souhrn — neukládat).
CATEGORY_MIN_ITEMS = 3

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
        # Stránka kategorie často nese i obecný Product („Hamaky — od 120 Kč")
        # — s větším ItemList ji ber jako mapu na detaily, ne jako produkt.
        if len(lists) >= CATEGORY_MIN_ITEMS:
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

        # Bez Productu: zkus stránku rozbalit jako kategorii (ItemList).
        for extra in lists[:MAX_ITEMLIST_URLS]:
            if (
                transport.domain_of(extra) == dom
                and extra not in seen
                and extra not in known
            ):
                seen.add(extra)
                queue.append(extra)

    feedhunt._note(
        f"crawler stránek: {pages} staženo, {found} produktů do katalogu"
    )
    return pages, found
