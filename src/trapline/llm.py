"""Tenký klient Ollamy — přímé, nebo přes Ollama proxy (ADR-0009).

Jediný vstupní bod pro LLM v celém projektu. Structured output se vynucuje
přes ``format`` (JSON schema) — Ollama pak sampluje jen tokeny, které schéma
dovolí, takže odpověď jde vždy naparsovat.

S vyplněným ``TRAPLINE_OLLAMA_KEY`` jede provoz přes proxy: každé volání
nese ``Authorization: Bearer opx_…`` a před inferencí se model objedná
u plánovače (``POST /mgmt/v1/models/load``) — GPU sdílíme s dalšími
klienty, takže se čeká ve frontě, dokud plánovač nevrátí ``loaded: true``,
a inference se pošle v okně ``hold_s``. Bez klíče se chová jako dřív:
holé ``/api/chat`` na Ollamu.

Watcher LLM volat nesmí (ADR-0003); tenhle modul používá jen discovery
a skóring.
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from .config import settings

log = logging.getLogger("trapline.llm")

#: Jak dlouho smí jedno volání plánovače blokovat (strop API je 300 s).
LOAD_POLL_S = 60.0

#: Proxy bez plánovače (starší verze, nebo URL míří na holou Ollamu):
#: po prvním 404 se /mgmt/v1 do restartu nezkouší — stejný princip jako
#: ``transport._browser_first``.
_scheduler_missing = False


class LlmBusy(RuntimeError):
    """GPU se ve lhůtě neuvolnilo, nebo proxy odmítla kvůli limitům."""


def _base() -> str:
    return settings.ollama_url.rstrip("/")


def _headers() -> dict[str, str]:
    if settings.ollama_key:
        return {"Authorization": f"Bearer {settings.ollama_key}"}
    return {}


def available_models(timeout: float = 5.0) -> list[str]:
    """Seznam modelů na serveru. Výjimky (síť) propadají volajícímu —
    slouží i jako test dosažitelnosti."""
    resp = httpx.get(f"{_base()}/api/tags", headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


def ensure_loaded(model: str, budget_s: float | None = None) -> float:
    """Objednej model u plánovače proxy a počkej, až sedí na GPU.

    Vrací ``hold_s`` — kolik sekund plánovač model podrží pro náš dotaz
    (0 = plánovač není, jede se naslepo jako dřív). Když GPU ve lhůtě
    ``budget_s`` nedostaneme, zvedne ``LlmBusy`` — volající si rozhodne,
    jestli počkat na další obchůzku, nebo to vzdát.
    """
    global _scheduler_missing
    if not settings.llm_proxy_enabled or _scheduler_missing:
        return 0.0
    budget = settings.llm_load_wait_s if budget_s is None else budget_s
    deadline = time.monotonic() + budget
    last_status = "?"
    while True:
        wait = max(0.0, min(LOAD_POLL_S, deadline - time.monotonic()))
        resp = httpx.post(
            f"{_base()}/mgmt/v1/models/load",
            json={"model": model, "wait_s": wait},
            headers=_headers(),
            timeout=wait + 30.0,
        )
        if resp.status_code in (404, 405):
            _scheduler_missing = True
            log.info("llm: proxy nemá plánovač (%d), jedu bez něj", resp.status_code)
            return 0.0
        if resp.status_code == 429:
            raise LlmBusy(f"proxy odmítla (429): {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("loaded"):
            return float(data.get("hold_s") or 0.0)
        status = data.get("status") or "?"
        if status != last_status:
            log.info("llm: %s — %s, čekám na GPU", model, status)
            last_status = status
        if time.monotonic() >= deadline:
            raise LlmBusy(
                f"model {model} se za {budget:.0f} s nedostal na GPU ({status})"
            )


def parse_content(content: str) -> dict:
    """Tolerantní parsování odpovědi modelu.

    Vynucený formát by měl vracet čistý JSON, ale přes proxy (open-webui)
    občas dorazí obalený v markdown plotě nebo s textem okolo — vytáhne se
    první JSON objekt. Prázdná odpověď je chyba volajícího requestu.
    """
    text = content.strip()
    if not text:
        raise ValueError("prázdná odpověď modelu")
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            return json.loads(text[start : end + 1])
        raise ValueError(f"odpověď modelu není JSON: {text[:200]!r}") from None


def running_models(timeout: float = 5.0) -> list[dict]:
    """Co právě běží na Ollama serveru (GET /api/ps) — název, velikost
    a kolik z modelu je ve VRAM. size_vram < size = část na CPU."""
    resp = httpx.get(f"{_base()}/api/ps", headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    return [
        {
            "name": m.get("name"),
            "size_mb": round(m.get("size", 0) / 1e6),
            "vram_mb": round(m.get("size_vram", 0) / 1e6),
            "vram_pct": round(
                100 * m.get("size_vram", 0) / m["size"]
            ) if m.get("size") else None,
        }
        for m in resp.json().get("models", [])
    ]


def proxy_status(timeout: float = 5.0) -> dict:
    """Co říká proxy o sobě a o GPU (``/mgmt/v1/health`` + ``models/status``).
    Jen s klíčem; bez něj vrací ``enabled: False``. Nesmí padat."""
    if not settings.llm_proxy_enabled:
        return {"enabled": False}
    out: dict = {"enabled": True}
    try:
        resp = httpx.get(
            f"{_base()}/mgmt/v1/health", headers=_headers(), timeout=timeout
        )
        out["reachable"] = resp.status_code == 200
        if resp.status_code == 401:
            out["error"] = "proxy klíč odmítnut (401) — zkontroluj TRAPLINE_OLLAMA_KEY"
            return out
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
            return out
    except Exception as exc:  # noqa: BLE001 — diagnostika nesmí padat
        return {**out, "reachable": False, "error": str(exc)}
    try:
        resp = httpx.get(
            f"{_base()}/mgmt/v1/models/status", headers=_headers(), timeout=timeout
        )
        if resp.status_code == 200:
            out["scheduler"] = resp.json()
    except Exception as exc:  # noqa: BLE001
        out["scheduler_error"] = str(exc)
    return out


def chat_json(
    system: str,
    user: str,
    schema: dict,
    model: str | None = None,
    timeout: float = 240.0,
    retries: int = 1,
) -> dict:
    """Jedno kolo chatu s vynuceným JSON výstupem podle schématu.
    Nevalidní/prázdnou odpověď jednou zopakuje — přes proxy se to stává."""
    model = model or settings.llm_main
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        # Při teplotě 0 je model deterministický — nevalidní odpověď by se
        # opakovala bajt po bajtu stejně. Retry proto jede s teplotou.
        temperature = 0 if attempt == 0 else 0.4
        # Nejdřív fronta na GPU, teprve pak dotaz — jinak by proxy dotaz
        # stejně zadržela, jen bez informace, co se děje.
        ensure_loaded(model)
        resp = httpx.post(
            f"{_base()}/api/chat",
            headers=_headers(),
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                # think:false — thinking modely jinak spálí celý výstupní
                # rozpočet na přemýšlení a content zůstane prázdný při
                # HTTP 200 (potvrzený Ollama bug, viz Kuchařka ollamachat.py;
                # přesně tenhle příznak ukázal ostrý běh na gemma4)
                "think": False,
                "format": schema,
                # num_predict: bez explicitního stropu se delší JSON přes
                # proxy usekne v půlce ("Unterminated string" z ostrého běhu)
                "options": {"temperature": temperature, "num_predict": 4096},
            },
            timeout=timeout,
        )
        if resp.status_code == 429:
            raise LlmBusy(f"proxy odmítla dotaz (429): {resp.text[:200]}")
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        try:
            return parse_content(content)
        except ValueError as exc:
            last_exc = exc
            # syrová odpověď do logu — bez ní se vadný výstup nedá diagnostikovat
            log.warning(
                "pokus %d selhal (%s); odpověď modelu: %r",
                attempt + 1, exc, content[:400],
            )
    raise last_exc  # type: ignore[misc]
