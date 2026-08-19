"""Automatické hledání zdrojů: past → SearXNG → domény → oťukání feedů.

Ruční postup (vygooglit obchody, zkusit /heureka.xml) automatizovaný 1:1.
LLM odvodí z pasti hledací fráze, SearXNG vrátí eshopy, u každé domény se
zkusí obvyklé cesty veřejných Heureka feedů a nalezené feedy se spočítají
proti předfiltru pasti.

Silné nálezy (past má předfiltr a projde jím dost položek) se rovnou
ZAPÍNAJÍ — obchůzka je pak volá přes ``run_pending()`` a noví prodejci
i produkty přibývají bez klikání. Slabé nálezy a nálezy pastí bez
předfiltru zůstávají vypnuté návrhy: bez levného filtru by zapnutí feedu
vysypalo do katalogu celý sortiment obchodu a LLM by skóroval stovky
nesouvisejících položek.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
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

#: Auto-zapnutí: minimum položek prošlých předfiltrem pasti (slabší nález
#: zůstane vypnutým návrhem) a strop nově zapnutých zdrojů na jeden běh.
AUTO_ENABLE_MIN_MATCHING = 3
AUTO_ENABLE_MAX_PER_RUN = 5

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


def _prefilter_terms(trap: Criteria) -> list[str]:
    """Slova z předfiltru pasti — uživatelem ručně vybrané názvy kategorií
    („houpací síť", „lednic"…), často lepší hledací fráze než cokoli od LLM."""
    return [
        t.strip() for t in (trap.prefilter or "").split(",") if len(t.strip()) >= 3
    ]


def derive_queries(trap: Criteria) -> list[str]:
    """LLM odvodí hledací fráze pro nalezení eshopů; slova z předfiltru se
    přidávají vždy (LLM je dostane jako nápovědu, fallback je nese taky).

    Ponaučení z ostrého běhu: past „Hamaka" bez předfiltru v promptu vedla
    jen na fráze se slovem hamaka, zatímco obchody prodávají „houpací sítě".
    """
    queries: list[str] = []
    try:
        out = llm.chat_json(
            "Z požadavků uživatele odvoď 3 až 5 krátkých českých frází pro "
            "vyhledání ESHOPŮ, které daný typ zboží prodávají (názvy kategorií "
            "zboží, ne vlastnosti). Používej i synonyma — zboží se v obchodech "
            "často jmenuje jinak než v zadání. Např. pro přenosnou ledničku: "
            "autochladnička, kompresorová autochladnička eshop, chladicí box "
            "do auta.",
            f"Past: {trap.name}. Požadavky: {', '.join(trap.query_terms)}."
            + (f" Názvy kategorií zboží: {trap.prefilter}" if trap.prefilter else ""),
            _QUERY_SCHEMA,
        )
        queries = [q.strip() for q in out.get("dotazy", []) if q.strip()][:5]
    except Exception as exc:  # noqa: BLE001
        _note(f"LLM fráze selhaly ({exc}) — jedu z názvu pasti a předfiltru")
    if not queries:
        queries = [trap.name]
    for term in _prefilter_terms(trap):
        if term.lower() not in {q.lower() for q in queries}:
            queries.append(term)
    return queries[:7]


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


#: href na externí web ve stránce výsledků. Vnitřní odkazy SearXNG
#: (nastavení, další stránka…) jsou relativní, takže je regex mine.
_HTML_LINK = re.compile(r'href="(https?://[^"]+)"')


def extract_domains_from_html(html: str) -> list[str]:
    """Domény z HTML stránky výsledků — v pořadí prvního výskytu."""
    seen: list[str] = []
    for url in _HTML_LINK.findall(html):
        dom = _domain(url)
        if dom and dom not in seen:
            seen.append(dom)
    return seen


def _search_one(client: httpx.Client, query: str) -> list[str]:
    """Jeden dotaz na SearXNG → domény. Preferuje JSON API; když je
    zamčené (403 — defaultně vypnuté formats: json), sjede HTML výstup,
    který funguje vždy."""
    base = f"{settings.searxng_url.rstrip('/')}/search"
    params = {"q": query, "language": "cs", "categories": "general"}
    try:
        resp = client.get(base, params={**params, "format": "json"})
        resp.raise_for_status()
        return [
            _domain(item.get("url", ""))
            for item in resp.json().get("results", [])
        ]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 403:
            raise
    # JSON API zamčené → HTML fallback
    resp = client.get(base, params=params)
    resp.raise_for_status()
    return extract_domains_from_html(resp.text)


def search_domains(queries: list[str]) -> list[str]:
    """SearXNG → kandidátní domény, seřazené podle četnosti ve výsledcích."""
    counts: dict[str, int] = {}
    with httpx.Client(
        timeout=20, headers={"User-Agent": settings.user_agent}
    ) as client:
        for query in queries:
            try:
                domains = _search_one(client, query)
            except Exception as exc:  # noqa: BLE001
                _note(f"hledání „{query}“ selhalo: {exc}")
                continue
            for dom in domains:
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


def _hunt_trap(session, trap: Criteria) -> tuple[int, int]:
    """Jeden lov pro jednu past. Vrací (nových zdrojů, z toho auto-zapnutých).

    Commituje průběžně po každém nálezu a na konci razítkuje
    ``trap.last_hunt`` — throttle pro obchůzku.
    """
    known = {_domain(s.url) for s in session.scalars(select(FeedSource))}
    _note(f"past „{trap.name}“: odvozuji hledací fráze")
    queries = derive_queries(trap)
    _note("fráze: " + ", ".join(queries))
    domains = [d for d in search_domains(queries) if d not in known]
    domains = domains[:MAX_CANDIDATES]
    _note(f"kandidátů k oťukání: {len(domains)}")

    found = enabled = 0
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
        auto = (
            bool(trap.prefilter)
            and matching >= AUTO_ENABLE_MIN_MATCHING
            and enabled < AUTO_ENABLE_MAX_PER_RUN
        )
        if auto:
            status_text = (
                f"auto-zapnuto z pasti „{trap.name}“: {matching}/{total} "
                "položek odpovídá — vypni v sekci Zdroje, pokud nesedí"
            )
        elif not trap.prefilter:
            status_text = (
                f"návrh z pasti „{trap.name}“: {matching}/{total} položek — "
                "past nemá předfiltr, bez něj se auto-nezapíná (celý sortiment)"
            )
        else:
            status_text = (
                f"návrh z pasti „{trap.name}“: {matching}/{total} "
                "položek odpovídá — povol a spusť discovery"
            )
        session.add(FeedSource(
            name=domain,
            url=feed_url,
            category_filter=trap.prefilter or "",
            enabled=auto,
            last_status=status_text,
        ))
        session.commit()
        found += 1
        enabled += 1 if auto else 0
        _note(
            f"{domain}: NALEZEN feed, {matching}/{total} položek odpovídá"
            + (" → auto-zapnut" if auto else "")
        )
    # Naivní UTC jako zbytek schématu (server_default func.now()) — ať jde
    # sloupec porovnávat s cutoffem bez tanců kolem timezone.
    trap.last_hunt = datetime.now(UTC).replace(tzinfo=None)
    session.commit()
    _note(
        f"past „{trap.name}“ hotová: {found} nových zdrojů, "
        f"{enabled} auto-zapnutých"
        + (" — návrhy povol v sekci Zdroje" if found > enabled else "")
    )
    return found, enabled


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
            _hunt_trap(session, trap)
    finally:
        with _lock:
            _state["running"] = False
            _state["finished"] = datetime.now(UTC).isoformat()


def run_pending() -> tuple[int, int]:
    """Obchůzka: lov pro každou aktivní past, jejíž poslední hunt je starší
    než HUNT_HOURS. Běží synchronně (volá se z vlákna obchůzky) a vrací
    (nových zdrojů, auto-zapnutých). Nově zapnuté feedy stáhne discovery
    hned v témže cyklu."""
    if settings.hunt_hours <= 0 or not settings.searxng_url:
        return 0, 0
    with _lock:
        if _state["running"]:
            return 0, 0
        _state.update(
            running=True,
            started=datetime.now(UTC).isoformat(),
            finished=None,
            log=[],
        )
    found = enabled = 0
    try:
        if not db.ensure_ready():
            _note("databáze není dostupná — běh se ruší")
            return 0, 0
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            hours=settings.hunt_hours
        )
        with db.open_session() as session:
            traps = session.scalars(
                select(Criteria).where(Criteria.active)
            ).all()
            due = [
                t for t in traps
                if t.last_hunt is None or t.last_hunt < cutoff
            ]
            if not due:
                _note("žádná past nemá hunt na řadě")
                return 0, 0
            _note(f"automatický hunt: {len(due)} pastí na řadě")
            for trap in due:
                f, e = _hunt_trap(session, trap)
                found += f
                enabled += e
    finally:
        with _lock:
            _state["running"] = False
            _state["finished"] = datetime.now(UTC).isoformat()
    return found, enabled


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
