"""MCP server exposing French and EU public procurement data (BOAMP, DECP, TED).

Design notes (see Anthropic, "Writing effective tools for agents"): few tools with clear
boundaries, descriptions that say when *not* to use a tool, compact structured outputs,
actionable error messages and an actionable ``hint`` when a result is empty.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, ToolAnnotations
from pydantic import BaseModel, Field

from marches_publics_mcp import __version__, geo
from marches_publics_mcp import cpv as cpv_codes
from marches_publics_mcp.http import HttpClient, NotFoundError, UpstreamError
from marches_publics_mcp.logging_setup import setup_logging
from marches_publics_mcp.models import (
    BoampNoticeDetail,
    BoampSearchResult,
    DecpRanking,
    DecpSearchResult,
    TedNoticeDetail,
    TedSearchResult,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

INSTRUCTIONS = """\
Open public-procurement data: France (BOAMP notices, DECP awarded contracts) and EU (TED).

Which tool for which question
- Open or past tenders/notices in France -> search_boamp_notices, then get_boamp_notice(id).
- Who won which French contract, for how much, and rankings of top buyers or suppliers ->
  search_decp_awards (list) or rank_decp_parties (aggregate "who buys/wins the most").
- Tenders or notices published EU-wide or abroad -> search_ted_notices, then get_ted_notice.
- A SIRET/SIREN -> company name: lookup_company.
- Words -> CPV codes (e.g. "cleaning", "travaux de voirie"): search_cpv_codes.
Do not use TED for French-only questions (BOAMP is fresher and filters by department). DECP
only lists awarded contracts, never open tenders.

Conventions
- Dates are ISO (YYYY-MM-DD). CPV codes are digits only (no check digit). A 2-7 digit prefix
  covers the whole branch (72 = all IT services) on BOAMP, DECP and TED; an 8-digit code on BOAMP
  matches that exact code only. Unsure of the code? search_cpv_codes("nettoyage").
- Lists are paginated: pass the `next_offset` of a result as `offset` (TED: pass `next_page`
  as `page`).
  Default limit is 10, maximum 50. Prefer narrowing filters over paging through many pages.
- DECP carries only SIRET identifiers; buyer and supplier names are resolved automatically
  through the public company directory (a name may be null when unknown).
- Results are open data and may lag the source by days; they are not legal advice.
"""

# recherche-entreprises documents 7 requests/s: stay a bit below.
_MIN_INTERVAL = {"recherche-entreprises.api.gouv.fr": 0.15}

_RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)

# --------------------------------------------------------------------------- input types
Limit = Annotated[int, Field(ge=1, le=50, description="Maximum number of results per page (1-50).")]
Offset = Annotated[
    int,
    Field(ge=0, le=9999, description="Pagination offset: use `next_offset` of the previous page."),
]
Keywords = Annotated[
    str | None,
    Field(
        description="Free-text words searched in the title/object. Short and in French for "
        "BOAMP/DECP.",
        examples=["maintenance logiciel", "nettoyage locaux"],
        max_length=200,
    ),
]
CpvExact = Annotated[
    str | None,
    Field(
        pattern=r"^\d{8}$",
        description="CPV code, 8 digits, no check digit (e.g. 72000000 = IT services).",
        examples=["72000000", "45000000"],
    ),
]
CpvBoamp = Annotated[
    str | None,
    Field(
        pattern=r"^\d{2,8}$",
        description=(
            "CPV prefix (2-7 digits, whole branch: 72 = all IT services, max 300 codes) or exact "
            "8-digit code (that code only). No check digit."
        ),
        examples=["72", "9091", "72212000"],
    ),
]
CpvPrefix = Annotated[
    str | None,
    Field(
        pattern=r"^\d{2,8}$",
        description="CPV code or prefix, 2-8 digits (e.g. 72 = IT, 7222 = IT consulting).",
        examples=["72", "45233"],
    ),
]
Siret = Annotated[str | None, Field(pattern=r"^\d{14}$", description="14-digit SIRET.")]
DepartmentCode = Annotated[
    str | None,
    Field(
        pattern=geo.DEPARTMENT_PATTERN,
        description="Department code: 01-95, 2A, 2B or 971-976.",
        examples=["31", "2A", "974"],
    ),
]
DateFrom = Annotated[
    date | None, Field(description="Earliest date, ISO YYYY-MM-DD.", examples=["2026-01-01"])
]
DateTo = Annotated[
    date | None, Field(description="Latest date, ISO YYYY-MM-DD.", examples=["2026-11-20"])
]


# --------------------------------------------------------------------------- output wrappers
class _Hinted(BaseModel):
    """Mixin: ``hint`` is filled only when the result is empty."""

    hint: str | None = Field(default=None, description="What to change when nothing matched.")


class BoampSearchResponse(BoampSearchResult, _Hinted):
    """BOAMP search result plus the department codes actually applied."""

    departments_used: list[str] = Field(
        default=[], description="Department codes derived from `region`/`departments`."
    )


class DecpSearchResponse(DecpSearchResult, _Hinted):
    """DECP search result with an empty-result hint."""


class DecpRankingResponse(DecpRanking, _Hinted):
    """DECP ranking with an empty-result hint."""


class TedSearchResponse(TedSearchResult, _Hinted):
    """TED search result with an empty-result hint."""


class CpvMatch(BaseModel):
    """One CPV code with its label."""

    code: str
    label: str


class CpvSearchResult(_Hinted):
    """CPV codes matching a text query."""

    matches: list[CpvMatch]


class CompanyInfo(_Hinted):
    """Result of a company-name lookup."""

    identifier: str = Field(description="The SIRET or SIREN that was looked up.")
    name: str | None = Field(description="Company name, null when unknown.")


# --------------------------------------------------------------------------- lazy clients
class _Clients:
    """Holds source clients; builds missing ones lazily from a shared ``HttpClient``."""

    def __init__(
        self,
        http: HttpClient | None,
        boamp: Any,
        decp: Any,
        ted: Any,
        resolver: Any,
    ) -> None:
        self._http = http
        self._owns_http = False
        self._boamp, self._decp, self._ted, self._resolver = boamp, decp, ted, resolver

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(min_interval=_MIN_INTERVAL)
            self._owns_http = True
        return self._http

    @property
    def resolver(self) -> Any:
        if self._resolver is None:
            from marches_publics_mcp.sources.entreprises import CompanyResolver

            self._resolver = CompanyResolver(self.http)
        return self._resolver

    @property
    def boamp(self) -> Any:
        if self._boamp is None:
            from marches_publics_mcp.sources.boamp import BoampClient

            self._boamp = BoampClient(self.http)
        return self._boamp

    @property
    def decp(self) -> Any:
        if self._decp is None:
            from marches_publics_mcp.sources.decp import DecpClient

            self._decp = DecpClient(self.http, resolver=self.resolver)
        return self._decp

    @property
    def ted(self) -> Any:
        if self._ted is None:
            from marches_publics_mcp.sources.ted import TedClient

            self._ted = TedClient(self.http)
        return self._ted

    async def aclose(self) -> None:
        """Close the HTTP client only if this object created it."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()


async def _guard(source: str, call: Awaitable[T]) -> T:
    """Await ``call`` and translate anticipated failures into model-readable ``ToolError``."""
    try:
        return await call
    except ToolError:
        raise
    except ValueError as exc:
        raise ToolError(f"Invalid request: {exc}") from exc
    except NotFoundError as exc:
        raise ToolError(
            f"{source}: nothing found ({exc.message}). Check the identifier, or search first."
        ) from exc
    except UpstreamError as exc:
        raise ToolError(
            f"The {source} source is unavailable or rejected the request ({exc.message}). "
            "Retry in a minute, or narrow the filters."
        ) from exc
    except Exception:
        logger.exception("unexpected tool failure", extra={"source": source})
        raise


# --------------------------------------------------------------------------- server factory
def build_server(
    http: HttpClient | None = None,
    *,
    boamp: Any = None,
    decp: Any = None,
    ted: Any = None,
    resolver: Any = None,
) -> MCPServer:
    """Build the MCP server.

    Any client not injected is created lazily (on first use) from ``http``; if ``http`` is
    ``None`` a private ``HttpClient`` is created and closed when the server shuts down.

    Args:
        http: Shared HTTP client for the default source clients.
        boamp: Object with the ``BoampClient`` interface (tests inject fakes).
        decp: Object with the ``DecpClient`` interface.
        ted: Object with the ``TedClient`` interface.
        resolver: Object with the ``CompanyResolver`` interface.

    Returns:
        A configured ``MCPServer`` (tools, resources, prompt).
    """
    clients = _Clients(http, boamp, decp, ted, resolver)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await clients.aclose()

    server = MCPServer(
        "marches-publics",
        title="Marchés publics (BOAMP, DECP, TED)",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------ BOAMP
    @server.tool(annotations=_RO, title="Search BOAMP notices (France)")
    async def search_boamp_notices(
        keywords: Keywords = None,
        cpv: CpvBoamp = None,
        region: Annotated[
            str | None,
            Field(
                description="French region name (accents/case ignored); expanded to its "
                "departments. See resource marches://regions.",
                examples=["Occitanie", "ile-de-france"],
            ),
        ] = None,
        departments: Annotated[
            list[str] | None,
            Field(
                description="Department codes or exact names; combined with `region`.",
                examples=[["31", "Tarn"]],
                max_length=20,
            ),
        ] = None,
        published_from: DateFrom = None,
        published_to: DateTo = None,
        open_only: Annotated[
            bool, Field(description="Only notices whose response deadline is still ahead.")
        ] = False,
        notice_kind: Annotated[
            Literal["tender", "award", "any"],
            Field(description="`tender` = calls for tenders, `award` = award results."),
        ] = "any",
        market_type: Annotated[
            Literal["TRAVAUX", "SERVICES", "FOURNITURES"] | None,
            Field(description="Works, services or supplies."),
        ] = None,
        buyer: Annotated[
            str | None,
            Field(description="Part of the buyer name.", examples=["Ville de Toulouse"]),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 10,
    ) -> BoampSearchResponse:
        """Search French public procurement notices (BOAMP), newest first.

        Use for: open calls for tenders and published notices in France, filtered by topic,
        CPV code, region/department, buyer or date. Example: open IT-services tenders in
        Occitanie -> cpv="72", region="Occitanie", open_only=true, notice_kind="tender".
        Do NOT use for: contract amounts or winners over time or rankings (use
        search_decp_awards / rank_decp_parties), nor for notices outside France (use
        search_ted_notices). Returns a compact list; call get_boamp_notice with an `id` for
        the full notice.
        """
        codes = await _guard("boamp", _resolve(region, departments))
        result = await _guard(
            "boamp",
            clients.boamp.search(
                keywords=keywords,
                cpv=cpv,
                departments=codes,
                published_from=published_from,
                published_to=published_to,
                open_only=open_only,
                notice_kind=notice_kind,
                market_type=market_type,
                buyer=buyer,
                offset=offset,
                limit=limit,
            ),
        )
        hint = None
        if not result.notices:
            hint = (
                "No notice matched. Widen the date range, drop `cpv` or `buyer`, use fewer "
                "keywords, or set open_only=false."
            )
        return BoampSearchResponse(**result.model_dump(), departments_used=codes or [], hint=hint)

    @server.tool(annotations=_RO, title="Get one BOAMP notice")
    async def get_boamp_notice(
        notice_id: Annotated[
            str,
            Field(
                pattern=r"^\d{2}-\d+$",
                description="BOAMP `idweb` from search_boamp_notices (YY-NNNNNN).",
                examples=["26-112058"],
            ),
        ],
    ) -> BoampNoticeDetail:
        """Get the compact detail of one BOAMP notice: description, CPV, lots, value, winners.

        Use after search_boamp_notices when the user wants more on a specific notice. Do NOT
        guess ids, and do not use for TED notices (get_ted_notice).
        """
        return await _guard("boamp", clients.boamp.get_notice(notice_id))

    # ------------------------------------------------------------------ DECP
    @server.tool(annotations=_RO, title="Search awarded contracts (DECP, France)")
    async def search_decp_awards(
        keywords: Keywords = None,
        cpv_prefix: CpvPrefix = None,
        buyer_siret: Annotated[Siret, Field(description="14-digit SIRET of the buyer.")] = None,
        supplier_siret: Annotated[
            Siret, Field(description="14-digit SIRET of the supplier.")
        ] = None,
        department: DepartmentCode = None,
        notified_from: DateFrom = None,
        notified_to: DateTo = None,
        min_amount: Annotated[float | None, Field(ge=0, description="Minimum amount, EUR.")] = None,
        max_amount: Annotated[float | None, Field(ge=0, description="Maximum amount, EUR.")] = None,
        procedure: Annotated[
            str | None, Field(description="Procedure label.", examples=["Procédure adaptée"])
        ] = None,
        order_by: Annotated[
            Literal["date", "amount"], Field(description="Sort by notification date or amount.")
        ] = "date",
        offset: Offset = 0,
        limit: Limit = 10,
    ) -> DecpSearchResponse:
        """List awarded French public contracts (DECP) with amount, buyer and supplier.

        Use for: "which contracts were awarded for X", biggest contracts (order_by="amount"),
        what a given buyer or supplier signed. Names are resolved from SIRET automatically.
        Do NOT use for open tenders (search_boamp_notices), for "top N buyers/suppliers"
        (rank_decp_parties does the aggregation upstream), or for EU data (search_ted_notices).
        Amounts are as notified; framework agreements show their maximum.
        """
        result = await _guard(
            "decp",
            clients.decp.search(
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
                order_by=order_by,
                offset=offset,
                limit=limit,
            ),
        )
        hint = None
        if not result.awards:
            hint = (
                "No award matched. Widen the notification dates, use a shorter `cpv_prefix` "
                "(e.g. 2 digits), drop the amount bounds or `department`."
            )
        return DecpSearchResponse(**result.model_dump(), hint=hint)

    @server.tool(annotations=_RO, title="Rank top buyers or suppliers (DECP)")
    async def rank_decp_parties(
        by: Annotated[
            Literal["buyer", "supplier"],
            Field(description="`buyer` = who purchases most, `supplier` = who wins most."),
        ],
        order: Annotated[
            Literal["count", "amount"],
            Field(description="Rank by number of contracts or by total amount (EUR)."),
        ] = "count",
        cpv_prefix: CpvPrefix = None,
        keywords: Keywords = None,
        department: DepartmentCode = None,
        buyer_siret: Annotated[
            Siret, Field(description="Restrict to one buyer (14-digit SIRET).")
        ] = None,
        notified_from: DateFrom = None,
        notified_to: DateTo = None,
        top: Annotated[int, Field(ge=1, le=50, description="Number of ranks (1-50).")] = 10,
    ) -> DecpRankingResponse:
        """Rank the top buyers or suppliers of French awarded contracts (DECP).

        Use for: "who are the main suppliers of IT services in Occitanie in 2025", "which
        buyers spend most on cleaning". Aggregated upstream over all matching contracts, not
        only a page. Example: by="supplier", order="amount", cpv_prefix="72", department="31".
        Do NOT use to list individual contracts (search_decp_awards). Read `caveats`: DECP
        coverage is incomplete and amounts are notified values, not payments.
        """
        result = await _guard(
            "decp",
            clients.decp.rank(
                by=by,
                order=order,
                cpv_prefix=cpv_prefix,
                keywords=keywords,
                department=department,
                buyer_siret=buyer_siret,
                notified_from=notified_from,
                notified_to=notified_to,
                top=top,
            ),
        )
        hint = None
        if not result.entries:
            hint = "No contract matched. Widen the dates, shorten `cpv_prefix` or drop filters."
        return DecpRankingResponse(**result.model_dump(), hint=hint)

    # ------------------------------------------------------------------ TED
    @server.tool(annotations=_RO, title="Search TED notices (EU)")
    async def search_ted_notices(
        keywords: Annotated[
            str | None,
            Field(
                description="Words searched in the notice (any language; English is safest).",
                examples=["cloud hosting"],
                max_length=200,
            ),
        ] = None,
        cpv: CpvExact = None,
        countries: Annotated[
            list[str] | None,
            Field(
                description="ISO 3166 alpha-3 codes of the place of performance.",
                examples=[["FRA", "DEU"]],
                max_length=30,
            ),
        ] = None,
        published_from: DateFrom = None,
        published_to: DateTo = None,
        open_only: Annotated[
            bool, Field(description="Only notices still open for tenders.")
        ] = False,
        notice_type: Annotated[
            str | None,
            Field(description="TED notice type code.", examples=["cn-standard", "can-standard"]),
        ] = None,
        page: Annotated[int, Field(ge=1, le=100, description="1-based page number.")] = 1,
        limit: Limit = 10,
    ) -> TedSearchResponse:
        """Search EU-wide public procurement notices on TED (Tenders Electronic Daily).

        Use for: tenders in other EU countries or cross-border, or above-threshold French
        contracts seen from the EU side. Example: cpv="72000000", countries=["DEU","AUT"],
        open_only=true. Do NOT use for France-only searches by department/region (use
        search_boamp_notices) or for award amounts/rankings in France (DECP tools). Pagination
        is by `page`: pass the result's `next_page` as `page` (null = no more pages); `next_offset`
        is informational only. Call get_ted_notice with a `publication_number`.
        """
        bad = [c for c in countries or [] if len(c) != 3 or not c.isalpha()]
        if bad:
            raise ToolError(
                f"Invalid country code(s) {bad}: use ISO alpha-3 such as 'FRA', 'DEU', 'ESP'."
            )
        result = await _guard(
            "ted",
            clients.ted.search(
                keywords=keywords,
                cpv=cpv,
                countries=[c.upper() for c in countries] if countries else None,
                published_from=published_from,
                published_to=published_to,
                open_only=open_only,
                notice_type=notice_type,
                page=page,
                limit=limit,
            ),
        )
        hint = None
        if not result.notices:
            hint = (
                "No notice matched. Widen the dates, drop `cpv` or `notice_type`, try English "
                "keywords, or set open_only=false."
            )
        return TedSearchResponse(**result.model_dump(), hint=hint)

    @server.tool(annotations=_RO, title="Get one TED notice")
    async def get_ted_notice(
        publication_number: Annotated[
            str,
            Field(
                pattern=r"^\d{1,8}-\d{4}$",
                description="TED publication number from search_ted_notices (NNNNNN-YYYY).",
                examples=["757212-2026"],
            ),
        ],
    ) -> TedNoticeDetail:
        """Get the compact detail of one TED notice: description, value, procedure, winners.

        Use after search_ted_notices. Do NOT use for BOAMP ids (get_boamp_notice).
        """
        return await _guard("ted", clients.ted.get_notice(publication_number))

    # ------------------------------------------------------------------ companies
    @server.tool(annotations=_RO, title="Look up a company name by SIRET/SIREN")
    async def lookup_company(
        identifier: Annotated[
            str,
            Field(
                pattern=r"^(\d{9}|\d{14})$",
                description="SIREN (9 digits) or SIRET (14 digits).",
                examples=["35600000000048"],
            ),
        ],
    ) -> CompanyInfo:
        """Resolve a French SIRET or SIREN to the company name (public company directory).

        Use when a DECP/BOAMP result shows only an identifier. Do NOT use to search by name
        or to get financial data. DECP tools already resolve names; this is for ad-hoc checks.
        """
        try:
            name = await clients.resolver.lookup(identifier)
        except UpstreamError as exc:
            raise ToolError("recherche-entreprises unavailable, retry later") from exc
        hint = None if name else "Unknown identifier: check the digits or try the 9-digit SIREN."
        return CompanyInfo(identifier=identifier, name=name, hint=hint)

    # ------------------------------------------------------------------ CPV nomenclature
    @server.tool(annotations=_RO, title="Find CPV codes from words")
    async def search_cpv_codes(
        text: Annotated[
            str,
            Field(
                min_length=2,
                max_length=100,
                description="Words to look up in CPV labels (prefix match, accents ignored).",
                examples=["nettoyage", "maintenance logiciel", "road works"],
            ),
        ],
        lang: Annotated[Literal["fr", "en"], Field(description="Label language of `text`.")] = "fr",
        limit: Annotated[int, Field(ge=1, le=50, description="Max codes returned.")] = 10,
    ) -> CpvSearchResult:
        """Find CPV 2008 codes whose label matches the words (official EU nomenclature, offline).

        Use before a search when the user names an activity instead of a code. Shorter codes
        (e.g. 90910000) are broader; pass their significant digits as a prefix ("9091") to the
        search tools. Do NOT use to find notices: use the search_* tools with the code found.
        """
        try:
            found = cpv_codes.search(text, lang=lang, limit=limit)
        except ValueError as exc:
            raise ToolError(f"Invalid request: {exc}") from exc
        hint = None if found else "No CPV label matched. Try a shorter word or the other language."
        return CpvSearchResult(
            matches=[CpvMatch(code=c, label=label) for c, label in found], hint=hint
        )

    # ------------------------------------------------------------------ resources and prompt
    @server.resource(
        "marches://regions",
        name="regions",
        description="French regions mapped to their department codes (and names).",
        mime_type="application/json",
    )
    def regions_resource() -> str:
        payload = {
            region: {code: geo.DEPARTMENTS[code] for code in codes}
            for region, codes in geo.REGIONS.items()
        }
        return json.dumps(payload, ensure_ascii=False)

    @server.resource(
        "marches://sources",
        name="sources",
        description="Data sources, licences and freshness caveats.",
        mime_type="text/markdown",
    )
    def sources_resource() -> str:
        return SOURCES_MARKDOWN

    @server.prompt(
        title="Veille marchés",
        description="Monitoring workflow: open tenders for a CPV code in a region, then context.",
    )
    def veille_marches(cpv: str, region: str) -> str:
        if not re.fullmatch(r"\d{2,8}", cpv.strip()):
            raise MCPError(
                INVALID_PARAMS, f"`cpv` must be 2 to 8 digits, e.g. 72000000 (got {cpv!r})."
            )
        try:
            geo.region_departments(region)
        except ValueError as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from exc
        cpv, region = cpv.strip(), region.strip()
        return (
            f"Run a procurement watch for CPV {cpv} in the region {region}.\n"
            f"1. search_boamp_notices(cpv='{cpv}', region='{region}', open_only=true, "
            "notice_kind='tender'); list id, buyer, deadline, link.\n"
            f"2. For the 3 most promising, call get_boamp_notice and summarise object, value, "
            "lots, deadline.\n"
            f"3. rank_decp_parties(by='buyer', cpv_prefix='{cpv[:4]}') for the region's "
            "departments to show the main buyers, then by='supplier' for the competitors.\n"
            "End with a short table: notice, buyer, deadline, why it matters."
        )

    return server


SOURCES_MARKDOWN = """\
# Sources

| Source | Content | Licence | Freshness |
|---|---|---|---|
| BOAMP (DILA, opendatasoft) | French notices: tenders, awards | Licence Ouverte v2.0 | daily; \
no CPV field upstream (matched inside the notice payload) |
| DECP (data.economie.gouv.fr, decp-2022-marches-valides) | French awarded contracts, \
essential data | Licence Ouverte v2.0 | weeks of lag; no buyer/supplier names, only SIRET; \
incomplete coverage; amounts are notified values |
| recherche-entreprises (DINUM) | SIRET/SIREN -> company name | Licence Ouverte v2.0 | live |
| TED (EU Publications Office) | EU-wide notices | CC BY 4.0 | daily |
"""


async def _resolve(region: str | None, departments: list[str] | None) -> list[str] | None:
    """Async wrapper so geo ``ValueError`` goes through ``_guard``."""
    return geo.resolve_departments(region, departments)


def main(argv: list[str] | None = None) -> None:
    """Run the server (stdio by default). Nothing but the protocol goes to stdout."""
    parser = argparse.ArgumentParser(prog="marches-publics-mcp", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (streamable-http).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (streamable-http).")
    args = parser.parse_args(argv)

    setup_logging()
    server = build_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
