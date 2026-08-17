"""Kruhový buffer logů v paměti (po vzoru Kuchařky).

Slouží GUI k zobrazení, co se děje na pozadí, bez přístupu k `docker logs`.
Handler se věší na logger "trapline", takže zachytí discovery, skóring, LLM
i auth — ne ale access log uvicornu, ten je schválně mimo.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime

_MAX = 500
_records: deque[dict] = deque(maxlen=_MAX)
_lock = threading.Lock()


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                    timespec="seconds"
                ),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
        except Exception:  # noqa: BLE001
            return
        with _lock:
            _records.append(entry)


def install() -> None:
    root = logging.getLogger("trapline")
    if any(isinstance(h, _BufferHandler) for h in root.handlers):
        return
    root.addHandler(_BufferHandler())
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def get_records(
    limit: int = 100,
    level: str | None = None,
    contains: str | None = None,
) -> list[dict]:
    """Nejnovější první."""
    with _lock:
        items = list(_records)
    if level:
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
        threshold = order.get(level.upper(), 20)
        items = [r for r in items if order.get(r["level"], 20) >= threshold]
    if contains:
        needle = contains.lower()
        items = [r for r in items if needle in r["msg"].lower()]
    return list(reversed(items[-limit:]))
