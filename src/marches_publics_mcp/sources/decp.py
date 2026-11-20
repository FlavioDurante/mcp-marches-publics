"""DECP (French public procurement awards) client on the data.economie.gouv.fr ODS dataset.

Dataset ``decp-2022-marches-valides``. It holds no party names (SIRET only): names are filled
through an optional ``CompanyResolver``. Missing values are the string ``"CDL"``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from datetime import date
from typing import Any, Literal, TypeVar

from marches_publics_mcp.http import HttpClient, UpstreamError
from marches_publics_mcp.models import (
    DecpAward,
    DecpRanking,
    DecpSearchResult,
    PageInfo,
    RankEntry,
    truncate,
)
from marches_publics_mcp.sources.entreprises import CompanyResolver
from marches_publics_mcp.sources.ods import and_all, date_literal, in_list, quote

DEFAULT_BASE_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "decp-2022-marches-valides/records"
)
SOURCE = "decp"
logger = logging.getLogger(__name__)
PLACEHOLDER = "CDL"
T = TypeVar("T")
# Place-code types whose value starts with the department code (verified live, 2026-11-20).
# Regions ("Code région": 75, 93...) and countries collide with department numbers, so they
# are excluded; arrondissement/canton codes are rare and left out too.
_DEPT_TYPECODES = ("Code postal", "Code département", "Code commune")
MAX_PAGE = 100  # ODS hard limit per request
MAX_WINDOW = 10_000  # ODS: offset + limit <= 10000

SELECT_FIELDS = (
    "id,objet,codecpv,procedure,acheteur_id,"
    "titulaire_id_1,titulaire_typeidentifiant_1,titulaire_id_2,titulaire_typeidentifiant_2,"
    "titulaire_id_3,titulaire_typeidentifiant_3,montant,datenotification,dureemois,"
    "offresrecues,lieuexecution_code"
)
# Corsica: department codes 2A/2B but postal codes start with 20x.
_CORSICA = {"2A": ("2A", "200", "201"), "2B": ("2B", "202", "206")}

RANK_CAVEATS = [
    "Amounts are the notified amounts; for framework agreements they are maximum amounts, "
    "not money actually spent.",
    "Supplier ranking uses the first co-contractor (titulaire_id_1) of each contract only.",
    "DECP coverage is incomplete: not all buyers publish their data, so rankings are lower "
    "bounds and may under-represent some buyers or regions.",
    "Framework agreements and their subsequent contracts (idaccordcadre) can both appear, so "
    "amounts may be counted twice.",
    "lieuexecution_code is declared upstream and is occasionally wrong.",
    "Suppliers without a SIRET (foreign VAT numbers, etc.) are excluded from supplier rankings.",
]


def _clean(value: Any) -> Any:
    """Map the ``CDL`` placeholder and empty strings to ``None``."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in ("", PLACEHOLDER):
        return None
    return value


def _parse(value: Any, field: str, cast: Callable[[Any], T]) -> T | None:
    """Convert a field with ``cast``; log a warning and return ``None`` when it is malformed."""
    if value is None:
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        logger.warning("malformed upstream value", extra={"source": SOURCE, "field": field})
        return None


def _num(value: float) -> str:
    """Render an amount without exponent or trailing ``.0``."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _digits(value: str, lengths: tuple[int, ...], label: str) -> str:
    """Validate a numeric identifier, ignoring spaces."""
    clean = "".join(value.split())
    if not clean.isdigit() or len(clean) not in lengths:
        raise ValueError(
            f"`{label}` must be {' or '.join(map(str, lengths))} digits (got {value!r})."
        )
    return clean


class DecpClient:
    """Search and rank French public contract awards (DECP)."""

    def __init__(
        self,
        http: HttpClient,
        base_url: str | None = None,
        resolver: CompanyResolver | None = None,
    ) -> None:
        """Create the client.

        Args:
            http: Shared HTTP client.
            base_url: Records endpoint; defaults to ``MCP_MP_DECP_URL`` or the public dataset.
            resolver: Optional company-name resolver used to fill buyer/supplier names.
        """
        self._http = http
        self._url = base_url or os.getenv("MCP_MP_DECP_URL") or DEFAULT_BASE_URL
        self._resolver = resolver

    # ------------------------------------------------------------------ filters
    @staticmethod
    def _where(
        *,
        keywords: str | None = None,
        cpv_prefix: str | None = None,
        buyer_siret: str | None = None,
        supplier_siret: str | None = None,
        department: str | None = None,
        notified_from: date | None = None,
        notified_to: date | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        procedure: str | None = None,
    ) -> str | None:
        """Build the ODSQL ``where`` clause from validated filters."""
        if notified_from and notified_to and notified_from > notified_to:
            raise ValueError("`notified_from` must be on or before `notified_to`.")
        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            raise ValueError("`min_amount` must be lower than or equal to `max_amount`.")
        clauses: list[str | None] = []
        if keywords and keywords.strip():
            clauses.append(f"search(objet, {quote(keywords.strip())})")
        if cpv_prefix:
            cpv = cpv_prefix.strip().split("-")[0]
            if not re.fullmatch(r"\d{2,8}", cpv):
                raise ValueError(
                    "`cpv_prefix` must be 2 to 8 digits, e.g. '9091' for cleaning services."
                )
            clauses.append(f"startswith(codecpv, {quote(cpv)})")
        if buyer_siret:
            clauses.append(f"acheteur_id = {quote(_digits(buyer_siret, (14,), 'buyer_siret'))}")
        if supplier_siret:
            sup = quote(_digits(supplier_siret, (14,), "supplier_siret"))
            clauses.append(" OR ".join(f"titulaire_id_{i} = {sup}" for i in (1, 2, 3)))
        if department:
            dept = department.strip().upper()
            if not re.fullmatch(r"\d{2,3}|2[AB]", dept):
                raise ValueError(
                    "`department` must be a department code such as '31', '2A', '971'."
                )
            prefixes = _CORSICA.get(dept, (dept,))
            starts = " OR ".join(f"startswith(lieuexecution_code, {quote(p)})" for p in prefixes)
            clauses.append(f"({in_list('lieuexecution_typecode', _DEPT_TYPECODES)}) AND ({starts})")
        if notified_from:
            clauses.append(f"datenotification >= {date_literal(notified_from)}")
        if notified_to:
            clauses.append(f"datenotification <= {date_literal(notified_to)}")
        if min_amount is not None:
            clauses.append(f"montant >= {_num(min_amount)}")
        if max_amount is not None:
            clauses.append(f"montant <= {_num(max_amount)}")
        if procedure and procedure.strip():
            clauses.append(f"procedure = {quote(procedure.strip())}")
        return and_all(clauses)

    # ------------------------------------------------------------------ search
    async def search(
        self,
        *,
        keywords: str | None = None,
        cpv_prefix: str | None = None,
        buyer_siret: str | None = None,
        supplier_siret: str | None = None,
        department: str | None = None,
        notified_from: date | None = None,
        notified_to: date | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        procedure: str | None = None,
        order_by: Literal["date", "amount"] = "date",
        offset: int = 0,
        limit: int = 10,
    ) -> DecpSearchResult:
        """Search awarded contracts.

        Args:
            keywords: Words searched in the contract object.
            cpv_prefix: CPV code prefix (2-8 digits), e.g. ``9091``.
            buyer_siret: Buyer SIRET (14 digits).
            supplier_siret: SIRET of any of the first three co-contractors.
            department: Department code; matches department and postal codes of the place.
            notified_from: Earliest notification date.
            notified_to: Latest notification date.
            min_amount: Minimum amount in euros.
            max_amount: Maximum amount in euros.
            procedure: Exact procedure label, e.g. ``Procédure adaptée``.
            order_by: ``date`` (newest first) or ``amount`` (largest first).
            offset: Pagination offset.
            limit: Page size (1-100).

        Returns:
            A paginated result; names are filled when a resolver is configured.

        Raises:
            ValueError: On invalid inputs.
            UpstreamError: When the upstream API fails.
        """
        if not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"`limit` must be between 1 and {MAX_PAGE}.")
        if offset < 0 or offset + limit > MAX_WINDOW:
            raise ValueError(
                f"`offset` + `limit` must be at most {MAX_WINDOW}: narrow the filters instead."
            )
        where = self._where(
            keywords=keywords,
            cpv_prefix=cpv_prefix,
            buyer_siret=buyer_siret,
            supplier_siret=supplier_siret,
            department=department,
            notified_from=notified_from,
            notified_to=notified_to,
            min_amount=min_amount,
            max_amount=max_amount,
            procedure=procedure,
        )
        params: dict[str, Any] = {
            "select": SELECT_FIELDS,
            "where": where,
            "order_by": "montant desc" if order_by == "amount" else "datenotification desc",
            "limit": limit,
            "offset": offset,
        }
        data = await self._http.get_json(self._url, params, source=SOURCE)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise UpstreamError(SOURCE, "unexpected data from upstream")
        awards = [self._award(row) for row in data["results"] if isinstance(row, dict)]
        total = _parse(data.get("total_count"), "total_count", int)
        if total is None:
            total = len(awards)
        if self._resolver is not None and awards:
            await self._fill_names(awards)
        nxt = offset + len(awards)
        return DecpSearchResult(
            page=PageInfo(
                total=total,
                offset=offset,
                returned=len(awards),
                next_offset=(nxt if awards and nxt < total and nxt + limit <= MAX_WINDOW else None),
            ),
            awards=awards,
            query=where,
        )

    @staticmethod
    def _award(row: dict[str, Any]) -> DecpAward:
        """Convert one ODS record into a ``DecpAward``."""
        sirets: list[str] = []
        for i in (1, 2, 3):
            tid = _clean(row.get(f"titulaire_id_{i}"))
            if tid:
                sirets.append(str(tid))
        notified = _clean(row.get("datenotification"))
        offers = _clean(row.get("offresrecues"))
        duration = _clean(row.get("dureemois"))
        amount = _clean(row.get("montant"))
        return DecpAward(
            id=str(row.get("id", "")),
            title=truncate(_clean(row.get("objet"))),
            buyer_siret=_clean(row.get("acheteur_id")),
            supplier_sirets=sirets,
            supplier_names=[None] * len(sirets),
            amount_eur=_parse(amount, "montant", float),
            notified=_parse(
                notified, "datenotification", lambda v: date.fromisoformat(str(v)[:10])
            ),
            cpv=_clean(row.get("codecpv")),
            procedure=_clean(row.get("procedure")),
            place_code=_clean(row.get("lieuexecution_code")),
            duration_months=_parse(duration, "dureemois", int),
            offers_received=int(offers) if offers is not None and str(offers).isdigit() else None,
        )

    async def _fill_names(self, awards: list[DecpAward]) -> None:
        """Resolve buyer and supplier names in place (SIRET identifiers only)."""
        assert self._resolver is not None
        wanted: list[str] = []
        for award in awards:
            if award.buyer_siret:
                wanted.append(award.buyer_siret)
            wanted.extend(s for s in award.supplier_sirets if len(s) == 14 and s.isdigit())
        names = await self._resolver.names_for(wanted)
        for award in awards:
            award.buyer_name = names.get(award.buyer_siret or "")
            award.supplier_names = [names.get(s) for s in award.supplier_sirets]

    # ------------------------------------------------------------------ rank
    async def rank(
        self,
        *,
        by: Literal["buyer", "supplier"],
        order: Literal["count", "amount"] = "count",
        cpv_prefix: str | None = None,
        keywords: str | None = None,
        department: str | None = None,
        buyer_siret: str | None = None,
        notified_from: date | None = None,
        notified_to: date | None = None,
        top: int = 10,
    ) -> DecpRanking:
        """Rank buyers or suppliers by contract count or total amount (aggregated upstream).

        Args:
            by: ``buyer`` (acheteur_id) or ``supplier`` (titulaire_id_1).
            order: Sort by number of contracts or by summed amount.
            cpv_prefix: CPV code prefix (2-8 digits).
            keywords: Words searched in the contract object.
            department: Department code (matches department and postal codes).
            buyer_siret: Restrict to one buyer.
            notified_from: Earliest notification date.
            notified_to: Latest notification date.
            top: Number of entries (1-100).

        Returns:
            The ranking with always-present caveats.

        Raises:
            ValueError: On invalid inputs.
            UpstreamError: When the upstream API fails.
        """
        if by not in ("buyer", "supplier"):
            raise ValueError("`by` must be 'buyer' or 'supplier'.")
        if order not in ("count", "amount"):
            raise ValueError("`order` must be 'count' or 'amount'.")
        if not 1 <= top <= MAX_PAGE:
            raise ValueError(f"`top` must be between 1 and {MAX_PAGE}.")
        base = self._where(
            keywords=keywords,
            cpv_prefix=cpv_prefix,
            buyer_siret=buyer_siret,
            department=department,
            notified_from=notified_from,
            notified_to=notified_to,
        )
        field = "acheteur_id" if by == "buyer" else "titulaire_id_1"
        guards = [f'{field} != "{PLACEHOLDER}"', f"{field} is not null"]
        if by == "supplier":
            guards.append('titulaire_typeidentifiant_1 = "SIRET"')
        where = and_all([base, *guards])
        params: dict[str, Any] = {
            "select": f"{field}, count(*) as n, sum(montant) as total",
            "where": where,
            "group_by": field,
            "order_by": "total desc" if order == "amount" else "n desc",
            "limit": top,
        }
        count_params: dict[str, Any] = {"select": "id", "where": base, "limit": 1}
        data, count = await asyncio.gather(
            self._http.get_json(self._url, params, source="decp"),
            self._http.get_json(self._url, count_params, source="decp"),
        )
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise UpstreamError(SOURCE, "unexpected data from upstream")
        entries = []
        for row in data["results"]:
            if not isinstance(row, dict) or not _clean(row.get(field)):
                continue
            amount = _parse(row.get("total"), "total", float)
            entries.append(
                RankEntry(
                    rank=len(entries) + 1,
                    siret=str(row[field]),
                    contracts=_parse(row.get("n") or 0, "n", int) or 0,
                    total_amount_eur=round(amount, 2) if amount is not None else None,
                )
            )
        if self._resolver is not None and entries:
            names = await self._resolver.names_for([e.siret for e in entries])
            for entry in entries:
                entry.name = names.get(entry.siret)
        filters = {
            k: v
            for k, v in {
                "keywords": keywords,
                "cpv_prefix": cpv_prefix,
                "department": department,
                "buyer_siret": buyer_siret,
                "notified_from": notified_from.isoformat() if notified_from else None,
                "notified_to": notified_to.isoformat() if notified_to else None,
            }.items()
            if v
        }
        return DecpRanking(
            by=by,
            order=order,
            filters=filters,
            total_contracts=(
                (_parse(count.get("total_count"), "total_count", int) or 0)
                if isinstance(count, dict)
                else 0
            ),
            entries=entries,
            caveats=list(RANK_CAVEATS),
        )
