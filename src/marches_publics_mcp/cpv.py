"""CPV 2008 (Common Procurement Vocabulary) lookups: labels, prefix expansion, text search.

The nomenclature (9454 codes, FR + EN labels) ships in ``data/cpv_2008.json.gz``; see
``data/README.md`` for the source. It is loaded lazily on first use and cached.
Codes are always 8 digits without the check digit (``"72212000"``). A code's level is given by
its significant digits: ``72000000`` is division ``72``, ``72200000`` group ``722``, etc.
"""

from __future__ import annotations

import gzip
import json
import re
import unicodedata
from functools import lru_cache
from importlib import resources
from typing import Literal

DATA_FILE = "cpv_2008.json.gz"
_CODE_RE = re.compile(r"^\s*(\d{8})(?:-\d)?\s*$")
_WORD_RE = re.compile(r"[a-z0-9]+")


@lru_cache(maxsize=1)
def _load() -> tuple[tuple[str, str, str], ...]:
    """Load the packaged nomenclature once as ``(code, label_fr, label_en)`` tuples."""
    raw = resources.files("marches_publics_mcp").joinpath("data", DATA_FILE).read_bytes()
    payload = json.loads(gzip.decompress(raw))
    return tuple((c, fr, en) for c, fr, en in payload["codes"])


@lru_cache(maxsize=1)
def _labels() -> dict[str, tuple[str, str]]:
    """Map code → ``(label_fr, label_en)``."""
    return {code: (fr, en) for code, fr, en in _load()}


def _fold(text: str) -> str:
    """Lower-case ``text`` and strip accents."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _words(text: str) -> list[str]:
    """Split ``text`` into accent-free lower-case alphanumeric words."""
    return _WORD_RE.findall(_fold(text))


@lru_cache(maxsize=2)
def _index(lang: str) -> tuple[tuple[str, str, frozenset[str]], ...]:
    """Per-language search index: ``(code, label, folded words)``."""
    pos = 1 if lang == "fr" else 2
    return tuple((row[0], row[pos], frozenset(_words(row[pos]))) for row in _load())


def label(code: str, lang: Literal["fr", "en"] = "fr") -> str | None:
    """Return the label of a CPV code.

    Args:
        code: 8-digit CPV code (a ``-<check digit>`` suffix is tolerated).
        lang: ``"fr"`` or ``"en"``.

    Returns:
        The label, or ``None`` if the code is unknown.
    """
    match = _CODE_RE.match(code or "")
    found = _labels().get(match.group(1)) if match else None
    if found is None:
        return None
    return found[1] if lang == "en" else found[0]


def expand(prefix: str, max_codes: int = 120) -> list[str]:
    """Return every 8-digit CPV code starting with ``prefix``.

    Args:
        prefix: 2 to 7 digits (``"72"``, ``"7221"``) or a full 8-digit code (returned as is
            when it exists, together with nothing else).
        max_codes: Safety cap on the result size (BOAMP query length).

    Returns:
        Sorted list of codes, the shortest-level code (e.g. ``72000000``) included.

    Raises:
        ValueError: If ``prefix`` is not 2-8 digits, matches no code, or matches more than
            ``max_codes`` codes (the message suggests a longer prefix).
    """
    clean = (prefix or "").strip()
    if not re.fullmatch(r"\d{2,8}", clean):
        raise ValueError(
            f"CPV prefix {prefix!r} must be 2 to 8 digits (e.g. '72' or '7221'). "
            "Use search_cpv_codes to find a code from words."
        )
    codes = [code for code, _fr, _en in _load() if code.startswith(clean)]
    if not codes:
        raise ValueError(
            f"no CPV code starts with {clean!r}. Use search_cpv_codes to find a code from words."
        )
    if len(codes) > max_codes:
        longer = sorted({code[: len(clean) + 1] for code in codes})
        raise ValueError(
            f"CPV prefix {clean!r} covers {len(codes)} codes (max {max_codes}). Use a longer "
            f"prefix, e.g. {', '.join(repr(p) for p in longer[:6])}, or search_cpv_codes."
        )
    return codes


def _specificity(code: str) -> int:
    """Number of significant digits (``72000000`` → 2); lower means a broader code."""
    return len(code.rstrip("0")) or 1


def search(text: str, lang: Literal["fr", "en"] = "fr", limit: int = 10) -> list[tuple[str, str]]:
    """Find CPV codes from words, ignoring case and accents.

    Every query word must match the start of a label word (``"nettoy"`` finds ``"nettoyage"``).
    Ranking: more exact-word matches first, then broader (shorter) codes, then code order.

    Args:
        text: Free text, e.g. ``"nettoyage locaux"`` or ``"software development"``.
        lang: Label language, ``"fr"`` or ``"en"``.
        limit: Maximum number of results.

    Returns:
        ``(code, label)`` pairs, best first. Empty if ``text`` has no word or nothing matches.
    """
    query = _words(text)
    if not query or limit <= 0:
        return []
    scored: list[tuple[int, int, int, str, str]] = []
    for code, lab, words in _index(lang):
        exact = 0
        for q in query:
            if q in words:
                exact += 1
            elif not any(w.startswith(q) for w in words):
                break
        else:
            # Exact words first, then the most focused label, then the broadest code.
            scored.append((-exact, len(words), _specificity(code), code, lab))
    scored.sort()
    return [(code, lab) for *_rank, code, lab in scored[:limit]]
