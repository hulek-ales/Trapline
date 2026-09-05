"""Allegro: hledání nabídek přes oficiální REST API.

Jediný bazar s oficiálním API (ADR-0008), takže žádné scrapování: aplikace
se přihlásí grantem ``client_credentials`` a čte veřejný katalog nabídek.
Token má vlastní životnost, drží se v paměti a obnovuje se s rezervou.

Allegro účtuje ve zlotých — přepočet na koruny dělá ``fx`` podle kurzu
ČNB. Bez známého kurzu se nabídka přeskočí, protože porovnávat zlotý
s korunovým rozpočtem pasti je horší než nabídku nevidět.

API neposílá ve výpisu popis inzerátu; místo něj se skládá text
z parametrů nabídky (hlavně ``Stan`` — nová/použitá) a z ceny dopravy,
aby měl LLM co hodnotit. Detail nabídky je v API vyhrazený prodejci,
takže „žije ještě?" se zjišťuje ze stavu veřejné stránky nabídky.

Pozor: ``GET /offers/listing`` Allegro pouští jen **ověřeným aplikacím**.
Neověřená dostane 403 ``AccessDenied`` a ověření se nedá vyžádat z portálu.
Dokud aplikace ověřená není, konektor se po prvním takovém odmítnutí sám
utlumí do restartu — jinak by každá obchůzka posílala desítky požadavků,
o kterých dopředu víme, že skončí stejně (stejný princip jako
``transport._browser_first``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from .. import fx
from ..config import settings

log = logging.getLogger("trapline.allegro")

AUTH_URL = "https://allegro.pl/auth/oauth/token"
API_URL = "https://api.allegro.pl"
OFFER_URL = "https://allegro.pl/oferta/{id}"
ACCEPT = "application/vnd.allegro.public.v1+json"

#: O kolik dřív než vyprší se token obnoví — ať volání nespadne na hraně.
TOKEN_MARGIN_S = 60.0

_token: str = ""
_token_until: float = 0.0
#: Allegro odmítlo výpis kvůli neověřené aplikaci — do restartu se nezkouší.
_denied: str = ""


class AllegroError(RuntimeError):
    """Allegro odmítlo klíč nebo požadavek."""


class AllegroDenied(AllegroError):
    """Aplikace není ověřená — výpis nabídek je pro ni zavřený."""


@dataclass(slots=True)
class AllegroAd:
    ext_id: str
    url: str
    title: str
    #: Přepočteno na koruny; None = cena aukční/neznámá.
    price: float | None
    locality: str
    description: str = ""
    parameters: dict[str, str] = field(default_factory=dict)


def reset_token() -> None:
    """Zahoď token i útlum — pro testy a po změně klíčů."""
    global _token, _token_until, _denied
    _token, _token_until, _denied = "", 0.0, ""


def token() -> str:
    """Aplikační token. Drží se, dokud nevyprší."""
    global _token, _token_until
    if _token and time.monotonic() < _token_until:
        return _token
    if not settings.allegro_enabled:
        raise AllegroError("chybí TRAPLINE_ALLEGRO_CLIENT_ID/_SECRET")
    try:
        resp = httpx.post(
            AUTH_URL,
            params={"grant_type": "client_credentials"},
            auth=(settings.allegro_client_id, settings.allegro_client_secret),
            headers={"User-Agent": settings.allegro_user_agent},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise AllegroError(f"token nedostupný: {exc}") from exc
    if resp.status_code != 200:
        raise AllegroError(f"token odmítnut ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    _token = data.get("access_token", "")
    if not _token:
        raise AllegroError("odpověď tokenu neobsahuje access_token")
    _token_until = time.monotonic() + max(
        60.0, float(data.get("expires_in", 43200)) - TOKEN_MARGIN_S
    )
    return _token


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token()}",
        "Accept": ACCEPT,
        "User-Agent": settings.allegro_user_agent,
    }


def _price_czk(offer: dict) -> float | None:
    price = ((offer.get("sellingMode") or {}).get("price")) or {}
    try:
        amount = float(price.get("amount"))
    except (TypeError, ValueError):
        return None
    return fx.to_czk(amount, price.get("currency") or "PLN")


def _locality(offer: dict) -> str:
    loc = offer.get("location") or {}
    parts = ["Polsko", loc.get("city"), loc.get("province")]
    return ", ".join(p for p in parts if p)[:120]


def _parameters(offer: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for param in offer.get("parameters") or []:
        name = (param.get("name") or "").strip()
        values = param.get("values") or []
        if name and values:
            out[name] = ", ".join(str(v) for v in values)[:200]
    return out


def _description(offer: dict, params: dict[str, str]) -> str:
    """Náhrada popisu: parametry nabídky + doprava. Výpis API popis nemá."""
    bits = [f"{k}: {v}" for k, v in params.items()]
    delivery = offer.get("delivery") or {}
    lowest = delivery.get("lowestPrice") or {}
    if delivery.get("availableForFree"):
        bits.append("doprava zdarma")
    elif lowest.get("amount"):
        czk = fx.to_czk(float(lowest["amount"]), lowest.get("currency") or "PLN")
        if czk is not None:
            bits.append(f"doprava od {czk:.0f} Kč")
    return " · ".join(bits)[:2000]


def _from_offer(offer: dict) -> AllegroAd | None:
    ext_id = str(offer.get("id") or "")
    name = (offer.get("name") or "").strip()
    if not ext_id or not name:
        return None
    params = _parameters(offer)
    return AllegroAd(
        ext_id=ext_id,
        url=OFFER_URL.format(id=ext_id),
        title=name[:255],
        price=_price_czk(offer),
        locality=_locality(offer),
        description=_description(offer, params),
        parameters=params,
    )


def search(phrase: str, limit: int = 30) -> list[AllegroAd]:
    """Veřejný výpis nabídek k frázi. Ceny už v korunách."""
    global _denied
    if _denied:
        raise AllegroDenied(_denied)
    resp = httpx.get(
        f"{API_URL}/offers/listing",
        params={"phrase": phrase, "limit": max(1, min(limit, 60))},
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code == 401:
        reset_token()
        raise AllegroError("token neplatný (401) — klíč nebo scope aplikace")
    if resp.status_code == 403:
        _denied = (
            "Allegro pouští výpis nabídek jen ověřeným aplikacím (403 "
            "AccessDenied) — do restartu appky se už nezkouší"
        )
        log.warning("allegro: %s", _denied)
        raise AllegroDenied(_denied)
    if resp.status_code != 200:
        raise AllegroError(f"hledání selhalo ({resp.status_code}): {resp.text[:200]}")
    items = resp.json().get("items") or {}
    offers = list(items.get("promoted") or []) + list(items.get("regular") or [])
    ads = [ad for ad in (_from_offer(o) for o in offers) if ad is not None]
    skipped = len(offers) - len(ads)
    if skipped:
        log.info("allegro: %d nabídek bez id/názvu přeskočeno", skipped)
    return ads


def alive(ext_id: str) -> bool:
    """Žije ještě nabídka? Detail nabídky je v API jen pro prodejce, takže
    se kouká na stav veřejné stránky. Jen jasné 404 znamená „pryč"; při
    jakékoli nejistotě (blokace, výpadek) se nabídka nechá žít."""
    try:
        resp = httpx.head(
            OFFER_URL.format(id=ext_id),
            headers={"User-Agent": settings.allegro_user_agent},
            timeout=20,
            follow_redirects=True,
        )
    except Exception:  # noqa: BLE001
        return True
    return resp.status_code != 404


def status(phrase: str = "lodówka turystyczna") -> dict:
    """Diagnostika pro GUI: projde klíč, jede kurz, vrací API nabídky?

    Nesmí padat — smyslem je pojmenovat, kde to vázne, ne shodit endpoint.
    """
    out: dict = {
        "configured": settings.allegro_enabled,
        "user_agent": settings.allegro_user_agent,
        "pln_czk": fx.rate("PLN"),
    }
    if not settings.allegro_enabled:
        out["error"] = "vyplň TRAPLINE_ALLEGRO_CLIENT_ID a _SECRET"
        return out
    try:
        token()
    except Exception as exc:  # noqa: BLE001
        out["token"] = False
        out["error"] = str(exc)
        return out
    out["token"] = True
    try:
        ads = search(phrase, 5)
    except AllegroDenied as exc:
        out["denied"] = True
        out["error"] = str(exc)
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        return out
    out["found"] = len(ads)
    out["sample"] = [
        {"title": ad.title, "price": ad.price, "url": ad.url} for ad in ads[:3]
    ]
    return out
