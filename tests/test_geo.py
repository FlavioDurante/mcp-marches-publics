"""Tests for the geography helpers."""

from __future__ import annotations

import re

import pytest

from marches_publics_mcp import geo


def test_department_count_and_pattern() -> None:
    assert len(geo.DEPARTMENTS) == 101
    pattern = re.compile(geo.DEPARTMENT_PATTERN)
    assert all(pattern.match(code) for code in geo.DEPARTMENTS)
    assert not pattern.match("20")
    assert not pattern.match("96")


def test_regions_partition_the_departments() -> None:
    codes = [c for dept in geo.REGIONS.values() for c in dept]
    assert sorted(codes) == sorted(geo.DEPARTMENTS)
    assert len(codes) == len(set(codes))
    assert len(geo.REGIONS) == 18  # 13 metropolitan + 5 overseas


@pytest.mark.parametrize("name", ["Occitanie", "occitanie", "OCCITANIE", " Occitanie "])
def test_region_lookup_is_case_insensitive(name: str) -> None:
    assert "31" in geo.region_departments(name)


@pytest.mark.parametrize("name", ["Île-de-France", "ile-de-france", "Ile de France", "IDF"])
def test_region_lookup_ignores_accents_and_dashes(name: str) -> None:
    assert geo.region_departments(name) == ["75", "77", "78", "91", "92", "93", "94", "95"]


def test_region_alias_paca() -> None:
    assert "13" in geo.region_departments("PACA")


def test_unknown_region_lists_valid_names() -> None:
    with pytest.raises(ValueError, match="Occitanie"):
        geo.region_departments("Atlantide")


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("31", "31"),
        ("2a", "2A"),
        ("974", "974"),
        ("Haute-Garonne", "31"),
        ("haute garonne", "31"),
        ("Côte-d'Or", "21"),
        ("finistere", "29"),
        ("La Réunion", "974"),
        ("9", "09"),
    ],
)
def test_department_code(value: str, code: str) -> None:
    assert geo.department_code(value) == code


def test_unknown_department() -> None:
    with pytest.raises(ValueError, match="Unknown department"):
        geo.department_code("Narnia")


def test_resolve_departments_merges_and_dedupes() -> None:
    assert geo.resolve_departments(None, None) is None
    assert geo.resolve_departments("Bretagne", ["Paris", "35"]) == ["22", "29", "35", "56", "75"]
