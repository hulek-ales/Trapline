# ADR-0005: Zboží.cz jako cesta k cenám velkého retailu

**Status:** přijato
**Datum:** 2026-08-18

## Kontext

Velké řetězce (Alza, Datart) blokují roboty na úrovni TLS otisku — přímý
crawl je mimo hru (ADR-0001) a obcházení otiskem jsme vědomě odmítli.
Jejich ceny ale agreguje Zboží.cz (Seznam). Empirický průzkum:

| cesta | SSR data | robots.txt |
|---|---|---|
| `/hledej/` (vyhledávání) | ano, kompletní | **zakázáno** |
| `/vyrobek/<slug>/` (detail) | ano, kompletní | povoleno |
| kategorie, `/_next/data/` | `data: null` | povoleno |
| `/api/` (frontend API) | — | **explicitně povoleno**, kontrakt nezjištěn (POST) |
| sitemapy | úplné, celý katalog | povoleno |

Oficiální Zboží API skončilo 16. 3. 2026 (přesun do Sklik, čistě
inzerentské).

## Rozhodnutí

**Hlídáme detailové stránky konkrétních produktů** — jediná cesta, která
je současně robots-povolená a nese data. Detail obsahuje min/max/mediánovou
cenu napříč všemi obchody (včetně velkých řetězců), počty obchodů a datum
uvedení na trh (vstup pro odpisovou křivku ADR-0002).

* URL detailu se k produktu připíná jednorázově (GUI tlačítko „Z+" /
  `PUT /api/products/{id}/zbozi`); obchůzka pak cenu obnovuje — jeden
  request na produkt za cyklus, s pauzami, vlastní User-Agent.
* Ukládá se jako `Offer(source=zbozi, shop="Zboží.cz")`, minimální cena do
  append-only historie. Pozor na interpretaci: je to *tržní minimum*, ne
  cena typického obchodu — `retail_best` zpřesňuje, medián může mírně
  táhnout dolů. Při desítkách nabídek na produkt zanedbatelné.
* Vyhledávání (`/hledej/`) nepoužíváme, přestože technicky funguje —
  robots.txt ho zakazuje a etiketa z ADR-0004 platí. Kdyby bylo někdy
  potřeba automatické objevování zbozi URL, kandidáti jsou: povolené
  `/api/` (dořešit kontrakt) nebo sitemapy (úplné, ale objemné).

## Důsledky

* Nový člen `Source.ZBOZI`; nativní MySQL ENUM se rozšiřuje aditivní
  migrací (`db._migrate_source_enum`) — `create_all` existující sloupec
  nezmění a INSERT by spadl.
* Pokrytí velkého retailu je per-produkt, ne plošné: připnuté produkty
  ano, zbytek katalogu ne. Pro režim „hlídám ~30 relevantních produktů"
  přesně stačí.
