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
- [ ] JSON-LD extraktor pro retail
- [ ] Sbazar / Bazoš crawler + diff pro `gone_at`
- [ ] Allegro REST API klient (OAuth2 client_credentials)
- [ ] LLM klasifikace inzerátů (qwen3:4b, structured output)
- [ ] Alembic migrace
- [ ] FastAPI backend
- [ ] React GUI + like/dislike
- [ ] ntfy notifikace

## Vývoj

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## Rozhodnutí

Zdůvodnění návrhových rozhodnutí je v `docs/adr/`. Než začneš měnit konstanty
v `pricing/`, přečti si je — většina čísel má důvod.
