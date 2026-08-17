# ADR-0001: Volba datových zdrojů

**Status:** přijato
**Datum:** 2026-08-17

## Kontext

Potřebujeme data o cenách nového i použitého zboží na českém trhu.

## Rozhodnutí

### Heureka — zamítnuto

Všechna API (Bidding, Marketplace, Ověřeno zákazníky) jsou merchant-side,
přístup jen přes obchodního zástupce, Bidding API navíc placené. Heureka
v dokumentaci sama uvádí, že přes polovinu serverové kapacity spotřebuje
robotické procházení a že ho chce omezit — tomu odpovídá i agresivita
anti-bot ochrany. Scraping tedy není ani legální cesta, ani udržitelný.

### JSON-LD na produktových stránkách — primární retail zdroj

Většina CZ eshopů (Shoptet, Upgates, WooCommerce) generuje `schema.org/Product`
+ `Offer` kvůli Heurece a Google Shopping. Cena a dostupnost jsou tak dostupné
strukturovaně, bez parsování HTML a bez LLM. Nejlepší poměr přínos/křehkost
v celém projektu.

### Heureka XML feedy jednotlivých eshopů — discovery

Cesty typu `/heureka.xml` jsou veřejné a obsahují celý katalog včetně parametrů
a EAN. Jedno stažení denně nahradí crawl stovek stránek.

### Allegro — oficiální REST API

OAuth2 `client_credentials` flow, registrace na `apps.developer.allegro.pl`,
limit 9 000 req/min na aplikaci. Vrací i použité zboží jako strukturovaná data
včetně parametrů — to bazary neumí. Pozor na povinný `User-Agent` a
`Accept: application/vnd.allegro.public.v1+json` (jinak 406).

### Sbazar, Bazoš — sekundární trh

Sbazar: nedokumentované JSON API frontendu, stabilní, bez anti-bot ochrany.
Bazoš: statické HTML bez JS, parsování triviální (selectolax).

Rate limit: 1 request / 2–3 s, sekvenčně, jeden identifikující User-Agent,
respektovat `robots.txt`.

### Facebook Marketplace — zamítnuto

Agresivní detekce, riziko banu účtu, ToS explicitně zakazuje scraping.
Poměr přínos/riziko nevychází.

### Aukro — odloženo

Starý SOAP API je mrtvý, dnes SPA s XHR endpointy. Proveditelné, ale křehčí
než ostatní zdroje. Až po zprovoznění Sbazaru a Bazoše.

## Důsledky

Retail pokrytí bude neúplné — musíme udržovat ruční seznam relevantních eshopů.
Přijatelné, protože v každé kategorii je jich reálně 10–20.
