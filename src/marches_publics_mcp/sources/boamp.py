"""BOAMP client (French public procurement notices) on the Opendatasoft Explore API v2.1.

Facts verified live against the upstream dataset (2026-11-20), which drive the design:

* ``nature`` values: ``APPEL_OFFRE`` (tenders), ``ATTRIBUTION`` (awards), plus ``RECTIFICATIF``,
  ``ANNULATION``, ``MODIFICATION``, ``PRE-INFORMATION``, ``PERIODIQUE``, ``EX_ANTE_VOLONTAIRE``.
  ``famille`` is the legal regime / payload schema (``JOUE`` = EU-level notices, mostly eForms
  since 2024; ``FNS`` = national simplified form; ``MAPA`` = adapted procedure; ``DSP``;
  ``DIVERS``) and is not used for filtering: ``nature`` carries the tender/award distinction.
* There is no CPV field. The code only lives inside the ``donnees`` JSON text, so the CPV filter
  is ``search(donnees, "<8-digit code>")``: an exact token match (a full 8-digit code, with or
  without its ``-<check digit>`` suffix). Wildcards are NOT usable: ``search(donnees, "72*")``
  matches any token starting with 72 (dates, SIRET, ...) and returned ~200k noise rows.
  There is no CPV hierarchy either: ``72000000`` does not match ``72212000``. A 2-7 digit prefix
  is therefore expanded with the official nomenclature (``cpv.expand``) into
  ``((search(donnees, "c1") OR ...) OR (...))``. Two upstream limits (measured live 2026-11-20)
  shape that: ODSQL rejects a *flat* clause of ~250+ expressions (HTTP 400 "too many
  expressions", counted together with the other filters), but accepts the same terms grouped in
  parenthesised chunks of 50; and the GET URL is rejected (414) around 20-30 KB, i.e. ~600+
  codes. Hence ``CPV_MAX_CODES = 300`` (~12 KB). Division ``72`` (252 codes) works; ``45`` (822)
  must be narrowed. Batching into several requests is not an option (counts would be wrong).
* ``search(objet, "a b")`` is a token AND over the title; it is what ``keywords`` uses.
* ``code_departement`` is multivalued and stored without zero padding for 1-9 (``"4"`` not
  ``"04"``); ``IN (...)`` matches if any value is in the list. Overseas codes are 3 digits.
* ``datelimitereponse >= now()`` works for "still open"; award notices have no deadline and are
  therefore excluded by ``open_only``.
* ``limit`` is capped at 100 and ``offset + limit`` at 10000 (upstream returns HTTP 400 beyond).
* An unknown ``idweb`` is not a 404: ODS answers ``total_count: 0``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from datetime import date
from typing import Any, Literal

from marches_publics_mcp import cpv as cpv_nomenclature
from marches_publics_mcp.http import HttpClient, NotFoundError
from marches_publics_mcp.models import (
    TEXT_MAX,
    BoampNotice,
    BoampNoticeDetail,
    BoampSearchResult,
    Lot,
    PageInfo,
    truncate,
)
from marches_publics_mcp.sources.ods import and_all, date_literal, in_list, quote

logger = logging.getLogger(__name__)

SOURCE = "boamp"
DEFAULT_BASE_URL = (
    "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records"
)
BASE_URL_ENV = "MCP_MP_BOAMP_URL"
MAX_WINDOW = 10_000
MAX_LIMIT = 100
NATURE_BY_KIND = {"tender": "APPEL_OFFRE", "award": "ATTRIBUTION"}
LIST_FIELDS = (
    "idweb,objet,nomacheteur,dateparution,datelimitereponse,code_departement,"
    "nature,nature_libelle,procedure_libelle,type_marche,url_avis"
)
CRITERIA_MAX = 500
CPV_MAX_CODES = 300
CPV_CHUNK = 50
_CPV_RE = re.compile(r"^\s*(\d{8})(?:-\d)?\s*$")


# ---------------------------------------------------------------- generic payload helpers
def _as_list(value: Any) -> list[Any]:
    """Return ``value`` as a list (``None`` → ``[]``, scalar/dict → one-element list)."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(node: Any) -> str | None:
    """Extract a string from an XML-as-JSON node (scalar, ``{"#text": ...}`` or first of a list)."""
    if isinstance(node, list):
        for item in node:
            found = _text(item)
            if found:
                return found
        return None
    if isinstance(node, dict):
        return _text(node.get("#text"))
    if node is None or isinstance(node, bool):
        return None
    clean = " ".join(str(node).split())
    return clean or None


def _dig(node: Any, *path: str) -> Any:
    """Follow ``path`` through nested dicts (taking the first element of lists); else ``None``."""
    for key in path:
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _find_all(node: Any, key: str) -> Iterator[Any]:
    """Yield every value stored under ``key`` at any depth."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _find_all(v, key)
    elif isinstance(node, list):
        for item in node:
            yield from _find_all(item, key)


def _first_text(node: Any, *keys: str) -> str | None:
    """First non-empty text found for ``keys`` (priority order), searching any depth."""
    for key in keys:
        for value in _find_all(node, key):
            found = _text(value)
            if found:
                return found
    return None


def _leaves(node: Any) -> Iterator[str]:
    """Yield every string leaf below ``node``."""
    if isinstance(node, dict):
        for v in node.values():
            yield from _leaves(v)
    elif isinstance(node, list):
        for v in node:
            yield from _leaves(v)
    elif isinstance(node, str):
        yield node
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield str(node)


def _number(value: Any) -> float | None:
    """Parse a number from a scalar or XML-as-JSON node (comma decimals accepted)."""
    raw = _text(value)
    if raw is None:
        return None
    try:
        return float(raw.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _amount(node: Any) -> tuple[float | None, str | None]:
    """Read ``(value, currency)`` from the various amount encodings seen upstream."""
    if isinstance(node, list):
        node = node[0] if node else None
    if isinstance(node, dict):
        raw = node.get("#text", node.get("valeur"))
        currency = next(
            (_text(node[k]) for k in ("@currencyID", "@DEVISE", "@devise") if k in node), None
        )
        return _number(raw), currency
    return _number(node), None


def _dedupe(items: list[str]) -> list[str]:
    """Remove duplicates, keeping the first occurrence order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------- donnees parsing
def _parse_donnees(raw: Any) -> dict[str, Any]:
    """Decode the ``donnees`` field (JSON text) into a dict; ``{}`` when absent or invalid."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("unparsable donnees", extra={"source": SOURCE})
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_cpv(node: Any) -> list[str]:
    """Collect 8-digit CPV codes below any ``codeCPV`` / ``CPV`` key of a legacy payload."""
    codes: list[str] = []
    for key in ("codeCPV", "CPV"):
        for sub in _find_all(node, key):
            for leaf in _leaves(sub):
                match = _CPV_RE.match(leaf)
                if match:
                    codes.append(match.group(1))
    return _dedupe(codes)


def _legacy_value(node: Any) -> tuple[float | None, str | None]:
    """Best amount of a legacy payload: estimate first, then the awarded amount."""
    for key in ("valeurEstimee", "VALEUR", "ESTIMATION_INITIALE", "montant", "MONTANT"):
        for sub in _find_all(node, key):
            value, currency = _amount(sub)
            if value is not None:
                return value, currency
    return None, None


def _legacy_criteria(node: Any) -> str | None:
    """Short award-criteria text from the legacy schemas (free text or weighted lists)."""
    parts: list[str] = []
    for sub in _find_all(node, "criteresAttrib"):
        if (text := _text(sub)) is not None:
            parts.append(text)
    for sub in _find_all(node, "criterePondere"):
        for item in _as_list(sub):
            name = _text(_dig(item, "critere"))
            weight = _text(_dig(item, "criterePCT"))
            if name:
                parts.append(f"{name} {weight}%" if weight else name)
    for sub in _find_all(node, "CRITERE"):
        for item in _as_list(sub):
            name = _text(item)
            weight = _text(item.get("@POIDS")) if isinstance(item, dict) else None
            if name:
                parts.append(f"{name} {weight}%" if weight else name)
    return truncate("; ".join(_dedupe(parts)), CRITERIA_MAX) if parts else None


def _legacy_lots(node: Any) -> list[Lot]:
    """Lots of a legacy payload (``LOTS/LOT`` or ``lot`` lists); empty when single-lot."""
    lots: list[Lot] = []
    for key in ("LOT", "lot"):
        for sub in _find_all(node, key):
            for item in _as_list(sub):
                if not isinstance(item, dict):
                    continue
                value, _ = _legacy_value(item)
                lots.append(
                    Lot(
                        number=_first_text(item, "NUM", "numero", "num", "@num"),
                        title=truncate(
                            _first_text(item, "INTITULE", "intitule", "designation", "DESIGNATION")
                        ),
                        cpv=_legacy_cpv(item),
                        estimated_value=value,
                    )
                )
    return lots


def _legacy_winners(node: Any) -> list[str]:
    """Awardee company names from legacy payloads (never person-name fields)."""
    names: list[str] = []
    for key in ("titulaire", "TITULAIRE"):
        for sub in _find_all(node, key):
            for item in _as_list(sub):
                name = _text(item) if not isinstance(item, dict) else None
                if isinstance(item, dict):
                    name = _first_text(item, "DENOMINATION", "PersonneMorale", "denomination")
                if name:
                    names.append(name)
    return _dedupe(names)


def _parse_legacy(data: dict[str, Any]) -> dict[str, Any]:
    """Parse FNSimple / MAPA / JOUE-legacy / other pre-eForms payloads."""
    value, currency = _legacy_value(data)
    description = _first_text(
        data, "OBJET_COMPLET", "description", "objet", "TITRE_MARCHE", "intitule"
    )
    return {
        "description": truncate(description, TEXT_MAX),
        "cpv": _legacy_cpv(data),
        "estimated_value": value,
        "currency": currency,
        "lots": _legacy_lots(data),
        "award_criteria": _legacy_criteria(data),
        "winners": _legacy_winners(data),
        "eforms": False,
    }


def _eforms_extension(notice: dict[str, Any]) -> dict[str, Any]:
    """Return the ``efext:EformsExtension`` block of an eForms notice (``{}`` if absent)."""
    for ext in _as_list(_dig(notice, "ext:UBLExtensions", "ext:UBLExtension")):
        content = _dig(ext, "ext:ExtensionContent", "efext:EformsExtension")
        if isinstance(content, dict):
            return content
    return {}


def _eforms_cpv(project: Any) -> list[str]:
    """CPV codes of an eForms ``cac:ProcurementProject`` (main + additional)."""
    codes: list[str] = []
    if not isinstance(project, dict):
        return codes
    for key in ("cac:MainCommodityClassification", "cac:AdditionalCommodityClassification"):
        for item in _as_list(project.get(key)):
            code = _text(_dig(item, "cbc:ItemClassificationCode"))
            if code and _CPV_RE.match(code):
                codes.append(code.strip()[:8])
    return codes


def _eforms_estimate(project: Any) -> tuple[float | None, str | None]:
    """Estimated value of an eForms ``cac:ProcurementProject``."""
    return _amount(_dig(project, "cac:RequestedTenderTotal", "cbc:EstimatedOverallContractAmount"))


def _eforms_criteria(notice: dict[str, Any]) -> str | None:
    """Short award-criteria text gathered over all lots of an eForms notice."""
    parts: list[str] = []
    for lot in _as_list(notice.get("cac:ProcurementProjectLot")):
        for crit in _as_list(
            _dig(lot, "cac:TenderingTerms", "cac:AwardingTerms", "cac:AwardingCriterion")
        ):
            for sub in _as_list(_dig(crit, "cac:SubordinateAwardingCriterion")):
                text = _text(_dig(sub, "cbc:Description")) or _text(
                    _dig(sub, "cbc:AwardingCriterionTypeCode")
                )
                if text:
                    parts.append(text)
            expr = _text(_dig(crit, "cbc:CalculationExpression"))
            if expr:
                parts.append(expr)
    return truncate("; ".join(_dedupe(parts)), CRITERIA_MAX) if parts else None


def _eforms_winners(notice: dict[str, Any]) -> list[str]:
    """Winner company names resolved from ``NoticeResult`` → tenderers → organisations."""
    ext = _eforms_extension(notice)
    orgs: dict[str, str] = {}
    for org in _as_list(_dig(ext, "efac:Organizations", "efac:Organization")):
        company = _dig(org, "efac:Company")
        org_id = _text(_dig(company, "cac:PartyIdentification", "cbc:ID"))
        name = _text(_dig(company, "cac:PartyName", "cbc:Name"))
        if org_id and name:
            orgs[org_id] = name
    result = _dig(ext, "efac:NoticeResult")
    parties: dict[str, list[str]] = {}
    for party in _as_list(_dig(result, "efac:TenderingParty")):
        party_id = _text(_dig(party, "cbc:ID"))
        if party_id:
            parties[party_id] = [
                _text(t.get("cbc:ID")) or "" for t in _as_list(party.get("efac:Tenderer"))
            ]
    winners: list[str] = []
    for lot_result in _as_list(_dig(result, "efac:LotResult")):
        if _text(_dig(lot_result, "cbc:TenderResultCode")) != "selec-w":
            continue
        tender_ids = {_text(t.get("cbc:ID")) for t in _as_list(lot_result.get("efac:LotTender"))}
        for tender in _as_list(_dig(result, "efac:LotTender")):
            if _text(_dig(tender, "cbc:ID")) not in tender_ids:
                continue
            party_id = _text(_dig(tender, "efac:TenderingParty", "cbc:ID"))
            winners.extend(orgs.get(org_id, "") for org_id in parties.get(party_id or "", []))
    return _dedupe(winners)


def _parse_eforms(eforms: dict[str, Any]) -> dict[str, Any]:
    """Parse an eForms (UBL) payload (contract, award or prior-information notice)."""
    notice = next((v for v in eforms.values() if isinstance(v, dict)), {})
    project = notice.get("cac:ProcurementProject")
    description = _text(_dig(project, "cbc:Description")) or _text(_dig(project, "cbc:Name"))
    value, currency = _eforms_estimate(project)
    if value is None:
        value, currency = _amount(
            _dig(_eforms_extension(notice), "efac:NoticeResult", "cbc:TotalAmount")
        )
    cpv = _eforms_cpv(project)
    lots: list[Lot] = []
    lot_amounts: list[tuple[float, str | None]] = []
    for lot in _as_list(notice.get("cac:ProcurementProjectLot")):
        if not isinstance(lot, dict):
            continue
        lot_project = lot.get("cac:ProcurementProject")
        lot_cpv = _eforms_cpv(lot_project)
        cpv.extend(lot_cpv)
        lot_value, lot_currency = _eforms_estimate(lot_project)
        if lot_value is not None:
            lot_amounts.append((lot_value, lot_currency))
        lots.append(
            Lot(
                number=_text(_dig(lot, "cbc:ID")),
                title=truncate(_text(_dig(lot_project, "cbc:Name"))),
                cpv=_dedupe(lot_cpv),
                estimated_value=lot_value,
            )
        )
    if value is None and lot_amounts:  # no global estimate: sum the lot estimates
        value = sum(v for v, _ in lot_amounts)
        currency = next((c for _, c in lot_amounts if c), None)
    return {
        "description": truncate(description, TEXT_MAX),
        "cpv": _dedupe(cpv),
        "estimated_value": value,
        "currency": currency,
        "lots": lots,
        "award_criteria": _eforms_criteria(notice),
        "winners": _eforms_winners(notice),
        "eforms": True,
    }


def parse_donnees(raw: Any) -> dict[str, Any]:
    """Extract compact fields from a BOAMP ``donnees`` payload (eForms or legacy schemas).

    Never raises: missing or malformed data yields ``None`` / empty lists.

    Args:
        raw: The ``donnees`` value (JSON text, already-decoded dict, or ``None``).

    Returns:
        A dict with keys ``description``, ``cpv``, ``estimated_value``, ``currency``, ``lots``,
        ``award_criteria``, ``winners`` and ``eforms``.
    """
    data = _parse_donnees(raw)
    try:
        eforms = data.get("EFORMS")
        if isinstance(eforms, dict):
            return _parse_eforms(eforms)
        return _parse_legacy(data)
    except (AttributeError, TypeError, KeyError, IndexError):  # defensive: odd upstream shapes
        logger.warning("donnees parser fell back to empty result", extra={"source": SOURCE})
        return {
            "description": None,
            "cpv": [],
            "estimated_value": None,
            "currency": None,
            "lots": [],
            "award_criteria": None,
            "winners": [],
            "eforms": isinstance(data.get("EFORMS"), dict),
        }


# ---------------------------------------------------------------- client
_CPV_PREFIX_RE = re.compile(r"^\s*(\d{2,7})\*?\s*$")


def _cpv_clause(cpv: str) -> str:
    """Build the ``where`` clause for an exact 8-digit CPV code or a 2-7 digit prefix.

    Raises:
        ValueError: If ``cpv`` is neither, or the prefix covers too many codes.
    """
    exact = _CPV_RE.match(cpv)
    if exact:
        return f"search(donnees, {quote(exact.group(1))})"
    prefix = _CPV_PREFIX_RE.match(cpv)
    if not prefix:
        raise ValueError(
            f"cpv {cpv!r} is not a CPV code: pass an exact 8-digit code ('72000000' or "
            "'72000000-5') or a 2-7 digit prefix ('72', '7221') covering all sub-codes. "
            "Use search_cpv_codes to find a code from words."
        )
    codes = cpv_nomenclature.expand(prefix.group(1), max_codes=CPV_MAX_CODES)
    terms = [f"search(donnees, {quote(c)})" for c in codes]
    chunks = [
        "(" + " OR ".join(terms[i : i + CPV_CHUNK]) + ")" for i in range(0, len(terms), CPV_CHUNK)
    ]
    return " OR ".join(chunks)


def _normalise_department(dep: str) -> list[str]:
    """Return the spellings under which a department may be stored (``'04'`` → ``['4', '04']``)."""
    clean = dep.strip().upper()
    if not re.fullmatch(r"\d{1,3}|2[AB]", clean):
        raise ValueError(
            f"department {dep!r} is invalid: use a code like '31', '2A' or '971' (no names)."
        )
    stripped = clean.lstrip("0") or "0"
    return _dedupe([stripped, clean])


def _to_notice(record: dict[str, Any]) -> BoampNotice:
    """Map one upstream record to ``BoampNotice``."""
    published = record.get("dateparution")
    try:
        published_date = date.fromisoformat(published[:10]) if published else None
    except ValueError:
        published_date = None
    return BoampNotice(
        id=str(record.get("idweb", "")),
        title=truncate(record.get("objet")),
        buyer=truncate(record.get("nomacheteur")),
        published=published_date,
        deadline=record.get("datelimitereponse"),
        departments=[str(d) for d in _as_list(record.get("code_departement"))],
        notice_type=record.get("nature_libelle"),
        procedure=record.get("procedure_libelle"),
        market_types=[str(t) for t in _as_list(record.get("type_marche"))],
        url=record.get("url_avis"),
    )


class BoampClient:
    """Search and read BOAMP notices (French public procurement bulletin)."""

    def __init__(self, http: HttpClient, base_url: str | None = None) -> None:
        """Create the client.

        Args:
            http: Shared HTTP client (retries, cache, pacing live there).
            base_url: Records endpoint; defaults to ``$MCP_MP_BOAMP_URL`` then the public API.
        """
        self._http = http
        self._base_url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL

    async def search(
        self,
        *,
        keywords: str | None = None,
        cpv: str | None = None,
        departments: list[str] | None = None,
        published_from: date | None = None,
        published_to: date | None = None,
        open_only: bool = False,
        notice_kind: Literal["tender", "award", "any"] = "any",
        market_type: Literal["TRAVAUX", "SERVICES", "FOURNITURES"] | None = None,
        buyer: str | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> BoampSearchResult:
        """Search notices, newest first.

        Args:
            keywords: Words that must all appear in the notice title (``objet``).
            cpv: Exact 8-digit CPV code (``-<digit>`` suffix tolerated) matched as a token in
                the notice body, or a 2-7 digit prefix (``"72"``, ``"7221"``) expanded to every
                official sub-code and OR-ed (ValueError beyond 300 codes, e.g. '45').
            departments: Department codes (``"31"``, ``"2A"``, ``"971"``); any match qualifies.
            published_from: Earliest publication date (inclusive).
            published_to: Latest publication date (inclusive).
            open_only: Keep only notices whose response deadline is in the future.
            notice_kind: ``tender`` (APPEL_OFFRE), ``award`` (ATTRIBUTION) or ``any``.
            market_type: ``TRAVAUX``, ``SERVICES`` or ``FOURNITURES``.
            buyer: Words that must appear in the buyer name.
            offset: Records to skip (``offset + limit`` must stay ≤ 10000).
            limit: Page size, 1 to 100.

        Returns:
            One page of notices with pagination info and the ODSQL ``where`` actually sent.

        Raises:
            ValueError: On invalid input, with a message explaining how to fix the call.
            UpstreamError: When BOAMP fails.
        """
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}.")
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}.")
        if offset + limit > MAX_WINDOW:
            raise ValueError(
                f"offset + limit = {offset + limit} exceeds BOAMP's window of {MAX_WINDOW} "
                "results. Narrow the search (dates, department, CPV, keywords) instead of "
                "paging deeper."
            )
        if published_from and published_to and published_from > published_to:
            raise ValueError("published_from must be on or before published_to.")
        clauses: list[str | None] = []
        if keywords and keywords.strip():
            clauses.append(f"search(objet, {quote(keywords.strip())})")
        cpv_clause = _cpv_clause(cpv.strip()) if cpv else None
        clauses.append(cpv_clause)
        if departments:
            codes = [c for dep in departments for c in _normalise_department(dep)]
            clauses.append(in_list("code_departement", _dedupe(codes)))
        if published_from:
            clauses.append(f"dateparution >= {date_literal(published_from)}")
        if published_to:
            clauses.append(f"dateparution <= {date_literal(published_to)}")
        if open_only:
            clauses.append("datelimitereponse >= now()")
        if notice_kind in NATURE_BY_KIND:
            clauses.append(f"nature = {quote(NATURE_BY_KIND[notice_kind])}")
        if market_type:
            clauses.append(in_list("type_marche", [market_type]))
        if buyer and buyer.strip():
            clauses.append(f"search(nomacheteur, {quote(buyer.strip())})")
        where = and_all(clauses)
        # Echo a readable query: an expanded CPV prefix can be ~12 KB of OR clauses.
        echo = where
        if cpv_clause is not None and where is not None and len(cpv_clause) > 300:
            n_codes = cpv_clause.count("search(donnees")
            echo = where.replace(cpv_clause, f"<cpv prefix {cpv.strip()}: {n_codes} codes>")
        params = {
            "select": LIST_FIELDS,
            "where": where,
            # idweb breaks ties between notices published the same day (stable paging).
            "order_by": "dateparution desc, idweb desc",
            "limit": limit,
            "offset": offset,
        }
        body = await self._http.get_json(self._base_url, params, source=SOURCE)
        records = [r for r in body.get("results", []) if isinstance(r, dict)]
        total = int(body.get("total_count", len(records)) or 0)
        next_offset = offset + len(records)
        return BoampSearchResult(
            page=PageInfo(
                total=total,
                offset=offset,
                returned=len(records),
                next_offset=(
                    next_offset
                    if records and next_offset < total and next_offset + limit <= MAX_WINDOW
                    else None
                ),
            ),
            notices=[_to_notice(r) for r in records],
            query=echo,
        )

    async def get_notice(self, notice_id: str) -> BoampNoticeDetail:
        """Fetch one notice and parse its payload into a compact detail view.

        Args:
            notice_id: BOAMP ``idweb`` such as ``26-112058``.

        Returns:
            The notice with description, CPV codes, value, lots, criteria and winners.

        Raises:
            ValueError: When ``notice_id`` is empty.
            NotFoundError: When no notice has this id.
            UpstreamError: When BOAMP fails.
        """
        clean = (notice_id or "").strip()
        if not clean:
            raise ValueError("notice_id is empty: pass a BOAMP id such as '26-112058'.")
        params = {"where": f"idweb = {quote(clean)}", "limit": 1}
        body = await self._http.get_json(self._base_url, params, source=SOURCE)
        records = body.get("results") or []
        if not records:
            raise NotFoundError(
                SOURCE,
                f"no BOAMP notice with id {clean!r}. Ids look like '26-112058'; "
                "get them from search_boamp_notices.",
                404,
            )
        record = records[0]
        parsed = parse_donnees(record.get("donnees"))
        winners = _dedupe([str(n) for n in _as_list(record.get("titulaire")) if n])
        related = _dedupe(
            [
                str(i)
                for key in ("annonce_lie", "annonces_anterieures")
                for i in _as_list(record.get(key))
            ]
        )
        return BoampNoticeDetail(
            **_to_notice(record).model_dump(),
            description=parsed["description"] or truncate(record.get("objet"), TEXT_MAX),
            cpv=parsed["cpv"],
            estimated_value=parsed["estimated_value"],
            currency=parsed["currency"],
            lots=parsed["lots"],
            award_criteria=parsed["award_criteria"],
            winners=winners or parsed["winners"],
            related_notices=related,
            eforms=parsed["eforms"],
        )
