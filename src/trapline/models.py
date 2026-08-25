"""Datové schéma Trapline.

Klíčové oddělení:
  * ``Product``  – kanonický produkt (jeden model ledničky)
  * ``Offer``    – konkrétní nabídka nového kusu v eshopu (N na produkt, dlouhoživotná)
  * ``Listing``  – bazarový inzerát (efemérní, bez EAN, párovaný fuzzy)

Retail pipeline zapisuje do Offer/PriceHistory, bazarová do Listing/ListingMatch.
Obě se potkávají v PriceReference.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


#: SQLite autoinkrementuje jen INTEGER PRIMARY KEY — varianta drží BIGINT na
#: MariaDB a testům na SQLite nechá funkční autoincrement.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


# --------------------------------------------------------------------------- #
# Enumy
# --------------------------------------------------------------------------- #

class Source(enum.StrEnum):
    """Zdroj dat. Retail = cenová historie, secondary = inzeráty."""

    JSONLD = "jsonld"          # přímý scrape produktové stránky eshopu
    HEUREKA_FEED = "feed"      # XML feed eshopu
    ZBOZI = "zbozi"            # zbozi.cz detail produktu (min. cena napříč obchody)
    ALLEGRO = "allegro"        # oficiální REST API
    SBAZAR = "sbazar"
    BAZOS = "bazos"
    AUKRO = "aukro"

    @property
    def is_secondary(self) -> bool:
        return self in {Source.SBAZAR, Source.BAZOS, Source.AUKRO}


class Verdict(enum.StrEnum):
    """Uživatelský feedback z GUI."""

    LIKE = "like"
    DISLIKE = "dislike"
    NEUTRAL = "neutral"
    OWNED = "owned"            # už koupeno → přestat hlídat


class Condition(enum.StrEnum):
    NEW = "new"
    LIKE_NEW = "like_new"
    USED = "used"
    DAMAGED = "damaged"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Kritéria hledání
# --------------------------------------------------------------------------- #

class Criteria(Base):
    """Jedna "past" — sada požadavků, kterou agent obchází.

    ``hard`` jsou constraints převeditelné na SQL/Python filtr, ``soft`` jsou
    vážené preference pro scoring. Obojí jako JSON, aby šlo měnit bez migrace.

    Příklad hard:
        {"power_230v": true, "power_12v": true, "delta_t_min": 20,
         "capacity_l_min": 12, "compressor": true}
    Příklad soft:
        {"freezer": 3.0, "weight_kg_max": 15, "noise_db_max": 45}
    """

    __tablename__ = "criteria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    query_terms: Mapped[list] = mapped_column(JSON, default=list)
    #: Levný předfiltr katalogu (čárkou oddělené podřetězce jako u zdrojů):
    #: LLM skóring hodnotí jen produkty, které projdou. Prázdné = všechno.
    prefilter: Mapped[str] = mapped_column(String(500), default="")
    hard: Mapped[dict] = mapped_column(JSON, default=dict)
    soft: Mapped[dict] = mapped_column(JSON, default=dict)
    budget_max: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Kdy pro past naposledy běželo hledání zdrojů (feedhunt) — obchůzka ho
    #: opakuje nejdřív po HUNT_HOURS. NULL = ještě nikdy.
    last_hunt: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# --------------------------------------------------------------------------- #
# Kanonický produkt
# --------------------------------------------------------------------------- #

class Product(Base):
    """Jeden konkrétní model. Deduplikace primárně přes EAN, sekundárně
    přes (brand, model_norm)."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("ean", name="uq_products_ean"),
        Index("ix_products_brand_model", "brand", "model_norm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ean: Mapped[str | None] = mapped_column(String(14))
    brand: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(160))
    model_norm: Mapped[str] = mapped_column(String(160), index=True)
    title: Mapped[str] = mapped_column(String(255))

    #: Extrahované parametry (LLM structured output). Volný JSON, protože
    #: schéma se liší podle kategorie.
    specs: Mapped[dict] = mapped_column(JSON, default=dict)

    #: Rok uvedení na trh — vstup do odpisové křivky.
    released: Mapped[date | None] = mapped_column(Date)
    discontinued: Mapped[bool] = mapped_column(Boolean, default=False)

    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    offers: Mapped[list[Offer]] = relationship(back_populates="product")


# --------------------------------------------------------------------------- #
# Retail větev
# --------------------------------------------------------------------------- #

class Offer(Base):
    """Nabídka nového kusu u konkrétního prodejce. Dlouhoživotná — sledujeme
    ji v čase přes PriceHistory."""

    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("source", "shop", "sku", name="uq_offers_shop_sku"),
        Index("ix_offers_active", "product_id", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    source: Mapped[Source] = mapped_column(Enum(Source))
    shop: Mapped[str] = mapped_column(String(120))
    sku: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(1024))

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Šedý dovoz / přeprodejce — vyloučit z výpočtu reference.
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)

    last_checked: Mapped[datetime | None] = mapped_column(DateTime)

    product: Mapped[Product] = relationship(back_populates="offers")


class PriceHistory(Base):
    """Append-only. Nikdy needitovat, jen přidávat.

    Zapisuj i beze změny ceny (jednou denně) — jinak nejde spočítat, jak dlouho
    cena držela, a percentily vyjdou zkresleně.
    """

    __tablename__ = "price_history"
    __table_args__ = (Index("ix_ph_offer_ts", "offer_id", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"))
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    price: Mapped[float] = mapped_column(Float)
    shipping: Mapped[float] = mapped_column(Float, default=0.0)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)


# --------------------------------------------------------------------------- #
# Bazarová větev
# --------------------------------------------------------------------------- #

class Listing(Base):
    """Bazarový inzerát. Na rozdíl od Offer má krátký život a cena se prakticky
    nemění — hodnota je v ``first_seen``/``gone_at``, ne v historii.

    ``gone_at`` se plní diffem proti předchozímu snapshotu. Bez toho nejde
    odhadnout, které ceny byly reálné (viz pricing.reference).
    """

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "ext_id", name="uq_listings_source_ext"),
        Index("ix_listings_seen", "source", "first_seen"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    source: Mapped[Source] = mapped_column(Enum(Source))
    ext_id: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(1024))

    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float)
    shipping: Mapped[float] = mapped_column(Float, default=0.0)

    locality: Mapped[str | None] = mapped_column(String(120))
    distance_km: Mapped[float | None] = mapped_column(Float)

    seller_ref: Mapped[str | None] = mapped_column(String(120))
    photo_count: Mapped[int] = mapped_column(Integer, default=0)

    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    #: NULL = stále aktivní. Vyplní diff, když inzerát zmizí z výpisu.
    gone_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def days_alive(self) -> float:
        end = self.gone_at or self.last_seen
        return max((end - self.first_seen).total_seconds() / 86400.0, 0.0)


class ListingMatch(Base):
    """Výsledek LLM klasifikace inzerátu. Oddělené od Listing, aby šlo
    překlasifikovat bez ztráty syrových dat."""

    __tablename__ = "listing_matches"
    __table_args__ = (UniqueConstraint("listing_id", name="uq_lm_listing"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id"), index=True
    )
    #: Past, pro kterou byl inzerát vyhodnocen (v1: inzerát ↔ past přímo;
    #: párování na konkrétní produkt zůstává volitelné).
    criteria_id: Mapped[int | None] = mapped_column(ForeignKey("criteria.id"))

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    condition: Mapped[Condition] = mapped_column(
        Enum(Condition), default=Condition.UNKNOWN
    )
    age_years: Mapped[float | None] = mapped_column(Float)
    #: Např. ["chybí 230V adaptér", "nemrazí", "platba předem"]
    red_flags: Mapped[list] = mapped_column(JSON, default=list)
    model_used: Mapped[str | None] = mapped_column(String(80))
    classified_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# --------------------------------------------------------------------------- #
# Referenční ceny a scoring
# --------------------------------------------------------------------------- #

class PriceReference(Base):
    """Snapshot tržní situace pro jeden produkt. Přepočítává se scheduled
    jobem, ne při každém requestu."""

    __tablename__ = "price_reference"
    __table_args__ = (Index("ix_pr_product_ts", "product_id", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    retail_median: Mapped[float | None] = mapped_column(Float)
    #: 10. percentil = reálně dosažitelná nová cena, ne UVP.
    retail_best: Mapped[float | None] = mapped_column(Float)
    retail_n: Mapped[int] = mapped_column(Integer, default=0)

    #: Vážený medián inzerátů s decay podle doby přežití.
    used_median: Mapped[float | None] = mapped_column(Float)
    used_n: Mapped[int] = mapped_column(Integer, default=0)
    #: Výsledek shrinkage blendu — tohle používej pro scoring.
    used_reference: Mapped[float | None] = mapped_column(Float)
    #: 0..1, jak moc reference stojí na datech vs. na prioru.
    confidence: Mapped[float] = mapped_column(Float, default=0.0)


class FeedSource(Base):
    """Eshop, jehož Heureka XML feed discovery stahuje (ADR-0003).

    ``category_filter``: čárkou oddělené podřetězce; položka feedu projde,
    když aspoň jeden matchne v CATEGORYTEXT nebo PRODUCTNAME (bez ohledu na
    velikost písmen). Prázdné = projde všechno.
    """

    __tablename__ = "feed_sources"
    __table_args__ = (UniqueConstraint("url", name="uq_feed_sources_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(1024))
    category_filter: Mapped[str] = mapped_column(String(500), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    last_run: Mapped[datetime | None] = mapped_column(DateTime)
    #: Krátké shrnutí posledního běhu ("ok, 15 položek" / text chyby).
    last_status: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CriteriaMatch(Base):
    """Výsledek LLM skóringu produktu proti pasti (ADR-0003).

    Automatický verdikt; ruční přebití uživatelem žije v UserFeedback a má
    vždy přednost. ``criteria_rev`` drží hash zadání pasti v době vyhodnocení,
    aby šlo poznat zastaralé skóry bez porovnávání textů.
    """

    __tablename__ = "criteria_matches"
    __table_args__ = (
        UniqueConstraint("criteria_id", "product_id", name="uq_cm_pair"),
        Index("ix_cm_criteria_score", "criteria_id", "score"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    criteria_id: Mapped[int] = mapped_column(ForeignKey("criteria.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))

    #: 0–100, jak moc produkt vyhovuje zadání.
    score: Mapped[float] = mapped_column(Float, default=0.0)
    relevant: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Rozpad per požadavek: [{"pozadavek": "...", "splneno": true, "pozn": "..."}]
    breakdown: Mapped[list] = mapped_column(JSON, default=list)
    model_used: Mapped[str | None] = mapped_column(String(80))
    criteria_rev: Mapped[str | None] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (UniqueConstraint("product_id", name="uq_feedback_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    verdict: Mapped[Verdict] = mapped_column(Enum(Verdict), default=Verdict.NEUTRAL)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Alert(Base):
    """Deduplikace notifikací. Bez tohohle ti agent pošle stejný inzerát
    při každém běhu."""

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_alerts_dedup"),
        Index("ix_alerts_sent", "sent_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    #: např. "listing:sbazar:12345" nebo "offer:alza:XYZ:4990"
    dedup_key: Mapped[str] = mapped_column(String(255))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"))
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"))

    score: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    sent_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
