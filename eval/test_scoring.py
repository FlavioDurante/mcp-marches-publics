"""Unit tests for eval/scoring.py (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from scoring import (
    extract_path,
    normalize_text,
    parse_answer_block,
    pick_agent_value,
    score_answer,
    summarize,
    to_number,
)


def test_normalize_text_accents_and_digits() -> None:
    assert normalize_text("  Énergie  Atomique ") == "energie atomique"
    assert normalize_text("305 163 040 00119") == "30516304000119"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (5, 5.0),
        ("1 234,5", 1234.5),
        ("1,234.5", 1234.5),
        ("1,234", 1234.0),
        ("1,234,567", 1234567.0),
        ("1,5", 1.5),
        ("1 234,56", 1234.56),
        ("1.234,56", 1234.56),
        ("12,50", 12.5),
        ("environ 220 avis", 220.0),
        ("n/a", None),
    ],
)
def test_to_number(raw: object, want: float | None) -> None:
    assert to_number(raw) == want


def test_to_number_rejects_bool() -> None:
    assert to_number(True) is None


def test_extract_path() -> None:
    data = {"page": {"total": 7}, "entries": [{"siret": "1"}, {"siret": "2"}]}
    assert extract_path(data, "page.total") == 7
    assert extract_path(data, "entries[1].siret") == "2"
    assert extract_path(data, "entries[*].siret") == ["1", "2"]
    assert extract_path([[1, 2], [3]], "[0][1]") == 2
    with pytest.raises(KeyError):
        extract_path(data, "entries[5].siret")
    with pytest.raises(KeyError):
        extract_path(data, "nope")


def test_parse_answer_block() -> None:
    text = 'Voici.\n```json\n{"answer": 220}\n```\nFin'
    assert parse_answer_block(text) == (True, 220)
    assert parse_answer_block("pas de bloc") == (False, None)
    two = '```json\n{"answer": 1}\n```\n```json\n{"answer": 2}\n```'
    assert parse_answer_block(two) == (True, 2)
    assert parse_answer_block('```json\n{"autre": 1}\n```') == (False, None)


def test_exact_and_set() -> None:
    assert score_answer("exact", "30516304000119", "305 163 040 00119").passed
    assert not score_answer("exact", "a", "b").passed
    assert score_answer("set", ["1", "2"], ["2", "1"]).passed
    assert score_answer("set", ["1", "2"], "2, 1").passed
    assert not score_answer("set", ["1", "2"], ["1"]).passed


def test_number_tolerance() -> None:
    assert score_answer("number_tolerance", 100, 104, tolerance_pct=5).passed
    assert not score_answer("number_tolerance", 100, 110, tolerance_pct=5).passed
    assert score_answer("number_tolerance", 100, "103", tolerance=3).passed
    assert not score_answer("number_tolerance", 100, "abc").passed


def test_number_tolerance_dict() -> None:
    expected = {"boamp": 37, "ted": 279}
    assert score_answer(
        "number_tolerance", expected, {"boamp": 38, "ted": 280}, tolerance_pct=5
    ).passed
    assert not score_answer("number_tolerance", expected, {"boamp": 38}, tolerance_pct=5).passed
    assert not score_answer("number_tolerance", expected, 5).passed


def test_contains() -> None:
    assert score_answer(
        "contains", ["SICTOM", "2026-12-03"], ["SICTOM Région", "2026-12-03T13:00"]
    ).passed
    assert not score_answer("contains", ["INFRANEO"], "Ville de Paris").passed
    assert score_answer("contains", ["energie"], "Commissariat à l'Énergie Atomique").passed


def test_count_range_and_none() -> None:
    assert score_answer("count_range", [0, 0], 0).passed
    assert score_answer("count_range", [10, 25], "17").passed
    assert not score_answer("count_range", [10, 25], 40).passed
    assert not score_answer("exact", "x", None).passed


def test_unknown_type() -> None:
    with pytest.raises(ValueError):
        score_answer("fuzzy", 1, 1)


def test_summarize() -> None:
    rows = [
        {
            "answer_type": "exact",
            "passed": True,
            "tool_calls": 2,
            "invalid_calls": 0,
            "latency_s": 1.0,
        },
        {
            "answer_type": "exact",
            "passed": False,
            "tool_calls": 2,
            "invalid_calls": 1,
            "latency_s": 3.0,
        },
        {"answer_type": "set", "passed": True, "tool_calls": 0, "invalid_calls": 0},
    ]
    s = summarize(rows)
    assert s["n"] == 3 and s["passed"] == 2
    assert s["by_answer_type"]["exact"] == {"n": 2, "passed": 1}
    assert s["invalid_call_rate"] == 0.25
    assert s["latency_mean_s"] == 2.0
    assert summarize([])["success_rate"] is None


def test_exact_treats_decimal_zero_as_equal_but_keeps_codes() -> None:
    assert score_answer("exact", "1", "1.0").passed
    assert score_answer("exact", 1, "1,0").passed
    assert not score_answer("exact", "01", "1").passed
    assert not score_answer("exact", "1.5", "1").passed


def test_pick_agent_value_scores_block_only_when_present() -> None:
    block = 'blabla 220\n```json\n{"answer": ["x"]}\n```'
    assert pick_agent_value("contains", block) == (["x"], "json_block")
    assert pick_agent_value("contains", "free text") == ("free text", "free_text")
    assert pick_agent_value("exact", "free text") == (None, None)
    assert pick_agent_value("contains", "  ") == (None, None)
