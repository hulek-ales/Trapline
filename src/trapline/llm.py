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


def chat_json(
    system: str,
    user: str,
    schema: dict,
    model: str | None = None,
    timeout: float = 240.0,
) -> dict:
    """Jedno kolo chatu s vynuceným JSON výstupem podle schématu."""
    model = model or settings.llm_main
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
    content = resp.json()["message"]["content"]
    return json.loads(content)
