"""Pure scoring helpers for the mcp-marches-publics evaluation.

Nothing here touches the network or the MCP server: every function takes plain Python values
so it can be unit-tested (see ``test_scoring.py``).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

ANSWER_TYPES = ("exact", "set", "number_tolerance", "contains", "count_range")

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of scoring one answer."""

    passed: bool
    detail: str


# ---------------------------------------------------------------- normalisation
def normalize_text(value: Any) -> str:
    """Lower-case, strip accents, collapse spaces; digit-only strings lose their spaces.

    Args:
        value: Any value; it is converted with ``str``.

    Returns:
        A comparison-friendly string.
    """
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = " ".join(text.replace(" ", " ").replace(" ", " ").casefold().split())
    compact = text.replace(" ", "")
    return compact if compact.isdigit() else text


def to_number(value: Any) -> float | None:
    """Parse a number from an int/float or a string such as ``"1 234,5"``.

    Args:
        value: Number or text.

    Returns:
        The parsed float, or None when no number can be read (booleans are rejected).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.replace(" ", "").replace(" ", "").replace(" ", "")
    decimal_comma = "," in text and "." not in text
    text = text.replace(",", "." if decimal_comma else "")
    match = _NUMBER.search(text)
    return float(match.group()) if match else None


# ---------------------------------------------------------------- extraction
def extract_path(obj: Any, path: str) -> Any:
    """Read a value with a tiny jsonpath-like syntax: ``a.b[0].c`` or ``entries[*].siret``.

    ``[*]`` maps the rest of the path over a list and returns a list.

    Args:
        obj: Parsed JSON (dicts and lists).
        path: Dotted path; ``""`` returns ``obj`` itself.

    Returns:
        The value found.

    Raises:
        KeyError: If a key or index does not exist.
    """
    if path == "":
        return obj
    head, _, rest = path.partition(".")
    match = re.fullmatch(r"([^\[\]]*)((?:\[(?:\d+|\*)\])*)", head)
    if match is None:
        raise KeyError(f"bad path segment: {head!r}")
    name, indexes = match.group(1), re.findall(r"\[(\d+|\*)\]", match.group(2))
    current = obj
    if name:
        if not isinstance(current, dict) or name not in current:
            raise KeyError(f"missing key {name!r}")
        current = current[name]
    # Rebuild the remaining path so wildcards can recurse over it.
    for position, index in enumerate(indexes):
        if index == "*":
            tail = "".join(f"[{i}]" for i in indexes[position + 1 :])
            sub = (tail + ("." + rest if rest else "")).lstrip(".")
            if not isinstance(current, list):
                raise KeyError("wildcard on a non-list")
            return [extract_path(item, sub) for item in current]
        if not isinstance(current, list) or int(index) >= len(current):
            raise KeyError(f"index {index} out of range")
        current = current[int(index)]
    return extract_path(current, rest) if rest else current


def parse_answer_block(text: str) -> tuple[bool, Any]:
    """Find the last ```json {"answer": ...}``` block of a final agent message.

    Args:
        text: Final assistant text.

    Returns:
        ``(found, value)``; ``value`` is the content of the ``answer`` key.
    """
    for raw in reversed(_JSON_BLOCK.findall(text or "")):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "answer" in data:
            return True, data["answer"]
    return False, None


# ---------------------------------------------------------------- scoring
def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list | tuple | set):
        return list(value)
    if isinstance(value, str):
        return [part for part in re.split(r"[,;\n]", value) if part.strip()]
    return [value]


def _score_number(actual: Any, expected: float, tolerance: float, pct: float) -> ScoreResult:
    number = to_number(actual)
    if number is None:
        return ScoreResult(False, f"not a number: {actual!r}")
    allowed = max(tolerance, abs(expected) * pct / 100.0)
    ok = abs(number - expected) <= allowed
    return ScoreResult(ok, f"got {number:g}, expected {expected:g} +/- {allowed:g}")


def score_answer(
    answer_type: str,
    expected: Any,
    actual: Any,
    tolerance: float = 0.0,
    tolerance_pct: float = 0.0,
) -> ScoreResult:
    """Score one answer against the ground truth.

    Args:
        answer_type: One of ``ANSWER_TYPES``.
        expected: Ground truth (shape depends on the type: str, list, number, ``[lo, hi]``,
            or a dict of numbers for ``number_tolerance`` on composite questions).
        actual: Value extracted from tool results (oracle) or parsed from the agent answer.
        tolerance: Absolute tolerance (``number_tolerance``).
        tolerance_pct: Relative tolerance in percent (``number_tolerance``).

    Returns:
        A ``ScoreResult``.

    Raises:
        ValueError: If ``answer_type`` is unknown.
    """
    if answer_type not in ANSWER_TYPES:
        raise ValueError(f"unknown answer_type {answer_type!r}")
    if actual is None:
        return ScoreResult(False, "no answer")
    if answer_type == "exact":
        ok = normalize_text(actual) == normalize_text(expected)
        return ScoreResult(ok, f"got {actual!r}, expected {expected!r}")
    if answer_type == "set":
        got = {normalize_text(v) for v in _as_list(actual)}
        want = {normalize_text(v) for v in _as_list(expected)}
        return ScoreResult(got == want, f"missing={sorted(want - got)} extra={sorted(got - want)}")
    if answer_type == "number_tolerance":
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                return ScoreResult(False, f"expected an object with keys {sorted(expected)}")
            parts = {
                key: _score_number(actual.get(key), float(want), tolerance, tolerance_pct)
                for key, want in expected.items()
            }
            return ScoreResult(
                all(p.passed for p in parts.values()),
                "; ".join(f"{k}: {p.detail}" for k, p in parts.items()),
            )
        return _score_number(actual, float(expected), tolerance, tolerance_pct)
    if answer_type == "contains":
        haystack_parts = actual if isinstance(actual, list | tuple) else [actual]
        haystack = normalize_text(" | ".join(str(p) for p in haystack_parts))
        squashed = haystack.replace(" ", "")
        wanted = expected if isinstance(expected, list) else [expected]
        missing = [
            w
            for w in wanted
            if normalize_text(w) not in haystack
            and normalize_text(w).replace(" ", "") not in squashed
        ]
        return ScoreResult(not missing, f"missing={missing}")
    # count_range
    low, high = float(expected[0]), float(expected[1])
    number = to_number(actual)
    if number is None:
        return ScoreResult(False, f"not a number: {actual!r}")
    return ScoreResult(low <= number <= high, f"got {number:g}, expected in [{low:g}, {high:g}]")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-item result rows into the summary written next to the details.

    Args:
        rows: Dicts with ``answer_type``, ``passed``, ``tool_calls``, ``invalid_calls`` and
            optionally ``input_tokens``, ``output_tokens``, ``latency_s``.

    Returns:
        Success rate overall and per answer type, mean tool calls, invalid-call rate, token
        totals and mean latency.
    """
    total = len(rows)
    by_type: dict[str, dict[str, int]] = {}
    for row in rows:
        slot = by_type.setdefault(row["answer_type"], {"n": 0, "passed": 0})
        slot["n"] += 1
        slot["passed"] += int(bool(row["passed"]))
    calls = sum(r.get("tool_calls", 0) for r in rows)
    invalid = sum(r.get("invalid_calls", 0) for r in rows)
    latencies = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
    return {
        "n": total,
        "passed": sum(int(bool(r["passed"])) for r in rows),
        "success_rate": (sum(int(bool(r["passed"])) for r in rows) / total) if total else None,
        "by_answer_type": by_type,
        "tool_calls_total": calls,
        "tool_calls_mean": (calls / total) if total else None,
        "invalid_call_rate": (invalid / calls) if calls else 0.0,
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "latency_mean_s": (sum(latencies) / len(latencies)) if latencies else None,
    }
