# ADR-0002: Odhad tržní ceny z nabídkových dat

**Status:** přijato
**Datum:** 2026-08-17

## Kontext

Bazary nezveřejňují, za kolik se skutečně prodalo. Máme jen *asking price*.

Ta je systematicky nadsazená: v každém okamžiku vidíme průřez aktivními
inzeráty, ve kterém jsou předražené kusy nadreprezentované — visí tam měsíce,
zatímco dobře naceněné zmizí za den. Prostý medián aktivních inzerátů tedy
měří především neprodejné zboží.

## Rozhodnutí

### Váha podle doby přežití

Každý vzorek dostane váhu `2^(-days_alive / 14)`. Inzerát, který zmizel za den,
byl blízko tržní ceny → plná váha. Inzerát visící 60 dní → váha ~5 %.

Poločas 14 dní je odhad, ne měření. Po nasbírání dat překalibrovat.

Je to chudá verze survival analýzy. Plnohodnotný Kaplan–Meier by byl přesnější,
ale při desítkách vzorků na produkt to nemá návratnost.

### Censored vzorky

Inzerát, který visí 3 dny a ještě nezmizel, není důkaz o přeceněnosti — jen
zatím nevíme. Aktivním inzerátům proto váhu shora ořízneme na 0,5, aby je
krátký věk neodměňoval.

### Winsorizace před výpočtem

10 % z každého konce. Nahoře šedí dovozci a přeprodejci, dole příslušenství
prodávané pod názvem produktu a překlepy. Nahrazujeme, neodstraňujeme —
zachová se počet vzorků.

### Shrinkage blend pro cold start

```
alpha = n / (n + 8)
used_reference = alpha * used_median + (1 - alpha) * prior
prior = retail_best * depreciation(age)
```

Spojitý přechod bez prahů. `alpha` se zároveň ukládá jako `confidence` a slouží
jako podmínka alertingu — nealertuj proti odhadu, jen proti datům.

### Reference je retail_best, ne UVP

Doporučená cena výrobce je v CZ fikce nafouknutá kvůli „slevám". Používáme
10. percentil aktivních retailových nabídek = reálně dosažitelná nová cena.

### Tvrdá blokace proti ceně nového

Použitý kus 30 % pod `used_median` je pořád špatný nákup, když je nový v akci
za srovnatelné peníze. Alert se nespustí při `total > 0.8 * retail_best`
bez ohledu na skóre.

## Důsledky

Odhad je vychýlený nahoru, dokud nemáme dost zmizelých inzerátů — první měsíc
prakticky jede na prioru. To je záměr: `confidence` to vyjadřuje explicitně
a alerting je podle toho blokovaný.

`gone_at` se musí plnit spolehlivě, jinak celá metoda ztrácí smysl. Watcher
proto musí dělat diff proti předchozímu snapshotu, ne jen append nových
inzerátů.

## Dodatek (2026-08-17): poučení pro fázi cenových alertů

Z návrhové fáze pocházejí dvě chyby alertovacích pravidel, odhalené testy
v prototypu. Modul detekce poklesu ceny zatím v repu není — až se bude
stavět, tahle poučení do něj patří od prvního dne:

1. **„Nejnižší cena za 90 dní" bez prahu citelnosti** se spouštěla při
   poklesu o 1,2 %. Minimum musí být zároveň citelně pod dlouhodobým
   mediánem (`MIN_MEANINGFUL_DROP ≈ 0.05`).
2. **Baseline z klouzavého mediánu je manipulovatelná prodejcem.** Když
   eshop před akcí zvedne cenu z 8 000 na 12 000, nafoukne se i 30denní
   medián a návrat na 7 900 vypadá jako 34% sleva. Baseline proto musí
   brát *nižší* z krátkodobého a dlouhodobého mediánu.

Obecné poučení: každá klouzavá statistika použitá jako reference je
manipulovatelná tím, kdo ceny nastavuje.
