# ADR-0003: Discovery katalogu a skóring proti pastem

**Status:** přijato
**Datum:** 2026-08-17

## Kontext

Past („kritérium") je psaná volným textem — „velikost na 4× 2L láhve",
„rozdíl teplot minimálně 20 °C". Původní představa byla, že se z ní ručně
vyplní strukturovaný filtr. To se ukázalo jako špatné pořadí: uživatel
přemýšlí v požadavcích, ne v parametrech, a převod má dělat stroj.

## Rozhodnutí

Pipeline má čtyři fáze, každá běží samostatně a má vlastní stav:

1. **Discovery** — stáhnout katalogy eshopů, založit kanonické produkty
   s tabulkou parametrů a nabídkami. Mechanické, bez LLM, levné, denně.
2. **Skóring** — LLM porovná parametry produktu proti volnému textu pasti,
   včetně dopočtů (4× 2L láhev → potřebný vnitřní objem). Výstup: skóre,
   verdikt relevantní/ne a zdůvodnění per parametr. Přepočítává se jen při
   změně pasti nebo příchodu nového produktu.
3. **Hlídání cen** — jen pro relevantní produkty (automaticky relevantní,
   nebo ručně přebité přes UserFeedback). Retail: JSON-LD + opakované
   stažení feedů. Bazary: Allegro API, Sbazar, Bazoš (ADR-0001).
4. **Alerty** — proti PriceReference (ADR-0002), deduplikace přes Alert.

### Discovery přes Heureka XML feedy, ne crawl

Za ADR-0001: veřejné cesty typu `/heureka/export/products.xml` (Shoptet),
`/heureka.xml` (Upgates aj.) vrací celý katalog včetně parametrů a EAN.
Jedno stažení denně na eshop. Velké řetězce (Alza, Datart) feedy nevystavují
a mají anti-bot — pro ně později JSON-LD na konkrétních produktech; discovery
šíři dodají specializované obchody, kterých je v každé kategorii 10–20.

Zdroje jsou v DB (`feed_sources`), spravují se za běhu z GUI. Každý zdroj má
`category_filter` — čárkou oddělené podřetězce; položka projde, když aspoň
jeden matchne v CATEGORYTEXT nebo PRODUCTNAME. Filtr je mechanický schválně:
držet discovery levné, inteligenci nechat skóringu.

### Deduplikace produktů

Primárně EAN. Bez EAN (menší eshopy ho ve feedu nemají) fallback na
`(brand, model_norm)`, kde model_norm je normalizovaný název bez značky,
diakritiky a interpunkce. Nepřesné — dva eshopy pojmenují týž model jinak;
řeší se to až LLM párováním ve fázi skóringu, ne v discovery.

### Ceny z feedu rovnou do PriceHistory

Každý běh discovery zapíše cenu nabídky do PriceHistory (append-only, i beze
změny, viz ADR-0002) — discovery a hlídání cen tak sdílí zápisovou cestu
a percentily mají od začátku data.

## Důsledky

* Nová tabulka `criteria_matches` (past × produkt): skóre, verdikt, rozpad
  zdůvodnění, verze pasti při vyhodnocení. Ruční verdikt v UserFeedback má
  vždy přednost před automatickým.
* Nová tabulka `feed_sources`.
* `query_terms` pasti zůstávají volný text uživatele; hledací fráze pro
  bazary z nich odvodí LLM až ve fázi 3 (do té doby se nepoužívají).
