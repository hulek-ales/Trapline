"""Tenký klient Ollamy.

Jediný vstupní bod pro LLM v celém projektu. Structured output se vynucuje
přes ``format`` (JSON schema) — Ollama pak sampluje jen tokeny, které schéma
dovolí, takže odpověď jde vždy naparsovat.

Watcher LLM volat nesmí (ADR-0003); tenhle modul používá jen discovery
a skóring.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from .config import settings

log = logging.getLogger("trapline.llm")


def _base() -> str:
    return settings.ollama_url.rstrip("/")


def available_models(timeout: float = 5.0) -> list[str]:
    """Seznam modelů na serveru. Výjimky (síť) propadají volajícímu —
    slouží i jako test dosažitelnosti."""
    resp = httpx.get(f"{_base()}/api/tags", timeout=timeout)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


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
    resp = httpx.get(f"{_base()}/api/ps", timeout=timeout)
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
        resp = httpx.post(
            f"{_base()}/api/chat",
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
