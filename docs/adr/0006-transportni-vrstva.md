# ADR-0006: Transportní vrstva a serverový browser

**Status:** přijato (závěry debaty 2026-08-18; v dokumentu číslováno jako
ADR-0003, čísla 0003–0005 už v repu obsazená)

## Kontext

Zdroje se liší tím, jak se k nim dá přistoupit: veřejný feed, strukturovaná
data v HTML, holé HTML, anti-bot vyžadující skutečný prohlížeč. Dosud je
transport natvrdo httpx; každý nový typ zdroje by znamenal zásah do kódu.

## Rozhodnutí

### Fetch je rozhraní s per-zdroj konfigurací (SourcePolicy.transport)

Fallback řetězec při přidávání zdroje, zkouší se v tomhle pořadí:

1. **/heureka.xml** — vždy první, serverově. U Shoptet/Upgates veřejný celý
   katalog včetně EAN; crawl domény tím odpadá celý. *(Hotovo — discovery.)*
2. **JSON-LD** přes extruct + prostý httpx. Pokryje většinu zbytku.
3. **HTML parser se selektorem** (extraktor jako data, viz ADR-0007).
4. **Browser** — jen kde předchozí selžou. NIKDY ve watcheru, jen
   v discovery.

### Serverový browser (ne na PC)

TrueNAS sdílí veřejnou IP s PC — IP reputace ani geolokace se přesunem na
server neztrácí; browser extension na PC tím odpadá.

* `selenium/standalone-chromium` v Dockeru, **headful přes Xvfb** (ne
  headless), `shm_size: 2gb` (bez toho Chrome padá)
* persistentní profil na datasetu s vypnutou kompresí
* `SE_NODE_MAX_SESSIONS: 2` (~1 GB RAM na session), GPU vypnout
* noVNC na 7900 pro ruční zásah; zvenku jen za Zero Trust, z LAN přes
  split-DNS; websockify `--heartbeat=30` kvůli CF idle timeoutu ~100 s
* čistší alternativa: `browserless/chromium` (REST /scrape místo WebDriver)

### Cookie bannery

`duckduckgo/autoconsent` jako init script (skutečně odklikává, ne skrývá;
OneTrust, Cookiebot, Didomi, Usercentrics + heuristika), uBlock s EasyList
Cookie jako pojistka. Persistentní profil → řeší se jen při první návštěvě
domény.

### Frekvence a etiketa

* watch 2–4× denně na produkt při horizontu 1–6 měsíců
* throttle **per-host**, ne globálně; jitter ±20 % (rozprostření, ne maskování)
* jeden tab v jeden okamžik, pauza 3–5 s

*(Sladit s obchůzkou: dnešní globální WATCH_HOURS=12 je hrubší replika;
per-host frekvence přijde s frontou úkolů z ADR-0007.)*

## Zamítnuto

* Placené scraping API (Zyte, Firecrawl, ScrapingBee) — při ~1 800
  requestech měsíčně a vlastním browseru nemají co přidat.
* Claude jako scheduler — neexistuje ve tvaru „server sám obchází weby";
  agentní běhy spouští člověk. Ověřit aktuální stav na docs.claude.com.
* Heureka — ADR-0001 platí, blokace je fingerprint-based.
* Extension na PC — nahrazena serverovým browserem.
* Alza: nezavrhovat, ale přes JSON-LD na produktových stránkách, ne
  parsování kategorií. *(Doplněk: ceny velkých řetězců mezitím řeší
  Zboží.cz per produkt, ADR-0005.)*

## SERP pro discovery

SearXNG (Seznam + DDG primárně, Google padá) vs. dedikovaná SERP API
(Serper, Tavily) — zůstává otevřené; feedhunt dnes jede přes SearXNG
s HTML fallbackem a stačí.
