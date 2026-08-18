# Trapline

Agent, který nastraží „pasti" podle zadaných kritérií, periodicky je obchází
a ozve se, když se objeví nabídka pod trhem.

Nastavíš kritéria → agent najde vyhovující produkty → sleduje jejich ceny
v eshopech i v bazarech → notifikuje, když se vyplatí koupit. Počítá se
s horizontem měsíců, ne dní.

## Proč to není jen hlídač ceny

Dvě zásadně různé domény, dva oddělené pipeline:

| | Retail (eshopy) | Bazary |
|---|---|---|
| Identita | EAN / GTIN | fuzzy match z titulku |
| Životnost nabídky | měsíce | hodiny až dny |
| Cenová historie | smysluplná | bezcenná (1 vzorek) |
| Sledovaná událost | pokles ceny | **nový inzerát** |
| Interval | 12 h | 15–30 min |

Spojuje je `PriceReference`: retail crawl dodá referenční cenu, proti které se
bazarový inzerát hodnotí relativně, ne absolutně.

## Odhad tržní ceny

Bazary neposkytují transakční ceny, jen nabídkové. Prostý medián aktivních
inzerátů je systematicky nadsazený — předražené inzeráty v datech visí měsíce,
dobře naceněné zmizí za den. Prostý medián tedy měří hlavně **neprodejné
zboží**.

Řešení v `pricing/reference.py`: vážený medián s váhou klesající podle doby
přežití inzerátu (poločas 14 dní). Cold start řeší shrinkage blend proti
odpisové křivce z retailové ceny — při nula vzorcích jede odhad čistě na
modelu, při 30 vzorcích je prior prakticky zapomenutý.

Vyžaduje, aby watcher zaznamenával i **zmizení** inzerátu (diff proti
předchozímu snapshotu), ne jen nové položky.

## Stav

- [x] Datové schéma (`models.py`)
- [x] Referenční ceny — vážený medián, winsorizace, shrinkage (`pricing/reference.py`)
- [x] Deal scoring — pickup cost, red flags, blokace proti ceně nového (`pricing/scoring.py`)
- [x] Discovery: Heureka XML feedy → katalog produktů s parametry (`discovery.py`, ADR-0003)
- [x] LLM skóring produktů proti pastem (`scoring.py`, verdikt per požadavek)
- [x] Zboží.cz watcher — ceny velkého retailu per produkt (ADR-0005)
- [ ] JSON-LD extraktor pro retail (střední eshopy bez feedů)
- [ ] Sbazar / Bazoš crawler + diff pro `gone_at`
- [ ] Allegro REST API klient (OAuth2 client_credentials)
- [ ] LLM klasifikace bazarových inzerátů (structured output)
- [ ] Alembic migrace
- [x] FastAPI skořápka + GUI se self-update z Gitu (`api/`)
- [x] DB inicializace (`db.py`, create_all) + CRUD kritérií v API a GUI
- [ ] FastAPI backend — zbylé doménové endpointy (produkty, inzeráty, alerty)
- [ ] React GUI + like/dislike
- [x] Watcher: obchůzka (APScheduler), retail reference, budget alerty + ntfy
- [ ] Cenové poklesy proti baseline (s poučeními z dodatku ADR-0002)

## Vývoj

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## Spuštění GUI

```bash
APP_PASSWORD=nejake-heslo UPDATE_ENABLED=true \
  uvicorn trapline.api.main:app --port 8000
```

`APP_PASSWORD` zapne přihlašovací obrazovku a zavře celé `/api/` (kromě
`/api/auth/*` a `/api/health`). Bez něj je appka otevřená a při zapnutém
self-update na to při startu upozorní v logu — endpoint umí spustit `git pull`
a restart procesu. Detaily v [DEPLOY-TRUENAS.md](DEPLOY-TRUENAS.md).

Zatím jde o skořápku: statická stránka a panel **Verze a aktualizace**, který
umí stáhnout novou verzi z Gitu a restartovat se — stejný mechanismus jako
v Kuchařce. Doménové obrazovky přibudou, až budou hotové crawlery.

Pod Dockerem běží uvicorn v supervisor smyčce (`docker/entrypoint.sh`);
tlačítko ukončí proces a smyčka udělá `git pull`, doinstaluje závislosti
a nastartuje novou verzi. Mimo Docker jen pullne a vyzve k ručnímu restartu.
Bez `UPDATE_ENABLED=true` se panel vůbec nevykreslí a endpointy vrací 403.

Nasazení na TrueNAS (custom app YAML) popisuje [DEPLOY-TRUENAS.md](DEPLOY-TRUENAS.md),
hotová definice je v [`TrueNasAPP.yaml`](TrueNasAPP.yaml).

## Rozhodnutí

Zdůvodnění návrhových rozhodnutí je v `docs/adr/`. Než začneš měnit konstanty
v `pricing/`, přečti si je — většina čísel má důvod.
