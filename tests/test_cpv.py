"""Tests for the CPV nomenclature helpers."""

from __future__ import annotations

import pytest

from marches_publics_mcp import cpv


def test_nomenclature_is_complete_and_well_formed() -> None:
    rows = cpv._load()
    assert len(rows) == 9454
    assert all(len(code) == 8 and code.isdigit() for code, _fr, _en in rows)
    assert all(fr and en for _c, fr, en in rows)


def test_label_both_languages_and_check_digit() -> None:
    assert cpv.label("72000000") == cpv.label("72000000-5")
    assert "technologies de l'information" in (cpv.label("72000000") or "").lower()
    assert "IT services" in (cpv.label("72000000", "en") or "")
    assert cpv.label("00000001") is None
    assert cpv.label("72") is None
    assert cpv.label("") is None


def test_expand_prefix_includes_subcodes() -> None:
    codes = cpv.expand("72212", max_codes=200)
    assert "72212000" in codes and "72212100" in codes
    assert all(c.startswith("72212") for c in codes)
    assert codes == sorted(codes)


def test_expand_full_code_and_errors() -> None:
    assert cpv.expand("72212000") == ["72212000"]
    with pytest.raises(ValueError, match="longer prefix"):
        cpv.expand("72")
    with pytest.raises(ValueError, match="2 to 8 digits"):
        cpv.expand("7")
    with pytest.raises(ValueError, match="no CPV code"):
        cpv.expand("99")
    assert len(cpv.expand("72", max_codes=10_000)) > 120


def test_expand_error_suggests_children() -> None:
    with pytest.raises(ValueError, match="'722'"):
        cpv.expand("72")


def test_search_accent_and_case_insensitive() -> None:
    plain = cpv.search("nettoyage")
    assert plain and plain == cpv.search("NETTOYAGE")
    assert cpv.search("electricite") == cpv.search("Électricité")
    assert all("nettoyage" in lab.lower() for _c, lab in plain)


def test_search_english_and_ranking() -> None:
    hits = cpv.search("cleaning", lang="en", limit=5)
    assert 0 < len(hits) <= 5
    assert any(code.startswith("909") for code, _l in hits)
    # Focused labels ("Cleaning services") rank before long labels that merely mention the word.
    top3 = [code for code, _l in cpv.search("cleaning", "en", 3)]
    assert "90910000" in top3
    assert "90910000" in [code for code, _l in cpv.search("nettoyage", "fr", 3)]


def test_search_requires_all_words_and_handles_empty() -> None:
    assert cpv.search("") == []
    assert cpv.search("   !!") == []
    assert cpv.search("zzzzqqqq") == []
    assert cpv.search("nettoyage", limit=0) == []
    narrow = cpv.search("nettoyage locaux", limit=100)
    assert all("locaux" in lab.lower() for _c, lab in narrow)


def test_data_file_is_packaged_resource() -> None:
    from importlib import resources

    assert resources.files("marches_publics_mcp").joinpath("data", cpv.DATA_FILE).is_file()
