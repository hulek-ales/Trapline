# ADR-0008: Bazary — Bazoš, Sbazar, Allegro

Datum: 2026-08-19. Stav: přijato, v1 implementováno (Bazoš + Sbazar + Allegro).

## Kontext

Bazarové inzeráty jsou druhá polovina vize (ADR-0001): levné použité kusy
a signál reálné prodejní ceny. Na rozdíl od eshopů žijí krátce, nemají EAN
ani parametry a cena se prakticky nemění — hodnota je v rychlém zachycení
a v datu zmizení (≈ prodáno za tuhle cenu).

## Rozhodnutí

### Bazoš — procházení rubrik, žádné vyhledávání

robots.txt zakazuje `/search.php` a všechny vyhledávací parametry;
výpisy rubrik povoluje. Proto se prochází výpis sekce (subdoména, např.
`sport.bazos.cz`) od nejnovějších a filtruje se lokálně předfiltrem pasti
— stejný princip „nehledat, jen číst povolené" jako u Zboží.cz (ADR-0005).
Sekci (1–2) vybírá LLM z pevného seznamu, fallback `ostatni`. Plný popis
se čte z meta `og:description` detailu.

### Sbazar — frontend API na výslovné rozhodnutí uživatele

robots.txt Sbazaru zakazuje roboty plošně (`Disallow: /` pro `*`).
Uživatel byl na rozpor upozorněn a **výslovně rozhodl Sbazar zapojit**
(19. 8. 2026). Zmírnění: osobní použití, jednotky dotazů na frázi a běh
(stejné API a menší objem než běžná návštěva webu prohlížečem), identifikace
vlastním User-Agentem, pauzy mezi požadavky. Kdyby Seznam přístup zablokoval,
respektujeme to a Sbazar vypneme.

### Allegro — oficiální API

Jediný bazar s oficiálním API, takže žádné scrapování: grant
`client_credentials` (token se drží v paměti do vypršení) a veřejný výpis
`GET /offers/listing` s frázemi z předfiltru pasti. Hledá se stejně jako
na Sbazaru; Allegro navíc nabízí i nové zboží, což pro past nevadí — stav
kusu určuje LLM z parametrů nabídky.

Tři věci, které API neumí a řeší se jinak:

- **Popis** výpis nevrací. Místo něj se skládá text z parametrů nabídky
  (hlavně `Stan` — nová/použitá) a z ceny dopravy, ať má LLM co hodnotit.
  Detail nabídky je v API vyhrazený prodejci, takže se nedotahuje.
- **Měna.** Allegro účtuje ve zlotých. Přepočet dělá `fx` podle denního
  kurzovního lístku ČNB (prostý text, bez klíče a limitů, kurz se drží
  do konce dne). Bez známého kurzu se cena **neuvede** — porovnávat zlotý
  s korunovým rozpočtem pasti je horší než nabídku vidět bez ceny.
- **„Žije ještě?"** se zjišťuje ze stavu veřejné stránky nabídky, a to
  konzervativně: jen jasné 404 znamená pryč, blokace ani výpadek inzerát
  nepohřbí.

`TRAPLINE_ALLEGRO_USER_AGENT` smí zůstat prázdný — Allegro žádný závazný
řetězec negeneruje, chce jen hlavičku identifikující aplikaci, takže se
použije obyčejný `TRAPLINE_USER_AGENT`.

## Pipeline v1 (`bazar.py`)

kandidáti (výpisy Bazoše, hledání na Sbazaru a Allegru) → levný předfiltr → nové kusy: detail + LLM
verdikt proti požadavkům pasti (splněno/nesplněno/nelze určit) + odhad
stavu + varovné signály (platba předem…) → `Listing` + `ListingMatch`
(nově `criteria_id` — v1 váže inzerát na past přímo, párování na produkt
zůstává volitelné) → relevantní pod budgetem = alert (dedup
`listing:{source}:{ext_id}`). Průchod je fází obchůzky; u relevantních
kusů se pravidelně kontroluje, jestli inzerát žije (`gone_at`).

Stropy: 3 stránky výpisu na sekci, 3 fráze na past, 30 nabídek Allegra
na frázi, 15 LLM vyhodnocení na past a běh, 20 kontrol života na běh,
pauzy `REQUEST_DELAY_S`.

## Zamítnuto / odloženo

- Vyhledávání na Bazoši (robots) — rubriky stačí, jen s delší latencí.
- Párování inzerátů na konkrétní produkty katalogu a bazarové reference
  cen (used_median) — až bude dost nasbíraných inzerátů se `gone_at`.
