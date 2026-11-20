"""TED (Tenders Electronic Daily, EU) Search API v3 client.

Endpoint: ``POST https://api.ted.europa.eu/v3/notices/search`` with a JSON body
``{"query": <expert query>, "fields": [...], "limit": n, "page": p}``.

Verified facts (live, 2026-11-20):
    * Access: anonymous, no API key for search ("openly accessible",
      https://docs.ted.europa.eu/api/latest/search.html).
    * Rate limits: the API itself documents none (no ``X-RateLimit`` header seen). The TED
      fair-usage policy (https://ted.europa.eu/en/simap/developers-corner-for-reusers,
      "Fair usage policy on TED") quotes: "700 requests in the last minute" per IP, and
      "600 visualizations or downloads in less than 6 minutes". Stay well below.
    * Pagination: ``limit`` <= 250 (``SEARCH_EXCEEDS_MAX_LIMIT``), ``page`` is 1-based and
      ``page x limit`` <= 15000 (``SEARCH_WINDOW_TOO_WIDE``). Beyond that window TED offers an
      iteration mode (``paginationMode: "ITERATION"`` + ``iterationNextToken``); not used here:
      narrow the filters (dates, CPV, country) instead. ``totalNoticeCount`` gives the total.
    * Errors: HTTP 400 with ``{"message": ..., "error": {"type": ...}}``; the message is surfaced
      by ``HttpClient`` in ``UpstreamError``. Unknown ``fields`` produce a 400 listing every
      supported field (1830 at check time).
    * Reuse: notices "can be freely reused, for commercial or non-commercial purposes" under the
      Commission reuse policy (Decision 2011/833/EU), see https://ted.europa.eu/en/legal-notice .
      SIMAP/TED logos are not covered.

Expert query syntax that works (dates are ``YYYYMMDD``)::

    FT ~ "logiciel" AND FT ~ "maintenance"          # full text, one clause per word
    classification-cpv IN (72000000)                # hierarchical: also matches 72510000
    place-of-performance IN (FRA, DEU)              # ISO alpha-3 (NUTS codes also stored)
    publication-date>=20261101 AND publication-date<=20261120
    deadline-receipt-tender-date-lot>=20261120      # still open
    notice-type IN (cn-standard)                    # cn-standard, can-standard, pin-only...
    publication-number=757212-2026
    ... SORT BY publication-date DESC

Country filter: ``place-of-performance`` (where the work is done, which is what a bidder asks
about: "tenders in Belgium"). ``buyer-country`` differs for cross-border buyers (e.g. EU
institutions, foreign agencies): for FRA in October 2026, CPV 72*, 279 vs 316 notices.

Field names (dict fields map language code -> text or list of texts): ``title-proc`` (clean
title, preferred), ``notice-title`` (prefixed with country and CPV label; fallback),
``buyer-name``, ``buyer-country``, ``publication-date``, ``deadline-receipt-tender-date-lot``,
``notice-type``, ``classification-cpv``, ``place-of-performance``, ``description-proc``,
``description-lot``, ``estimated-value-proc`` + ``estimated-value-cur-proc`` (lot level:
``estimated-value-lot``), ``procedure-type``, ``winner-name``, ``result-value-notice`` +
``result-value-cur-notice`` (also ``total-value`` / ``total-value-cur``), ``links``.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from marches_publics_mcp.http import HttpClient, NotFoundError, UpstreamError
from marches_publics_mcp.models import (
    TEXT_MAX,
    PageInfo,
    TedNotice,
    TedNoticeDetail,
    TedSearchResult,
    truncate,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.ted.europa.eu/v3/notices/search"
SOURCE = "ted"
MAX_LIMIT = 250
MAX_WINDOW = 15000
LANG_PREFERENCE = ("fra", "eng")
BRUSSELS = ZoneInfo("Europe/Brussels")
_ID_RE = re.compile(r"^\d{1,8}-\d{4}$")
_CPV_RE = re.compile(r"^\d{2,8}(-\d)?$")
_COUNTRY_RE = re.compile(r"^[A-Z]{3}$")
_NOTICE_TYPE_RE = re.compile(r"^[a-z0-9-]+$")

LIST_FIELDS = [
    "publication-number",
    "title-proc",
    "notice-title",
    "buyer-name",
    "buyer-country",
    "publication-date",
    "deadline-receipt-tender-date-lot",
    "notice-type",
    "classification-cpv",
    "place-of-performance",
    "links",
]
DETAIL_FIELDS = [
    *LIST_FIELDS,
    "description-proc",
    "description-lot",
    "estimated-value-proc",
    "estimated-value-cur-proc",
    "estimated-value-lot",
    "estimated-value-cur-lot",
    "procedure-type",
    "winner-name",
    "result-value-notice",
    "result-value-cur-notice",
    "total-value",
    "total-value-cur",
    "tender-value-cur",
]


def _ft_literal(term: str) -> str:
    """Quote a full-text term for the TED expert query (escape backslash and quote)."""
    return '"' + term.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(
    *,
    keywords: str | None = None,
    cpv: str | None = None,
    countries: list[str] | None = None,
    published_from: date | None = None,
    published_to: date | None = None,
    open_only: bool = False,
    notice_type: str | None = None,
    today: date | None = None,
) -> str:
    """Build a TED expert query from validated filters.

    Args:
        keywords: Free text; each whitespace-separated word becomes an ANDed ``FT ~`` clause.
        cpv: CPV code (2 to 8 digits, optional ``-N`` check digit); matches the whole subtree.
        countries: ISO alpha-3 buyer countries, e.g. ``["FRA"]``.
        published_from: First publication date (inclusive).
        published_to: Last publication date (inclusive).
        open_only: Keep only notices whose tender deadline is today or later.
        notice_type: TED notice type, e.g. ``cn-standard`` or ``can-standard``.
        today: Override for "today" (tests).

    Returns:
        The query string, ending with ``SORT BY publication-date DESC``.

    Raises:
        ValueError: If a value is malformed (the message says how to fix it).
    """
    if published_from and published_to and published_from > published_to:
        raise ValueError("published_from must be on or before published_to.")
    clauses: list[str] = []
    for word in (keywords or "").replace('"', " ").split():
        clauses.append(f"FT ~ {_ft_literal(word)}")
    if cpv:
        code = cpv.strip()
        if not _CPV_RE.match(code):
            raise ValueError(f"cpv must be 2 to 8 digits, e.g. '72000000' (got {cpv!r}).")
        clauses.append(f"classification-cpv IN ({code.split('-')[0]})")
    if countries:
        codes = [c.strip().upper() for c in countries]
        bad = [c for c in codes if not _COUNTRY_RE.match(c)]
        if bad:
            raise ValueError(f"countries must be ISO alpha-3 codes like FRA, DEU (got {bad}).")
        clauses.append(f"place-of-performance IN ({', '.join(codes)})")
    if published_from:
        clauses.append(f"publication-date>={published_from:%Y%m%d}")
    if published_to:
        clauses.append(f"publication-date<={published_to:%Y%m%d}")
    if open_only:
        clauses.append(f"deadline-receipt-tender-date-lot>={(today or _today()):%Y%m%d}")
    if notice_type:
        kind = notice_type.strip()
        if not _NOTICE_TYPE_RE.match(kind):
            raise ValueError(
                f"notice_type must look like 'cn-standard' or 'can-standard' (got {notice_type!r})."
            )
        clauses.append(f"notice-type IN ({kind})")
    if not clauses:
        # TED rejects an empty query; "all notices since the start of the current year" is safe.
        clauses.append(f"publication-date>={_today().year}0101")
    return " AND ".join(clauses) + " SORT BY publication-date DESC"


def _today() -> date:
    """Today's date in Europe/Brussels (TED publishes on Central European time)."""
    return datetime.now(BRUSSELS).date()


def _unescape(text: str) -> str:
    """Decode (possibly double-encoded) HTML entities found in TED texts."""
    return html.unescape(html.unescape(text))


def _pick_lang(value: Any) -> str | None:
    """Pick one text from a multilingual field (fra, then eng, then first available).

    Accepts ``{lang: str}``, ``{lang: [str, ...]}``, a plain string or a list of strings.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        for lang in (*LANG_PREFERENCE, *value):
            if lang in value:
                text = _pick_lang(value[lang])
                if text:
                    return text
        return None
    if isinstance(value, list):
        for item in value:
            text = _pick_lang(item)
            if text:
                return text
        return None
    text = _unescape(str(value)).strip()
    return text or None


def _all_texts(value: Any) -> list[str]:
    """All distinct texts of a multilingual field in the preferred language."""
    if isinstance(value, dict):
        for lang in (*LANG_PREFERENCE, *value):
            if lang in value:
                return _all_texts(value[lang])
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if item is None:
            continue
        text = _unescape(str(item)).strip()
        if text and text not in out:
            out.append(text)
    return out


def _as_list(value: Any) -> list[str]:
    """Distinct non-empty string items of a scalar-or-list field, order preserved."""
    items = value if isinstance(value, list) else [] if value is None else [value]
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _day(value: Any) -> str | None:
    """Reduce ``2026-11-02+01:00`` to ``2026-11-02`` (earliest when a list is given)."""
    days = sorted(v[:10] for v in _as_list(value))
    return days[0] if days else None


def _deadline(value: Any, today: date | None = None) -> str | None:
    """Pick the tender deadline among lot deadlines: earliest one not yet past, else latest.

    Args:
        value: One date string or a list (one per lot, e.g. ``2026-12-01+01:00``).
        today: Override for "today" in Europe/Brussels (tests).

    Returns:
        An ISO day (``YYYY-MM-DD``) or ``None`` when there is no deadline.
    """
    days = sorted(v[:10] for v in _as_list(value))
    if not days:
        return None
    limit = (today or _today()).isoformat()
    upcoming = [d for d in days if d >= limit]
    return upcoming[0] if upcoming else days[-1]


def _number(value: Any) -> float | None:
    """Parse a TED amount (string or number, first of a list); 0 means unknown -> ``None``."""
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _link(links: Any, kind: str) -> str | None:
    """Pick a notice link of ``kind`` (``html``/``pdf``): French, then English, then any."""
    group = links.get(kind) if isinstance(links, dict) else None
    if not isinstance(group, dict):
        return None
    for lang in ("FRA", "ENG", *group):
        if group.get(lang):
            return str(group[lang])
    return None


def _str_or_none(value: Any) -> str | None:
    """Return ``value`` as a string when it is a non-empty scalar, else ``None``."""
    if value is None or isinstance(value, dict | list):
        return None
    return str(value).strip() or None


def _first(value: Any) -> str | None:
    items = _as_list(value)
    return items[0] if items else None


def _parse_notice(raw: dict[str, Any]) -> TedNotice:
    """Map one raw TED notice to ``TedNotice``."""
    title = _pick_lang(raw.get("title-proc")) or _pick_lang(raw.get("notice-title"))
    return TedNotice(
        publication_number=str(raw.get("publication-number", "")),
        title=truncate(title),
        buyer=truncate(_pick_lang(raw.get("buyer-name"))),
        buyer_country=_first(raw.get("buyer-country")),
        published=_day(raw.get("publication-date")),
        deadline=_deadline(raw.get("deadline-receipt-tender-date-lot")),
        notice_type=_str_or_none(raw.get("notice-type")),
        cpv=_as_list(raw.get("classification-cpv")),
        places=_as_list(raw.get("place-of-performance")),
        url=_link(raw.get("links"), "html"),
    )


def _parse_detail(raw: dict[str, Any]) -> TedNoticeDetail:
    """Map one raw TED notice to ``TedNoticeDetail``."""
    base = _parse_notice(raw).model_dump()
    description = _pick_lang(raw.get("description-proc"))
    if not description:
        description = " ".join(_all_texts(raw.get("description-lot"))) or None
    estimated = _number(raw.get("estimated-value-proc"))
    currency = _first(raw.get("estimated-value-cur-proc"))
    if estimated is None:
        lots = [_number(v) for v in _as_list(raw.get("estimated-value-lot"))]
        known = [v for v in lots if v is not None]
        estimated = sum(known) if known else None
        currency = _first(raw.get("estimated-value-cur-lot")) or currency
    award = _number(raw.get("result-value-notice"))
    award_cur = _first(raw.get("result-value-cur-notice"))
    if award is None:
        award = _number(raw.get("total-value"))
        award_cur = _first(raw.get("total-value-cur")) or _first(raw.get("tender-value-cur"))
    base.update(
        description=truncate(description, TEXT_MAX),
        estimated_value=estimated,
        currency=currency if estimated is not None else award_cur,
        procedure=_str_or_none(raw.get("procedure-type")),
        winners=_all_texts(raw.get("winner-name")),
        award_value=award,
        pdf_url=_link(raw.get("links"), "pdf"),
    )
    return TedNoticeDetail(**base)


class TedClient:
    """Client for the TED Search API v3 (anonymous)."""

    def __init__(self, http: HttpClient, base_url: str | None = None) -> None:
        """Create the client.

        Args:
            http: Shared HTTP client (retries, cache, pacing).
            base_url: Search endpoint; defaults to ``MCP_MP_TED_URL`` or the public API.
        """
        self._http = http
        self._url = base_url or os.getenv("MCP_MP_TED_URL") or DEFAULT_BASE_URL

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        data = await self._http.post_json(self._url, body, source=SOURCE)
        if not isinstance(data, dict):
            raise UpstreamError(SOURCE, "unexpected response shape from TED")
        return data

    async def search(
        self,
        *,
        keywords: str | None = None,
        cpv: str | None = None,
        countries: list[str] | None = None,
        published_from: date | None = None,
        published_to: date | None = None,
        open_only: bool = False,
        notice_type: str | None = None,
        page: int = 1,
        limit: int = 10,
    ) -> TedSearchResult:
        """Search TED notices, newest first.

        Args:
            keywords: Free text (all words must match).
            cpv: CPV code; matches the code and its descendants.
            countries: ISO alpha-3 buyer countries, e.g. ``["FRA"]``.
            published_from: First publication date (inclusive).
            published_to: Last publication date (inclusive).
            open_only: Only notices whose tender deadline has not passed.
            notice_type: e.g. ``cn-standard`` (contract notice), ``can-standard`` (award).
            page: 1-based page number.
            limit: Notices per page (1 to 250).

        Returns:
            A page of notices with pagination info and the query sent.

        Raises:
            ValueError: On invalid filters or an out-of-range page window.
            UpstreamError: If TED fails or rejects the query.
        """
        if page < 1:
            raise ValueError("page starts at 1.")
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}.")
        if page * limit > MAX_WINDOW:
            raise ValueError(
                f"TED only serves the first {MAX_WINDOW} results (page x limit); "
                "narrow the search with dates, cpv or countries instead of paging further."
            )
        query = build_query(
            keywords=keywords,
            cpv=cpv,
            countries=countries,
            published_from=published_from,
            published_to=published_to,
            open_only=open_only,
            notice_type=notice_type,
        )
        data = await self._post(
            {"query": query, "fields": LIST_FIELDS, "limit": limit, "page": page}
        )
        raw_notices = data.get("notices") or []
        if not isinstance(raw_notices, list):
            raise UpstreamError(SOURCE, "unexpected data from upstream")
        notices: list[TedNotice] = []
        for raw in raw_notices:
            try:
                notices.append(_parse_notice(raw))
            except (TypeError, ValueError, AttributeError):
                logger.warning("skipping malformed TED notice", extra={"source": SOURCE})
        try:
            total = int(data.get("totalNoticeCount") or 0)
        except (TypeError, ValueError):
            logger.warning("malformed totalNoticeCount", extra={"source": SOURCE})
            total = len(notices)
        offset = (page - 1) * limit
        has_next = offset + limit < total and (page + 1) * limit <= MAX_WINDOW
        logger.info("ted search", extra={"source": SOURCE, "total": total, "page": page})
        return TedSearchResult(
            page=PageInfo(
                total=total,
                offset=offset,
                returned=len(notices),
                next_offset=offset + limit if has_next else None,
            ),
            notices=notices,
            query=query,
            next_page=page + 1 if has_next else None,
        )

    async def get_notice(self, publication_number: str) -> TedNoticeDetail:
        """Fetch one notice with its detail fields.

        Args:
            publication_number: TED id ``NNNNNN-YYYY``, e.g. ``757212-2026``.

        Returns:
            The compact notice detail (winners and award value for result notices).

        Raises:
            ValueError: If the id format is invalid.
            NotFoundError: If TED has no such notice.
            UpstreamError: If TED fails.
        """
        number = publication_number.strip()
        if not _ID_RE.match(number):
            raise ValueError(
                f"publication_number must look like '757212-2026' (got {publication_number!r})."
            )
        data = await self._post(
            {
                "query": f"publication-number={number}",
                "fields": DETAIL_FIELDS,
                "limit": 1,
                "page": 1,
            }
        )
        notices = data.get("notices") or []
        if not notices:
            raise NotFoundError(SOURCE, f"no TED notice {number}", 404)
        try:
            return _parse_detail(notices[0])
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            raise UpstreamError(SOURCE, "unexpected data from upstream") from exc
