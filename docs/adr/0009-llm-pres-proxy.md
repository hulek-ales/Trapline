# ADR-0009: LLM přes Ollama proxy — klíč a plánovač GPU

Datum: 2026-09-09. Stav: přijato, v1 implementováno (klíč + plánovač;
dávkové úlohy odložené).

## Kontext

Ollama na domácím serveru je jedna GPU sdílená víc klienty (Trapline, Open
WebUI, další projekty). Doteď Trapline volala holé `/api/chat` a o kolize se
nestarala: když GPU držel jiný model, dotaz buď čekal bez informace, co se
děje, nebo Ollama modely střídala a každý přepnutí stálo desítky sekund
nahrávání. Skóring stovek produktů to protahovalo na hodiny.

Před Ollamu přibyla **Ollama logging proxy** (`ollamaproxy.aleshulek.cz`):
reverse proxy s klíči, logem tokenů a výkonu, plánovačem GPU a frontou
odložených úloh. Dokumentace: `/mgmt/docs` (OpenAPI).

## Rozhodnutí

### Klíč místo otevřené Ollamy

Každé volání nese `Authorization: Bearer opx_…` (`TRAPLINE_OLLAMA_KEY`,
klíč z `/ui/keys` proxy). Klíč se nikam nevrací — `/api/system/integrations`
i `/api/scoring/ollama` hlásí jen, jestli je vyplněný a jestli proxy prošel.

**Bez klíče se nic nemění**: `/mgmt` se nevolá, dotazy jdou jako dřív na
holou Ollamu. Přechod je tedy jen o vyplnění dvou proměnných v YAML appky.

### Plánovač před každou inferencí

Protokol proxy: `POST /mgmt/v1/models/load {model, wait_s}` → buď
`loaded: true` (model sedí na GPU, dotaz pošli do `hold_s` sekund), nebo
`loaded: false` se `status` `queued` (GPU drží jiný model, jsme ve frontě)
/ `loading` (Ollama ho nahrává) a volá se znovu.

`llm.ensure_loaded()` to dělá za všechny: `chat_json()` ho volá před
každým `/api/chat`, v cyklech po `LOAD_POLL_S` (60 s) až do celkového
rozpočtu `TRAPLINE_LLM_LOAD_WAIT_S` (výchozí 15 min). Po vypršení zvedne
`LlmBusy` — volající (skóring, bazar, hunt) to bere jako přeskočený kus,
ne jako pád; příští obchůzka ho zkusí znovu.

Dvě pojistky, aby přechod nic nerozbil:

- **Proxy bez plánovače** (starší verze, nebo URL míří na holou Ollamu
  s klíčem omylem): první 404 na `/mgmt/v1/models/load` plánovač vypne do
  restartu appky a dotazy jedou naslepo jako dřív — stejný princip jako
  `transport._browser_first`.
- **429** (limit klíče nebo fronty) se hlásí jako `LlmBusy` s textem
  odpovědi, ne jako obecná HTTP chyba.

### Diagnostika

`/api/scoring/ollama` s klíčem přidává `proxy_status`: `/mgmt/v1/health`
(prošel klíč?) a `/mgmt/v1/models/status` (co drží GPU, fronta po
modelech). GUI to ukazuje u tlačítka skóringu.

## Odloženo: dávkové úlohy (`/mgmt/v1/jobs`)

Proxy umí i odložené úlohy seskupené podle modelu s prioritou interaktivních
dotazů — pro skóring stovek produktů by to bylo ideální (dávka jedním
voláním, výsledek přes `GET /jobs?batch=…&bodies=1`). Není to ale změna
protokolu, ale změna běhu skóringu: dnes se každý produkt hodnotí a commituje
zvlášť, dávka znamená rozdělit „pošli" a „zpracuj výsledek" přes obchůzky.
Až bude plánovač v provozu a uvidíme, kolik času skutečně ušetří, je to
další krok.

## Zamítnuto

- Vlastní fronta na straně Trapline — plánovač už proxy má a vidí i ostatní
  klienty; duplikovat ho lokálně by kolize neřešilo.
- Přechod na OpenAI-kompatibilní `/v1/chat/completions` proxy — Ollama
  `format` (JSON schema) je pro strukturovaný výstup spolehlivější než
  `response_format`, a proxy ho propouští beze změny.
