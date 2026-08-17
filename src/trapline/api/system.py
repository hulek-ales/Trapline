"""Self-update z Gitu přes WEB UI (stejný mechanismus jako Kuchařka).

Pod Dockerem běží uvicorn v supervisor smyčce (``docker/entrypoint.sh``).
„Aktualizovat" jen ukončí proces; smyčka udělá ``git pull``, doinstaluje
závislosti a nastartuje novou verzi. Mimo Docker endpoint provede ``git pull``
a vyzve k ručnímu restartu.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from .. import logbuffer
from ..config import settings

router = APIRouter(prefix="/api/system", tags=["system"])


def _repo_dir() -> str:
    if settings.repo_dir:
        return settings.repo_dir
    # .../src/trapline/api/system.py → kořen repa = parents[3]
    return str(Path(__file__).resolve().parents[3])


def _git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", _repo_dir(), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return (r.stdout or r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return f"chyba: {exc}"


def _branch() -> str:
    b = _git("rev-parse", "--abbrev-ref", "HEAD")
    return b if b and "chyba" not in b else settings.repo_branch


def _supervised() -> bool:
    return os.environ.get("SUPERVISED") == "1"


def _guard() -> None:
    if not settings.update_enabled:
        raise HTTPException(
            403, "Aktualizace přes UI nejsou povolené (UPDATE_ENABLED)."
        )


@router.get("/log")
def system_log(
    limit: int = Query(100, le=500),
    level: str | None = Query(None, description="INFO|WARNING|ERROR"),
    contains: str | None = Query(None, description="podřetězec ve zprávě"),
):
    """Poslední logy z paměti appky — co dělají procesy na pozadí, bez
    přístupu k docker logs. Nejnovější první."""
    return {"items": logbuffer.get_records(limit=limit, level=level, contains=contains)}


@router.get("/version")
def version():
    """Co právě běží. GUI si tím ověřuje i to, že se po restartu změnil commit."""
    return {
        "enabled": settings.update_enabled,
        "supervised": _supervised(),
        "commit": _git("log", "-1", "--format=%h"),
        "date": _git("log", "-1", "--format=%ci"),
        "subject": _git("log", "-1", "--format=%s"),
        "branch": _branch(),
    }


@router.post("/check")
def check():
    """Fetchne origin a spočítá, o kolik commitů jsme pozadu. Nic nemění."""
    _guard()
    _git("fetch", "--quiet")
    branch = _branch()
    behind = _git("rev-list", "--count", f"HEAD..origin/{branch}")
    try:
        n = int(behind)
    except ValueError:
        n = 0
    return {
        "behind": n,
        "update_available": n > 0,
        "remote_subject": _git("log", "-1", "--format=%s", f"origin/{branch}"),
        "branch": branch,
    }


@router.post("/update")
def update():
    _guard()
    if _supervised():
        # Supervisor smyčka po ukončení procesu udělá pull + install + restart.
        # Dotyk .needs-build ji donutí přeinstalovat závislosti i když se
        # pyproject.toml nezměnil.
        Path(_repo_dir(), ".needs-build").touch()

        def _restart() -> None:
            time.sleep(0.6)  # ať stihne odejít HTTP odpověď
            os._exit(0)

        threading.Thread(target=_restart, daemon=True).start()
        return {"mode": "docker", "message": "Stahuji z Gitu a restartuji…"}

    out = _git("pull", "--ff-only")
    return {
        "mode": "manual",
        "output": out,
        "message": "Staženo. Restartuj API pro aplikaci změn.",
    }
