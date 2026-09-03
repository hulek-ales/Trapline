"""Sbazar: hledání a detaily přes frontend JSON API.

Pozn. k robots: robots.txt Sbazaru zakazuje roboty plošně. Uživatel po
seznámení s tím výslovně rozhodl Sbazar zapojit — osobní použití, jednotky
dotazů na obchůzku, stejné API a objem jako běžný prohlížeč (viz ADR-0008).
Držíme minimální provoz: jen fráze pastí, malé limity, pauzy.

API: ``/api/v1/items/search?phrase=…&limit=…`` (JSON se stránkováním),
``/api/v1/items/{id}`` (detail s popisem, lokalitou a stavem — smazané
inzeráty mají ``status`` mimo aktivní, což slouží k detekci zmizení).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import settings

BASE = "https://www.sbazar.cz"


@dataclass(slots=True)
class SbazarAd:
    ext_id: str
    url: str
    title: str
    price: float | None     # None = dohodou
    locality: str
    created: str            # ISO datum vložení/editace


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=20,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )


def _locality(loc: dict | None) -> str:
    loc = loc or {}
    parts = [loc.get("municipality"), loc.get("district")]
    return ", ".join(p for p in parts if p)[:120]


def _from_item(item: dict) -> SbazarAd:
    price = item.get("price")
    return SbazarAd(
        ext_id=str(item["id"]),
        url=f"{BASE}/inzerat/{item.get('seo_name') or item['id']}",
        title=(item.get("name") or "")[:255],
        price=None if item.get("price_by_agreement") else float(price or 0) or None,
        locality=_locality(item.get("locality")),
        created=item.get("sorting_date") or item.get("create_date") or "",
    )


def search(phrase: str, limit: int = 40) -> list[SbazarAd]:
    with _client() as client:
        resp = client.get(
            f"{BASE}/api/v1/items/search",
            params={"phrase": phrase, "limit": limit, "sort": "create_date"},
        )
        resp.raise_for_status()
        return [
            _from_item(item)
            for item in resp.json().get("results", [])
            if item.get("id") and item.get("name")
        ]


def detail(ext_id: str) -> tuple[str | None, bool]:
    """(popis, žije?). Smazaný/deaktivovaný inzerát → (None, False)."""
    with _client() as client:
        resp = client.get(f"{BASE}/api/v1/items/{ext_id}")
        if resp.status_code == 404:
            return None, False
        resp.raise_for_status()
        item = resp.json().get("result") or {}
    status = (item.get("status") or {})
    status_id = status.get("id") if isinstance(status, dict) else status
    alive = not item.get("deactivation_reason") and status_id in (None, 1, "active")
    return (item.get("description") or "")[:4000] or None, alive
