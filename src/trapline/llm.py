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
        resp = httpx.post(
            f"{_base()}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": schema,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        try:
            return parse_content(content)
        except ValueError as exc:
            last_exc = exc
            log.warning("pokus %d: %s", attempt + 1, exc)
    raise last_exc  # type: ignore[misc]
