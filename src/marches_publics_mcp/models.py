"""Compact, LLM-oriented output models shared by the sources and the MCP tools.

Design rules: short field names, no raw upstream blobs, long texts truncated (see
``truncate``), every list result carries pagination info so the model can ask for more.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

TITLE_MAX = 200
TEXT_MAX = 1500


def truncate(text: str | None, limit: int = TITLE_MAX) -> str | None:
    """Collapse whitespace and cut ``text`` to ``limit`` characters (adds an ellipsis)."""
    if text is None:
        return None
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


class PageInfo(BaseModel):
    """Pagination block present in every list result."""

    total: int = Field(description="Total matching records upstream.")
    offset: int
    returned: int
    next_offset: int | None = Field(
        default=None, description="Pass as `offset` to get the next page; null when done."
    )


# ---------------------------------------------------------------- BOAMP (France, notices)
class BoampNotice(BaseModel):
    """One BOAMP notice in a result list."""

    id: str = Field(description="BOAMP `idweb`, e.g. 26-112058. Use with get_boamp_notice.")
    title: str | None
    buyer: str | None
    published: date | None
    deadline: str | None = Field(default=None, description="Tender deadline (ISO datetime).")
    departments: list[str] = []
    notice_type: str | None = Field(default=None, description="e.g. Avis de marché, Résultat.")
    procedure: str | None = None
    market_types: list[str] = Field(default=[], description="TRAVAUX / SERVICES / FOURNITURES.")
    url: str | None = None


class BoampSearchResult(BaseModel):
    """Paginated BOAMP search result."""

    page: PageInfo
    notices: list[BoampNotice]
    query: str | None = Field(default=None, description="ODSQL `where` actually sent (debug).")


class Lot(BaseModel):
    """One lot of a notice."""

    number: str | None = None
    title: str | None = None
    cpv: list[str] = []
    estimated_value: float | None = None


class BoampNoticeDetail(BoampNotice):
    """Full but compact view of one BOAMP notice."""

    description: str | None = Field(default=None, description=f"Truncated to {TEXT_MAX} chars.")
    cpv: list[str] = []
    estimated_value: float | None = None
    currency: str | None = None
    lots: list[Lot] = []
    award_criteria: str | None = None
    winners: list[str] = Field(default=[], description="Awardee names (award notices only).")
    related_notices: list[str] = []
    eforms: bool = Field(default=False, description="True when parsed from an eForms payload.")


# ---------------------------------------------------------------- DECP (France, awards)
class DecpAward(BaseModel):
    """One awarded contract from the DECP (essential procurement data)."""

    id: str
    title: str | None
    buyer_siret: str | None
    buyer_name: str | None = None
    supplier_sirets: list[str] = []
    supplier_names: list[str | None] = []
    amount_eur: float | None = Field(
        default=None, description="Notified amount (for framework agreements: the maximum)."
    )
    notified: date | None = None
    cpv: str | None = None
    procedure: str | None = None
    place_code: str | None = Field(default=None, description="Postal code or department code.")
    duration_months: int | None = None
    offers_received: int | None = None


class DecpSearchResult(BaseModel):
    """Paginated DECP search result."""

    page: PageInfo
    awards: list[DecpAward]
    query: str | None = None


class RankEntry(BaseModel):
    """One row of a buyer/supplier ranking."""

    rank: int
    siret: str
    name: str | None = None
    contracts: int
    total_amount_eur: float | None = None


class DecpRanking(BaseModel):
    """Top buyers or suppliers for a filter set (aggregated upstream)."""

    by: str = Field(description="`buyer` or `supplier`.")
    order: str = Field(description="`count` or `amount`.")
    filters: dict[str, str] = {}
    total_contracts: int = Field(description="Contracts matching the filters (all ranks).")
    entries: list[RankEntry]
    caveats: list[str] = []


# ---------------------------------------------------------------- TED (EU)
class TedNotice(BaseModel):
    """One TED notice in a result list."""

    publication_number: str = Field(description="e.g. 757212-2026. Use with get_ted_notice.")
    title: str | None = None
    buyer: str | None = None
    buyer_country: str | None = None
    published: str | None = None
    deadline: str | None = None
    notice_type: str | None = None
    cpv: list[str] = []
    places: list[str] = Field(default=[], description="NUTS codes / countries of performance.")
    url: str | None = None


class TedSearchResult(BaseModel):
    """Paginated TED search result."""

    page: PageInfo
    notices: list[TedNotice]
    query: str | None = Field(default=None, description="TED expert query actually sent.")


class TedNoticeDetail(TedNotice):
    """Compact detail of one TED notice."""

    description: str | None = None
    estimated_value: float | None = None
    currency: str | None = None
    procedure: str | None = None
    winners: list[str] = []
    award_value: float | None = None
    pdf_url: str | None = None
