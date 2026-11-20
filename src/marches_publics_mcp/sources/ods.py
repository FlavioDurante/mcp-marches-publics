"""Helpers for the Opendatasoft Explore API v2.1 (ODSQL), used by BOAMP and DECP."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date


def quote(value: str) -> str:
    """Quote a string literal for ODSQL (double quotes, backslash-escaped).

    Args:
        value: Raw user value.

    Returns:
        The quoted literal, e.g. ``"Lyon"``.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def date_literal(value: date) -> str:
    """Render a date as an ODSQL date literal (``date'2026-11-01'``)."""
    return f"date'{value.isoformat()}'"


def in_list(field: str, values: Iterable[str]) -> str:
    """Build ``field IN ("a", "b")`` (works on multivalued fields too)."""
    items = ", ".join(quote(v) for v in values)
    return f"{field} IN ({items})"


def and_all(clauses: Iterable[str | None]) -> str | None:
    """Join non-empty clauses with AND, each wrapped in parentheses.

    Returns:
        The combined clause, or ``None`` when there is nothing to filter on.
    """
    parts = [f"({c})" for c in clauses if c]
    return " AND ".join(parts) if parts else None


# Live 2026-11-20 on BOAMP and DECP: search(field, "IA") is a prefix match ("IARD", "AMIAD");
# field like "IA" is a whole-word match (accent/case-insensitive) that loses plurals.
SHORT_WORD_MAX = 4


def keyword_clause(field: str, keywords: str) -> str:
    """AND of the words: whole word if 4 letters or fewer, prefix search otherwise."""
    words = [w.strip("\"'«»()") for w in keywords.split()]
    parts = [
        f"{field} like {quote(w)}" if len(w) <= SHORT_WORD_MAX else f"search({field}, {quote(w)})"
        for w in words
        if w
    ]
    return and_all(parts) or ""


def exclude_clause(field: str, words: str) -> str:
    """AND of NOT clauses: drop records whose ``field`` contains any of ``words``."""
    return and_all(f"NOT ({keyword_clause(field, w)})" for w in words.split() if w.strip()) or ""
