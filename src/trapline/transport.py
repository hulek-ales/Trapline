"""Transportní vrstva (ADR-0006): jak stáhnout stránku, ne co z ní vytáhnout.

Žebřík od nejlevnějšího stupně k nejdražšímu:

  1. přímý HTTP fetch s vlastním User-Agentem (httpx),
  2. skutečný Chrome přes browserless/chromium (``POST /content``) — nasazuje
     se jako samostatná appka na TrueNAS (``TrueNasBrowser.yaml``).

Když stupeň 1 vrátí blokaci (403/429, Cloudflare challenge, captcha…) nebo
selže na TLS fingerprintu (velcí prodejci resetují spojení ještě před HTTP),
spadne se automaticky na stupeň 2. Bez nakonfigurovaného browseru se blokace
poctivě ohlásí jako chyba — žádné tiché prázdno.

Součástí je i monitoring tichých selhání (ADR-0007): když extraktor ze
stažené stránky nic nevytáhne, volající uloží HTML přes ``save_failure``
do ``data/failures/`` k ruční inspekci — nikdy do DB.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import settings

log = logging.getLogger("trapline.transport")

#: HTTP stavy, po kterých má smysl eskalovat na browser.
_BLOCKED_STATUSES = {401, 403, 407, 429, 503}

#: Markery anti-bot stěn v těle odpovědi (lowercase substring).
_BLOCK_MARKERS = (
    "cf-chl",              # Cloudflare challenge
    "just a moment",
    "attention required",
    "captcha",
    "datadome",
    "px-captcha",          # PerimeterX
    "access denied",
    "unusual traffic",
)

_FAILURES_KEEP = 50


class TransportError(RuntimeError):
    """Stránku se nepodařilo stáhnout žádným dostupným stupněm."""


@dataclass(slots=True)
class Page:
    url: str
    final_url: str
    status: int
    text: str
    #: Který stupeň stránku přinesl: "http" | "browser".
    via: str


def looks_blocked(status: int, text: str) -> bool:
    if status in _BLOCKED_STATUSES:
        return True
    low = text[:20000].lower()
    return any(marker in low for marker in _BLOCK_MARKERS)


def fetch_http(url: str, timeout: float = 30.0) -> Page:
    resp = httpx.get(
        url,
        headers={"User-Agent": settings.user_agent},
        timeout=timeout,
        follow_redirects=True,
    )
    return Page(
        url=url, final_url=str(resp.url), status=resp.status_code,
        text=resp.text, via="http",
    )


def _browser_body(url: str, minimal: bool = False) -> dict:
    body = {
        "url": url,
        "bestAttempt": True,
        "gotoOptions": {"waitUntil": "networkidle2", "timeout": 45000},
        "rejectResourceTypes": ["image", "media", "font"],
    }
    if not minimal:
        # Volitelná pole novějších verzí browserless — starší build je při
        # validaci těla odmítne celé („must NOT have additional properties"),
        # proto je fetch_browser umí při 400 zahodit a zkusit to bez nich.
        body["blockConsentModals"] = True
        # Cloudflare/Turnstile challenge se v reálném Chromu často vyřeší
        # sám během pár sekund — počkej, až se objeví JSON-LD cílové
        # stránky. bestAttempt vrátí obsah i při vypršení čekání.
        body["waitForSelector"] = {
            "selector": 'script[type="application/ld+json"]',
            "timeout": 20000,
        }
    return body


def fetch_browser(url: str, timeout: float = 60.0) -> Page:
    """Stáhne stránku skutečným Chromem přes browserless ``POST /content``.

    Vrací finální HTML po vykonání JS. ``bestAttempt`` nechá browserless
    vrátit obsah i při vypršení goto timeoutu (pomalé stránky s trackery).
    """
    if not settings.browser_enabled:
        raise TransportError("browser není nakonfigurovaný (TRAPLINE_BROWSER_URL)")
    base = settings.browser_url.rstrip("/")
    params: dict[str, str] = {}
    if settings.browser_token:
        params["token"] = settings.browser_token
    # Stealth maskuje nejběžnější detekce headless Chromu (navigator.webdriver
    # a spol.) — bez něj velcí prodejci servírují challenge i browseru.
    params["launch"] = json.dumps({"stealth": True})
    resp = httpx.post(
        f"{base}/content", params=params, json=_browser_body(url), timeout=timeout
    )
    if resp.status_code == 400 and "additional properties" in resp.text:
        log.info("transport: browserless nezná volitelná pole, zkouším bez nich")
        resp = httpx.post(
            f"{base}/content", params=params,
            json=_browser_body(url, minimal=True), timeout=timeout,
        )
    if resp.status_code != 200:
        # Tělo bývá validace — přesně říká, který parametr se nelíbí.
        raise TransportError(
            f"browserless vrátil HTTP {resp.status_code}: {resp.text[:300]}"
        )
    if looks_blocked(200, resp.text):
        # I skutečný Chrome dostal anti-bot stěnu — ulož ji k inspekci a
        # ohlas poctivě, ať se to neplete s „stránka nenese JSON-LD".
        saved = save_failure(domain_of(url) + "-browser", resp.text)
        raise TransportError(
            "eshop blokuje i skutečný Chrome (challenge/captcha)"
            + (f"; HTML uloženo: {saved}" if saved else "")
        )
    return Page(url=url, final_url=url, status=200, text=resp.text, via="browser")


def fetch(url: str, prefer_browser: bool = False) -> Page:
    """Žebřík HTTP → browser. Vyhazuje TransportError, když selžou oba stupně
    (nebo jediný dostupný)."""
    if prefer_browser and settings.browser_enabled:
        return fetch_browser(url)

    http_problem: str | None = None
    try:
        page = fetch_http(url)
        if not looks_blocked(page.status, page.text):
            return page
        http_problem = f"blokace (HTTP {page.status})"
    except httpx.HTTPError as exc:
        # TLS-fingerprint blokace se projeví jako reset/timeout už tady.
        http_problem = f"{type(exc).__name__}: {exc}"

    if not settings.browser_enabled:
        raise TransportError(f"přímý fetch selhal ({http_problem}), browser vypnutý")
    log.info("transport: %s — %s, eskaluji na browser", url, http_problem)
    try:
        return fetch_browser(url)
    except (httpx.HTTPError, TransportError) as exc:
        # WARNING do logbufferu — detail musí být vidět v GUI (Procesy a
        # logy) i když HTTP odpověď cestou zamaskuje reverse proxy.
        log.warning("transport: browser pro %s selhal: %s", url, exc)
        raise TransportError(
            f"selhal HTTP ({http_problem}) i browser ({exc})"
        ) from None


def browser_status() -> dict:
    """Diagnostika pro GUI: je browserless vůbec dostupný a jaký Chrome nese?

    ``GET /json/version`` je standardní CDP endpoint, browserless ho proxuje.
    """
    if not settings.browser_enabled:
        return {"configured": False, "reachable": False}
    base = settings.browser_url.rstrip("/")
    params = {"token": settings.browser_token} if settings.browser_token else {}
    try:
        resp = httpx.get(f"{base}/json/version", params=params, timeout=10)
        if resp.status_code != 200:
            return {
                "configured": True, "reachable": False,
                "error": f"HTTP {resp.status_code}",
            }
        info = resp.json()
        return {
            "configured": True, "reachable": True,
            "browser": info.get("Browser") or "?",
        }
    except Exception as exc:  # noqa: BLE001 — diagnostika nesmí padat
        return {"configured": True, "reachable": False, "error": str(exc)}


def save_failure(tag: str, html: str) -> str | None:
    """Ulož HTML tichého selhání do ``<snapshot_dir>/failures/`` (gzip).

    Drží posledních pár kusů, vrací cestu (pro log), None při vypnutých
    snapshotech nebo chybě zápisu — monitoring nesmí shodit pipeline.
    """
    if not settings.snapshot_dir:
        return None
    try:
        folder = Path(settings.snapshot_dir) / "failures"
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-z0-9-]+", "-", tag.lower()).strip("-")[:80] or "page"
        path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}.html.gz"
        path.write_bytes(gzip.compress(html.encode("utf-8", "replace")))
        for old in sorted(folder.glob("*.html.gz"))[:-_FAILURES_KEEP]:
            old.unlink(missing_ok=True)
        return str(path)
    except OSError as exc:
        log.warning("transport: snapshot selhání se neuložil: %s", exc)
        return None


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host.removeprefix("www.")
