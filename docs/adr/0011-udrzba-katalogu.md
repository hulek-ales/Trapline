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
