# ADR-0001 (Reality): Datové zdroje, datový model a referenční cena za m²

**Status:** navrženo
**Datum:** 2026-08-25

## Kontext

Realitní varianta Trapline: pasti na inzeráty nemovitostí, obchůzka,
notifikace při nabídce pod trhem. Jde o sesterskou aplikaci podle vzoru
Trapline, ne o novou doménu uvnitř ní — realitní trh nemá protějšek celé
retailové větve (Product/Offer/EAN/feedy/odpisová křivka), takže sdílíme
vzor a infrastrukturní vrstvy (transport dle ADR-0006, extraktory jako data
dle ADR-0007, watcher s diffem, LLM skóring, GUI skořápka), ale ne datové
schéma.

Nemovitosti se chovají jako bazarová větev Trapline s třemi podstatnými
rozdíly:

1. **Cena inzerátu se mění.** U bazaru je cenová historie bezcenná; u
   nemovitostí je zlevnění inzerátu jeden z nejsilnějších signálů
   (motivovaný prodejce). Cenová historie patří na inzerát.
2. **Identita je těžší než fuzzy match titulku.** Tentýž byt inzeruje víc
   agentur s jinými fotkami, jiným textem a zaokrouhlenou adresou.
3. **Neexistuje „cena nového"**, proti které by se kotvil cold start.
   Referenci nahrazuje Kč/m² per segment z comparables.

Odkazy na ADR-000x bez prefixu míří do `docs/adr/` Trapline.

## Rozhodnutí

### Zdroje

Empiricky ověřeno 2026-08-25; pořadí = priorita implementace.

#### Bezrealitky — první zdroj

GraphQL API frontendu (`api.bezrealitky.cz/graphql/`), funguje bez
autentizace, stačí poslat `Origin`/`Referer` hlavičky webu (bez nich 403).
Strukturovaná data včetně GPS, plochy a dispozice. Přímí prodejci — ceny
bez provize, tedy systematicky níž než u agentur; do vzorku pro referenci
vstupují s příznakem zdroje (viz segmentace níže).

#### Sreality — největší pokrytí, ale nejkřehčí přístup

Staré otevřené JSON API (`/api/cs/v2/estates`), na kterém stojí většina
známých scraperů, **už neexistuje** (404). Nový frontend volá
`/api/v1/estates`, které bez frontendové autorizace vrací 401
(`is_sbr: false` — Seznam bad-robot detekce). Cesta: browserem (ADR-0006,
jen v discovery) získat platné cookies/token a dál jet přes httpx; při
zkřehnutí fallback na parsování HTML výpisů extraktorem-jako-data.
Kvůli téhle nejistotě není Sreality první, přestože je největší.

#### Bazoš (sekce Reality) — triviální

Statické HTML bez JS, stejný přístup jako plánovaný bazarový crawler
Trapline. Málo strukturovaných parametrů → víc práce pro LLM klasifikaci,
ale zato inzeráty přímých prodejců, které jinde nejsou.

#### iDNES Reality — SSR HTML

Výpisy jsou server-rendered (ověřeno 200 bez JS), parsování selectolaxem
přes extraktor-jako-data. Doplněk pokrytí mimo Seznam ekosystém.

#### Katastr / ČÚZK — zamítnuto pro automatizaci

Jediný zdroj **transakčních** cen v ČR. Sestavy „údaje o cenách" jsou
placené, bez API, neautomatizovatelné. Jednorázový ruční import za vybranou
lokalitu je ale legitimní kalibrace survival-weighted odhadu — nechat si
otevřená vrátka ve schématu (vzorek s příznakem `transactional`).

#### Facebook Marketplace — zamítnuto

Stejné důvody jako v ADR-0001 Trapline: detekce, riziko banu, ToS.

#### UlovDomov a nájemní servery — odloženo

Fáze 1 je prodej bytů. Nájmy jsou jiný trh s jinou dynamikou (poločas dnů,
ne týdnů); až po zaběhnutí prodejní pipeline.

Rate limit všude: 1 request / 2–3 s, sekvenčně, jeden identifikující
User-Agent, respektovat robots.txt. Inzeráty žijí týdny — obchůzka à 1–2 h
stačí, žádná 15minutová smyčka.

### Datový model

Dvě jádrové entity místo trojice Product/Offer/Listing:

* **`Property`** — deduplikovaná nemovitost (jeden fyzický byt). Kanonické
  parametry: dispozice, plocha, patro, vlastnictví (osobní/družstevní),
  stav, energetická třída, GPS, lokalita.
* **`Listing`** — inzerát u konkrétního zdroje/agentury, N na Property.
  Přebírá z Trapline `first_seen`/`last_seen`/`gone_at` (diff proti
  snapshotu, stejně kritické jako v ADR-0002) a přidává
  **`PriceHistory` per Listing** — append-only, zlevnění je událost
  první třídy.

**Párování Listing → Property** (obdoba `ListingMatch`): mechanický
kandidátní filtr (GPS do ~150 m, plocha ±3 %, shodná dispozice, patro
pokud uvedeno) a LLM potvrzení hraničních případů z textu a fotek popisků.
Dedup musí běžet od prvního dne — bez něj tentýž byt od tří agentur
třikrát nafoukne vzorek reference i alerty.

**Relisting:** inzerát zmizí a za pár dní se objeví „nový" s nižší cenou
(agentury tak resetují datum vložení). Match na Property ho odhalí; pro
referenci se řetězec relistingů počítá jako jeden vzorek s kumulovanou
dobou života, pro alerting je to skryté zlevnění.

**Pasti** (`Criteria`) beze změny konceptu: hard = mechanický filtr
(cena, plocha, dispozice, polygon/seznam lokalit), soft = LLM skóring
volného textu pasti proti popisu inzerátu („do 10 minut na metro, ne
přízemí, ne družstevní, ne věcné břemeno"). Popisy realitních inzerátů
jsou nestrukturovaný text plný podstatných detailů — LLM tu má větší
návratnost než u produktových parametrů.

### Referenční cena: survival-weighted Kč/m² per segment

Adaptace ADR-0002 — mechanika zůstává, mění se osa normalizace a prior:

* Vzorek = **Kč/m²** inzerátu (ne absolutní cena), váha `2^(-days_alive/H)`.
  Poločas **H = 45 dní** místo 14 (odhad z typické doby prodeje bytu;
  překalibrovat z vlastních dat). Cap 0,5 pro aktivní (censored) inzeráty
  a winsorizace 10 % z obou konců zůstávají.
* **Segment** = (lokalita × dispozice × stav). Cold start neřeší odpisová
  křivka (není z čeho), ale **hierarchický shrinkage** proti hrubšímu
  segmentu: (lokalita × dispozice × stav) → (lokalita × dispozice) →
  (obec/okres). Na každé úrovni `alpha = n / (n + 8)`, `alpha` se ukládá
  jako `confidence` a bez ní se nealertuje — stejný princip jako ADR-0002.
* Vzorky z Bezrealitky a Bazoše (bez provize) nesou příznak zdroje;
  pokud se posun proti agenturním cenám ukáže systematický, korigovat
  konstantou, ne mícháním segmentů.
* Tvrdá blokace „proti ceně nového" nemá protějšek a **vypouští se**.

### Alerty

Tři typy událostí, deduplikace přes `Alert` jako v Trapline:

1. **Nový inzerát pod referencí** — Kč/m² citelně pod segmentovou
   referencí při dostatečné `confidence`.
2. **Zlevnění sledovaného inzerátu** — s poučeními z dodatku ADR-0002 od
   prvního dne: práh citelnosti (`MIN_MEANINGFUL_DROP ≈ 0.03` — realitní
   slevy bývají relativně menší než retailové akce), baseline = *nižší*
   z krátkodobého a dlouhodobého pohledu na historii inzerátu.
3. **Relisting se skrytým zlevněním** — detekovaný přes match na Property.

## Důsledky

* Sreality je největší zdroj a zároveň jediný s autorizační bariérou —
  pipeline musí být plně funkční i bez něj (Bezrealitky + Bazoš + iDNES),
  jinak stojí celý projekt na nejkřehčím článku.
* Bez retailového prioru jede odhad první ~2 měsíce prakticky jen na
  hrubších segmentech hierarchie; `confidence` to vyjadřuje a alerting
  je podle toho blokovaný. Trpělivost je v zadání projektu („horizont
  měsíců, ne dní").
* `gone_at` a dedup na Property jsou nosné zdi: bez diffu nefunguje
  survival váha, bez dedupu je vzorek i alerting násobně zkreslený.
  Obojí musí být v první iteraci watcheru, ne „potom".
* Kč/m² normalizace mlčky předpokládá, že plocha je v inzerátu pravdivá
  a srovnatelně měřená (užitná vs. podlahová). Nepřesnosti řeší
  winsorizace; systematický rozdíl mezi zdroji případně stejná korekce
  jako u provize.
