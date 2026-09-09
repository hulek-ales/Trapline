# ADR-0010: Nastavení z GUI — databáze má přednost před prostředím

Datum: 2026-09-09. Stav: přijato, v1 implementováno (jen LLM).

## Kontext

Veškerá konfigurace šla přes proměnné prostředí v YAML TrueNAS appky
(ADR-0004). To je správně pro věci, které se nastavují jednou — DB, heslo,
Git. Pro LLM to ale znamenalo, že každá změna modelu, přepnutí na proxy
(ADR-0009) nebo úprava čekání na GPU = otevřít YAML, přenasadit appku,
čekat na start. Přesně u LLM se přitom ladí nejvíc.

## Rozhodnutí

### Vrstva nad prostředím, ne náhrada

Tabulka `app_settings` (klíč, hodnota) drží to, co uživatel uložil v záložce
**Administrace**. Při startu a po každém uložení se hodnoty promítnou do
objektu `settings` — zbytek kódu čte pořád jeden objekt a o vrstvě neví.

Pořadí: **DB > prostředí > výchozí hodnota v kódu.** Prostředí zůstává
záchranou: bez DB (nebo po tlačítku „vrátit hodnoty z prostředí") se jede
z YAML jako dřív.

### Rozsah: jen LLM

`ollama_url`, `ollama_key`, `llm_main`, `llm_bulk`, `llm_load_wait_s`.
Nic víc — heslo, DB, Git a self-update z GUI měnit nejde a nemá; to jsou
věci, kde chyba znamená, že se do GUI už nedostaneš.

### Tajné hodnoty

Klíč proxy se ukládá do DB jako každé jiné nastavení. Je to stejná hranice
důvěry jako heslo k DB v YAML — vlastní NAS, vlastní DB. Ven z API ale
klíč **nikdy nejde**: `GET /api/admin/settings` vrací jen `ollama_key_set:
true/false`, GUI má prázdné pole s nápisem „beze změny" a zvláštní tlačítko
na zapomenutí.

### Diagnostika hned po uložení

„Otestovat spojení" volá totéž co `/api/scoring/ollama`: dosažitelnost,
seznam modelů (ten se nabídne do polí s modely), stav proxy a plánovače.
Uložení navíc resetuje útlum plánovače (`llm.reset_state()`), protože nová
URL může mít plánovač, který ta stará neměla.

## Zamítnuto

- Zápis zpět do `.env` / YAML — kontejner k YAML appky nemá přístup
  a `.env` se v Dockeru nepoužívá.
- Obecný editor všech proměnných — lákavé, ale právě heslo a DB URL
  v GUI jsou past: jedna chyba a appka je nedostupná.
