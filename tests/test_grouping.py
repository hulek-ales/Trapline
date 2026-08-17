"""Testy seskupování barevných variant a tolerantního parsování LLM."""

from __future__ import annotations

import pytest

from trapline import grouping, llm

# --- family_key ------------------------------------------------------------

def test_barevne_varianty_stejny_klic():
    base = "Chladnička BestBerg BBFR-95X{} / 84 l / LED osvětlení / {}"
    a = grouping.family_key("BestBerg", base.format("B", "černá"))
    b = grouping.family_key("BestBerg", base.format("S", "stříbrná"))
    c = grouping.family_key("BestBerg", base.format("W", "bílá"))
    assert a == b == c


def test_mini_chladnicky_varianty():
    base = "Mini chladnička BestBerg BBFR-48{} / 45 l / {}"
    a = grouping.family_key("BestBerg", base.format("W", "bílá"))
    b = grouping.family_key("BestBerg", base.format("B", "černá"))
    assert a == b


def test_velikostni_rady_zustavaji_oddelene():
    base = "Přenosná kompresorová autochladnička BestBerg BBPF-{0}A / {0} l"
    a = grouping.family_key("BestBerg", base.format(30))
    b = grouping.family_key("BestBerg", base.format(40))
    assert a != b


def test_luklandy_zustavaji_oddelene():
    a = grouping.family_key("Lukland", "Chladicí box 12 litrů - Lukland")
    b = grouping.family_key("Lukland", "Chladicí box 26 litrů - Lukland")
    assert a != b


def test_dvouzonova_se_neslucuje_s_jednozonovou():
    a = grouping.family_key(
        "BestBerg",
        "Dvouzónová přenosná kompresorová autochladnička BestBerg BBPF-65D / 65 l",
    )
    b = grouping.family_key(
        "BestBerg",
        "Přenosná kompresorová autochladnička BestBerg BBPF-50A / 50 l",
    )
    assert a != b


def test_ruzne_znacky_se_neslucuji():
    a = grouping.family_key("BestBerg", "Chladnička X100 černá")
    b = grouping.family_key("Klarstein", "Chladnička X100 černá")
    assert a != b


# --- family_title ----------------------------------------------------------

def test_family_title_spolecny_prefix():
    t = grouping.family_title([
        "Chladnička BestBerg BBFR-95XB / 84 l / LED osvětlení / černá",
        "Chladnička BestBerg BBFR-95XS / 84 l / LED osvětlení / stříbrná",
    ])
    assert t == "Chladnička BestBerg BBFR-95X"


def test_family_title_jeden_kus():
    assert grouping.family_title(["Solo produkt"]) == "Solo produkt"


# --- llm.parse_content -----------------------------------------------------

def test_parse_cisty_json():
    assert llm.parse_content('{"a": 1}') == {"a": 1}


def test_parse_markdown_plot():
    assert llm.parse_content('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_text_okolo():
    assert llm.parse_content('Výsledek: {"a": 1} hotovo') == {"a": 1}


def test_parse_prazdna_odpoved():
    with pytest.raises(ValueError, match="prázdná"):
        llm.parse_content("   ")


def test_parse_nesmysl():
    with pytest.raises(ValueError, match="není JSON"):
        llm.parse_content("tady žádný JSON není")
