# ADR-0011: Údržba katalogu — práh hlídání, sirotci, dopad pasti

Datum: 2026-09-10. Stav: přijato, implementováno.

## Kontext

Katalog roste jen jedním směrem. Produkty do něj sype crawler pastí
(ADR-0007) i feedy a nic je nemaže — ani smazání pasti, kvůli které se
našly (`delete_criteria` maže jen `CriteriaMatch` a `ListingMatch`).

Než přibyl filtr v `catalog`, obchůzka obcházela **všechny** aktivní
nabídky. Měření na produkci 10. 9. 2026:

| | |
|---|---|
| aktivní pasti | 2 |
| obcházených stránek | 668 |
| z toho sirotků po smazané pasti „Příbory" | 218 |
| produktů s hlídanou stránkou / **relevantních** | 183 / **7** |

Filtr sirotky odstřihl (450 zbylo), ale zůstala druhá polovina problému:
hlídalo se všechno, co past **kdy oskórovala** — včetně kusů se skóre 5.

## Rozhodnutí

### Práh skóre místo „všechno, co má match"

Hlídá se produkt, jehož match k aktivní pasti je **relevantní**, nebo má
skóre aspoň `TRAPLINE_WATCH_MIN_SCORE` (výchozí 40, měnitelné
v Administraci podle ADR-0010).

Proč práh a ne jen relevantní: skóre se s cenou nemění, takže hlídat kus
oskórovaný na 5 bodů je čirá ztráta času — ale `references` počítá odhad
tržní ceny z cenové historie a širší vzorek mu pomáhá. Práh je ta páka
mezi délkou obchůzky a přesností odhadu; proto je v GUI, ne v kódu.

### Sirotek ≠ „pod prahem"

- **Sirotek** = produkt, který nezná žádná aktivní past. Nehlídá se vůbec
  a jde ho v Administraci zahodit.
- **Pod prahem** = past ho zná, jen ho nestojí za to obcházet. Zůstává
  v katalogu i po úklidu — snížením prahu se hlídání vrátí.

Vypnutá past dělá ze svých produktů sirotky. Je to záměr: vypnutí je
signál „tohle mě nezajímá", a úklid je vždycky vědomé kliknutí, ne
automat.

### Úklid je ruční a nevratný

`POST /api/admin/catalog/purge` maže sirotky i s nabídkami, cenovou
historií, referencemi, feedbackem a alerty. Automatický úklid v obchůzce
zamítnut: nevratná operace nad daty, která se sbírala týdny, si zaslouží
člověka u klávesnice. GUI se ptá a ukazuje počty předem.

### Zdroje feedů, které nikomu neslouží

Sirotky nesype jen crawler — sype je hlavně **feed**, který přežil past,
kvůli které vznikl. `porcelanovysvet.cz` nebo `luis.cz` s filtrem na
příbory zůstaly po smazané pasti „Příbory" zapnuté a v každé obchůzce
katalog znova naplnily.

**Slouží** feed, který naimportoval aspoň jednu nabídku produktu, jejž
zná aspoň jedna aktivní past — i pod prahem hlídání. Nabídky se k feedu
vážou přes `Offer.shop == FeedSource.name`, jak je zapisuje `discovery`.

Úklid běží v `feedcare.review()` na konci skóringu a je **dvoustupňový**:

1. Feed přestane sloužit → **vypne se**, dostane poznámku s datem
   a do `useless_since` se zapíše, odkdy je k ničemu. Tím hned přestane
   sypat sirotky, ale URL i filtr zůstanou — obnovíš-li past, stačí ho
   zapnout zpátky.
2. Zůstane-li vypnutý a k ničemu `TRAPLINE_FEED_PURGE_DAYS` dní
   (výchozí 14, 0 = nikdy), **smaže se**.

Proč ne rovnou smazat, když si to uživatel přál: čerstvě přidaný feed
a feed s výpadkem sítě vypadají zvenčí stejně jako nepotřebný. Vypnutí
je vratné jedním kliknutím, smazání ne — a lhůta dá čerstvé chybě čas
se projevit. Dvě věci se proto nesahají vůbec: feed, který **ještě
neběžel** (`last_run` prázdný), a feed, který nemá **žádnou** nabídku —
ten stejně nic nesype, takže není co řešit.

Na rozdíl od sirotků v katalogu je tenhle úklid automatický. Feed je
konfigurace, ne data: vypnutý feed nic neztratí a smazaný jde přidat
zpátky za deset sekund, kdežto smazaná cenová historie je pryč.

### Dopad pasti v seznamu

`GET /api/criteria` vrací u každé pasti `scored`, `relevant`
a `watched_offers`. Bez toho není u pasti vidět, co drží — a přesně proto
se stalo, že dvě pasti tiše držely 668 stránek na obchůzku.

## Zamítnuto

- **Mazat produkty rovnou při smazání pasti** — produkt může znát víc
  pastí a smazání jedné není důvod zahodit historii; a při vypnutí pasti
  by to bylo vyloženě špatně.
- **Hlídat jen relevantní** — nejrychlejší, ale `references` by přišly
  o většinu vzorku a odhad tržní ceny by zhrubl.
- **Mazat nepotřebný feed hned při první obchůzce** — přesně to, co
  zadání říkalo, ale nerozliší to „nikomu neslouží" od „dneska nestáhl
  nic, protože byl e-shop dole".
