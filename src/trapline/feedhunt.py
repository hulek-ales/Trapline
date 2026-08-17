"""Automatické hledání zdrojů: past → SearXNG → domény → oťukání feedů.

Ruční postup (vygooglit obchody, zkusit /heureka.xml) automatizovaný 1:1.
LLM odvodí z pasti hledací fráze, SearXNG vrátí eshopy, u každé domény se
zkusí obvyklé cesty veřejných Heureka feedů a nalezené feedy se spočítají
proti předfiltru pasti. Výsledek se zakládá jako VYPNUTÝ zdroj s poznámkou
— uživatel v GUI rozhodne, co povolit; nic se nestahuje bez jeho souhlasu.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from . import db, llm
from .config import settings
from .crawlers import heureka_feed
from .models import Criteria, FeedSource

log = logging.getLogger("trapline.feedhunt")

#: Obvyklé cesty veřejných feedů (Shoptet, Upgates, vlastní řešení).
FEED_PATHS = [
    "heureka/export/products.xml",
    "heureka.xml",
    "export/heureka.xml",
    "export/products.xml",
    "feed/heureka",
    "heureka/products.xml",
]

#: Agregátory, bazary, sociální sítě, zahraniční marketplaces — nemají
#: veřejný feed nebo do katalogu nepatří (ADR-0001).
DOMAIN_BLACKLIST = frozenset({
    "heureka.cz", "zbozi.cz", "alza.cz", "mall.cz", "datart.cz", "aukro.cz",
    "bazos.cz", "sbazar.cz", "allegro.cz", "allegro.pl", "amazon.de",
    "amazon.com", "aliexpress.com", "temu.com", "ebay.com", "ebay.de",
    "glami.cz", "favi.cz", "biano.cz", "srovnanicen.cz", "arukereso.hu",
    "facebook.com", "instagram.com", "youtube.com", "wikipedia.org",
    "seznam.cz", "google.com", "idnes.cz", "novinky.cz", "root.cz",
})

#: Kolik domén z výsledků hledání maximálně oťukat v jednom běhu.
MAX_CANDIDATES = 15

_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "dotazy": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["dotazy"],
}

_lock = threading.Lock()
_state: dict = {"running": False, "started": None, "finished": None, "log": []}


def status() -> dict:
    with _lock:
        return dict(_state, log=list(_state["log"]))


def _note(msg: str) -> None:
    log.info(msg)
    with _lock:
        _state["log"].append(msg)
        _state["log"] = _state["log"][-100:]


def derive_queries(trap: Criteria) -> list[str]:
    """LLM odvodí hledací fráze pro nalezení eshopů; při selhání fallback
    na název pasti."""
    try:
        out = llm.chat_json(
            "Z požadavků uživatele odvoď 3 až 5 krátkých českých frází pro "
            "vyhledání ESHOPŮ, které daný typ zboží prodávají (názvy kategorií "
            "zboží, ne vlastnosti). Např. pro přenosnou ledničku: autochladnička, "
            "kompresorová autochladnička eshop, chladicí box do auta.",
            f"Past: {trap.name}. Požadavky: {', '.join(trap.query_terms)}",
            _QUERY_SCHEMA,
        )
        queries = [q.strip() for q in out.get("dotazy", []) if q.strip()]
        if queries:
            return queries[:5]
    except Exception as exc:  # noqa: BLE001
        _note(f"LLM fráze selhaly ({exc}) — jedu z názvu pasti")
    return [trap.name]


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def search_domains(queries: list[str]) -> list[str]:
    """SearXNG → kandidátní domény, seřazené podle četnosti ve výsledcích."""
    counts: dict[str, int] = {}
    with httpx.Client(
        timeout=20, headers={"User-Agent": settings.user_agent}
    ) as client:
        for query in queries:
            try:
                resp = client.get(
                    f"{settings.searxng_url.rstrip('/')}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "language": "cs",
                        "categories": "general",
                    },
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
            except Exception as exc:  # noqa: BLE001
                _note(f"hledání „{query}“ selhalo: {exc}")
                continue
            for item in results:
                dom = _domain(item.get("url", ""))
                if not dom or any(dom.endswith(b) for b in DOMAIN_BLACKLIST):
                    continue
                counts[dom] = counts.get(dom, 0) + 1
            time.sleep(1.0)
    return sorted(counts, key=counts.get, reverse=True)


def probe_domain(domain: str) -> str | None:
    """Zkus obvyklé cesty feedu; stačí prvních pár KB s <SHOPITEM>."""
    with httpx.Client(
        timeout=12,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        for path in FEED_PATHS:
            url = f"https://{domain}/{path}"
            try:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        continue
                    if "xml" not in resp.headers.get("content-type", ""):
                        continue
                    head = b""
                    for chunk in resp.iter_bytes():
                        head += chunk
                        if len(head) > 65536:
                            break
                    if b"<SHOPITEM" in head:
                        return url
            except Exception:  # noqa: BLE001 — mrtvá doména, jedeme dál
                continue
            time.sleep(0.5)
    return None


def evaluate_feed(url: str, trap: Criteria) -> tuple[int, int]:
    """(celkem položek, kolik projde předfiltrem pasti)."""
    items = heureka_feed.fetch(url)
    if not trap.prefilter:
        return len(items), len(items)
    matching = sum(
        1 for i in items if heureka_feed.matches_filter(i, trap.prefilter)
    )
    return len(items), matching


def _run(criteria_id: int) -> None:
    try:
        if not db.ensure_ready():
            _note("databáze není dostupná — běh se ruší")
            return
        with db.open_session() as session:
            trap = session.get(Criteria, criteria_id)
            if trap is None:
                _note("past neexistuje")
                return
            known = {
                _domain(s.url)
                for s in session.scalars(select(FeedSource))
            }
            _note(f"past „{trap.name}“: odvozuji hledací fráze")
            queries = derive_queries(trap)
            _note("fráze: " + ", ".join(queries))
            domains = [d for d in search_domains(queries) if d not in known]
            domains = domains[:MAX_CANDIDATES]
            _note(f"kandidátů k oťukání: {len(domains)}")

            found = 0
            for i, domain in enumerate(domains):
                if i:
                    time.sleep(settings.request_delay_s)
                feed_url = probe_domain(domain)
                if not feed_url:
                    continue
                try:
                    total, matching = evaluate_feed(feed_url, trap)
                except Exception as exc:  # noqa: BLE001
                    _note(f"{domain}: feed nejde přečíst ({exc})")
                    continue
                if matching == 0:
                    _note(f"{domain}: feed OK, ale 0 položek odpovídá — přeskakuji")
                    continue
                session.add(FeedSource(
                    name=domain,
                    url=feed_url,
                    category_filter=trap.prefilter or "",
                    enabled=False,
                    last_status=(
                        f"návrh z pasti „{trap.name}“: {matching}/{total} "
                        "položek odpovídá — povol a spusť discovery"
                    ),
                ))
                session.commit()
                found += 1
                _note(f"{domain}: NALEZEN feed, {matching}/{total} položek odpovídá")
            _note(
                f"hotovo: {found} nových návrhů zdrojů"
                + (" — povol je v sekci Zdroje" if found else "")
            )
    finally:
        with _lock:
            _state["running"] = False
            _state["finished"] = datetime.now(UTC).isoformat()


def start(criteria_id: int) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(
            running=True,
            started=datetime.now(UTC).isoformat(),
            finished=None,
            log=[],
        )
    threading.Thread(
        target=_run, args=(criteria_id,), daemon=True, name="feedhunt"
    ).start()
    return True
