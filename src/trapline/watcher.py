"""Obchůzka: pravidelný cyklus discovery → skóring → reference → alerty.

„Nastraž jednou, pak jen obcházej" — APScheduler spouští cyklus každých
``WATCH_HOURS`` hodin (0 = vypnuto, zůstávají ruční tlačítka). První běh je
až za celý interval: self-update restartuje proces často a okamžitý běh po
každém restartu by zbytečně mlátil do zdrojů i GPU.

Cyklus čeká na doběhnutí každé fáze (discovery i skóring jedou ve vlastních
vláknech se zámky) — když fáze visí přes limit, cyklus to vzdá a nechá
zbytek na příště, aby se obchůzky nehromadily.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from . import alerts, db, discovery, references, scoring, zbozi_watch
from .config import settings

log = logging.getLogger("trapline.watcher")

_scheduler = None

#: Nejdéle čekáme na jednu fázi (velký katalog na pomalém GPU).
PHASE_TIMEOUT_S = 3 * 3600


def _wait(status_fn, name: str) -> bool:
    deadline = time.monotonic() + PHASE_TIMEOUT_S
    while status_fn()["running"]:
        if time.monotonic() > deadline:
            log.warning("obchůzka: fáze %s nedoběhla do limitu, vzdávám", name)
            return False
        time.sleep(10)
    return True


def run_cycle() -> None:
    log.info("obchůzka: start")
    if discovery.start():
        if not _wait(discovery.status, "discovery"):
            return
    else:
        log.info("obchůzka: discovery už běží, přeskakuji spuštění")
        if not _wait(discovery.status, "discovery"):
            return

    if scoring.start():
        if not _wait(scoring.status, "skóring"):
            return
    else:
        if not _wait(scoring.status, "skóring"):
            return

    try:
        zbozi_watch.refresh_all()
    except Exception:  # noqa: BLE001 — zbozi výpadek nesmí zastavit reference
        log.exception("obchůzka: obnova zbozi cen selhala")

    if not db.ensure_ready():
        log.warning("obchůzka: databáze nedostupná, reference a alerty vynechány")
        return
    with db.open_session() as session:
        references.recompute_all(session)
        alerts.evaluate_all(session)
    log.info("obchůzka: hotovo")


def start() -> None:
    global _scheduler
    if settings.watch_hours <= 0:
        log.info("obchůzka vypnutá (WATCH_HOURS=0)")
        return
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        run_cycle,
        "interval",
        hours=settings.watch_hours,
        id="obchuzka",
        name="Obchůzka (discovery → skóring → reference → alerty)",
        next_run_time=datetime.now(UTC) + timedelta(hours=settings.watch_hours),
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    log.info("obchůzka naplánována každých %s h", settings.watch_hours)


def jobs_overview() -> list[dict]:
    if _scheduler is None:
        return []
    out = []
    for job in _scheduler.get_jobs():
        out.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })
    return out
