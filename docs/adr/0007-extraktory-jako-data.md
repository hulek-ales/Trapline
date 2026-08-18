# ADR-0007: Extraktory jako data, fronta úkolů, syrová pozorování

**Status:** přijato (závěry debaty 2026-08-18; v dokumentu číslováno jako
ADR-0004)

## Základní dělba

**LLM na strukturu, kód na hodnoty.** Discovery používá LLM (interpretace,
párování, generování extraktorů), watcher je čistě deterministický —
nedeterminismus na deterministické úloze znamená cenu a nedebugovatelnost.
*(V repu už platí: skóring = LLM, obchůzka = kód.)*

## Extraktory jako data, ne kód

Uložené v DB (typ + cesta/selektor), verzované, testované proti uloženému
HTML snapshotu. Generuje je agent při přidání zdroje; watcher je vykonává
bez jediného tokenu. **Přidání eshopu = řádek v tabulce, ne commit.**

## Fronta úkolů

Čtyři typy: `SEARCH` (SERP → organické odkazy), `CATEGORY` (výpis →
odkazy na produkty + další strana), `PRODUCT` (detail → JSON-LD + DOM
fragment), `PRICE` (detail → jen cena a dostupnost).

Stropy, bez kterých to uteče:

* max 200 stránek na past celkem; max 30 na doménu; max 3 strany stránkování
* hloubka 3 (SERP → kategorie → produkt)
* 5× nerelevantní produkt z domény → blacklist domény pro tuhle past

Průběh při založení pasti: LLM vrátí ~10 dotazů (synonyma, CZ i EN,
značky) → SEARCH → filtr domén (agregátory, srovnávače a blogy pryč) →
zkusit /heureka.xml (existuje → crawl odpadá) → CATEGORY → PRODUCT (LLM
vyhodnotí příslušnost a parametry → Product + Offer) → potvrzené produkty
do watch režimu → PRICE úkoly.

## Syrová pozorování a tolerance k mezerám

* `raw_observations`, **append-only**: URL, timestamp, JSON-LD, HTML
  fragment. Klient/browser neinterpretuje. Umožní přeparsovat historii bez
  opakovaného scrapování. *(Feedy už snapshotujeme — ADR-0004; rozšířit na
  všechny transporty.)*
* Alerting rozlišuje „cena klesla" a „poslední známá cena je stará 3 dny".
* **EAN jako primární klíč, ne URL.** Detekce 404/redirectu → re-discovery.

## Monitoring selhání — nejdůležitější bod celého návrhu

Antidetect i parsery selhávají **tiše**. Extraktor vrátí prázdno nebo cenu
mimo očekávaný řád → screenshot + HTML do `data/failures/` + notifikace,
**NE zápis do DB**.
