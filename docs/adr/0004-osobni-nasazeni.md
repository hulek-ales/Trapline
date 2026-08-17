# ADR-0004: Nasazení pouze pro osobní použití

**Status:** přijato
**Datum:** 2026-08-17

## Kontext

Trapline běží výhradně na privátní infrastruktuře (TrueNAS, reverzní proxy
NPM Plus + Cloudflare Zero Trust) a nepublikuje žádná data navenek. Nalezené
ceny nikdo jiný nevidí. Tenhle režim určuje právní i technická rozhodnutí
níž — pokud by se z Trapline někdy stala veřejná služba, tohle ADR je první
k revizi, spolu s ADR-0001 a ADR-0002.

## Důsledky

* **GDPR čl. 2(2)(c)** — výjimka pro osobní potřebu platí. Kontakty
  z bazarových inzerátů lze ukládat. Retence `listings` = 12 měsíců.
* **Databázové právo (sui generis)** se prakticky neaktivuje — data se
  nešíří ani z nich nevzniká konkurenční služba.
* **Autentizace:** primární hranicí je reverzní proxy / Zero Trust.
  Aplikace k tomu má jednu tenkou vrstvu navíc — sdílené heslo
  (`APP_PASSWORD`, HttpOnly cookie) — protože endpoint self-update umí
  spustit `git pull` a restart procesu a nemá stát jen na proxy.
  Vědomě ale nejde o plnohodnotný auth systém: žádné uživatelské účty,
  žádná multi-tenancy, žádné `user_id` v datech.

  *Pozn.: původní návrh počítal s tím, že aplikace autentizaci nemá vůbec;
  heslo přibylo na explicitní žádost. Rozsah (jedno sdílené heslo) zůstává
  v duchu původního rozhodnutí.*
* **Affiliate feedy (eHUB, Dognet) zamítnuty:** technicky by fungovaly
  výborně, ale publisher registrace předpokládá, že produkty někde
  zobrazuješ. Čistě soukromý nástroj nic nepublikuje — registrace by byla
  nepravdivá deklarace, což je horší než šedá zóna scrapingu.
* **Syrové snapshoty:** stažené odpovědi zdrojů se ukládají gzipované na
  disk (`SNAPSHOT_DIR`, default `data/snapshots` — mimo git, uvnitř volume),
  aby šlo při změně parseru zpětně přeparsovat historii bez opakovaného
  stahování. Drží se posledních ~30 na zdroj.

## Crawler etiketa (praktická pravidla)

* Vlastní identifikovatelný User-Agent (`Trapline/0.1 (+osobni cenovy
  monitor)`) je bezpečnější než maskování za prohlížeč.
* Jeden request za 2–3 s, sekvenčně; respektovat Crawl-delay z robots.txt.
* `If-Modified-Since`/`ETag` cachování doplnit, až poběží pravidelný watcher.
* Politika má být konfigurovatelná per zdroj (respect_robots, min_delay,
  max denních requestů) — zatím stačí globální default, per-zdroj přijde
  s bazarovými crawlery.
