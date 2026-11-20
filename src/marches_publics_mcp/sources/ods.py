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
