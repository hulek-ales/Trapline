"""Discovery: feedy → kanonické produkty + nabídky + cenová historie.

Mechanická fáze pipeline z ADR-0003 — žádné LLM. Deduplikace primárně přes
EAN, bez něj fallback (brand, model_norm); nepřesnosti fallbacku řeší až
LLM párování ve fázi skóringu.

Běží v jednom vlákně na pozadí (feedy se stahují sekvenčně s pauzou,
crawler etiketa). Stav drží modul v paměti — stejný vzor jako self-update:
jednoduché, a při restartu se prostě spustí znovu.
"""

from __future__ import annotations

import gzip
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db
from .config import settings
from .crawlers import heureka_feed
from .crawlers.heureka_feed import FeedItem
from .models import FeedSource, Offer, PriceHistory, Product, Source

log = logging.getLogger("trapline.discovery")

_lock = threading.Lock()
_state: dict = {"running": False, "started": None, "finished": None, "log": []}


def status() -> dict:
    with _lock:
        return dict(_state, log=list(_state["log"]))


def _note(msg: str) -> None:
    log.info(msg)
    with _lock:
        _state["log"].append(msg)
        _state["log"] = _state["log"][-50:]


def _brand_model_norm(item: FeedItem) -> tuple[str, str]:
    brand = (item.manufacturer or "?").strip()
    norm = heureka_feed.normalize(item.name)
    brand_norm = heureka_feed.normalize(brand)
    if brand_norm and norm.startswith(brand_norm):
        norm = norm[len(brand_norm):].strip()
    return brand, norm[:160]


def _upsert_product(session: Session, item: FeedItem) -> Product:
    product = None
    if item.ean:
        product = session.scalars(
            select(Product).where(Product.ean == item.ean)
        ).first()
    brand, model_norm = _brand_model_norm(item)
    if product is None and model_norm:
        product = session.scalars(
            select(Product).where(
                Product.brand == brand, Product.model_norm == model_norm
            )
        ).first()
    if product is None:
        specs = dict(item.params)
        if item.description:
            # klíče s podtržítkem GUI nezobrazuje v parametrech; popis je
            # vstup pro LLM, _img náhled pro katalog
            specs["_popis"] = item.description
        if item.image:
            specs["_img"] = item.image
        product = Product(
            ean=item.ean,
            brand=brand,
            model=item.name[:160],
            model_norm=model_norm,
            title=item.name[:255],
            specs=specs,
        )
        session.add(product)
        session.flush()
        return product

    if item.ean and not product.ean:
        product.ean = item.ean
    # parametry slučuj, nové klíče vyhrávají nad chybějícími, existující nech
    merged = dict(item.params)
    if item.description:
        merged["_popis"] = item.description
    if item.image:
        merged["_img"] = item.image
    merged.update(product.specs or {})
    product.specs = merged
    return product


def _upsert_offer(
    session: Session, source: FeedSource, item: FeedItem, product: Product
) -> None:
    shop = source.name
    offer = session.scalars(
        select(Offer).where(
            Offer.source == Source.HEUREKA_FEED,
            Offer.shop == shop,
            Offer.sku == item.item_id,
        )
    ).first()
    if offer is None:
        offer = Offer(
            product_id=product.id,
            source=Source.HEUREKA_FEED,
            shop=shop,
            sku=item.item_id,
            url=item.url,
        )
        session.add(offer)
        session.flush()
    offer.url = item.url
    offer.active = True
    offer.last_checked = datetime.now(UTC)
    session.add(PriceHistory(offer_id=offer.id, price=item.price))


#: Kolik snapshotů na zdroj držet, než se nejstarší smažou.
SNAPSHOT_KEEP = 30


def _save_snapshot(source: FeedSource, raw: bytes) -> None:
    """Gzipni syrovou odpověď na disk (ADR-0004). Selhání discovery nezastaví."""
    if not settings.snapshot_dir:
        return
    try:
        directory = Path(settings.snapshot_dir) / str(source.id)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        (directory / f"{stamp}.xml.gz").write_bytes(gzip.compress(raw))
        for old in sorted(directory.glob("*.xml.gz"))[:-SNAPSHOT_KEEP]:
            old.unlink()
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot %s selhal: %s", source.name, exc)


def _fetch_items(source: FeedSource) -> list[heureka_feed.FeedItem]:
    raw = heureka_feed.fetch_raw(source.url)
    _save_snapshot(source, raw)
    return heureka_feed.parse(raw)


def run_source(session: Session, source: FeedSource) -> str:
    items = _fetch_items(source)
    kept = [i for i in items if heureka_feed.matches_filter(i, source.category_filter)]
    for item in kept:
        product = _upsert_product(session, item)
        _upsert_offer(session, source, item, product)
    result = f"ok, {len(kept)}/{len(items)} položek prošlo filtrem"
    source.last_run = datetime.now(UTC)
    source.last_status = result
    session.commit()
    return result


def _run_all() -> None:
    try:
        if not db.ensure_ready():
            _note("databáze není dostupná — běh se ruší")
            return
        with db.open_session() as session:
            sources = session.scalars(
                select(FeedSource).where(FeedSource.enabled).order_by(FeedSource.id)
            ).all()
            _note(f"start, {len(sources)} zdrojů")
            for i, source in enumerate(sources):
                if i:
                    time.sleep(settings.request_delay_s)
                try:
                    _note(f"{source.name}: {run_source(session, source)}")
                except Exception as exc:  # noqa: BLE001 — jeden zdroj nesmí shodit běh
                    session.rollback()
                    source.last_status = f"chyba: {exc}"
                    source.last_run = datetime.now(UTC)
                    session.commit()
                    _note(f"{source.name}: chyba: {exc}")
    finally:
        with _lock:
            _state["running"] = False
            _state["finished"] = datetime.now(UTC).isoformat()


def start() -> bool:
    """Spustí běh na pozadí. False = už běží."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(
            running=True,
            started=datetime.now(UTC).isoformat(),
            finished=None,
            log=[],
        )
    threading.Thread(target=_run_all, daemon=True, name="discovery").start()
    return True
