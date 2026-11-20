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
* ``search(objet, "a b")`` is a token AND over the title, but each token is a *prefix*: "IA"
  matches "IARD", "data" matches "Datacenter". ``objet like "IA"`` matches the whole word
  (accent- and case-insensitive) but loses plurals. ``keywords`` therefore uses ``like`` for
  words of 4 letters or fewer (acronyms) and ``search`` for longer ones (measured live
  2026-11-20: "IA" 6 → 2 true hits, "data" 4 → 1).
* ``code_departement`` is multivalued and stored without zero padding for 1-9 (``"4"`` not
  ``"04"``); ``IN (...)`` matches if any value is in the list. Overseas codes are 3 digits.
  Corsica is stored as ``"20A"`` / ``"20B"``: ``"2A"`` / ``"2B"`` match nothing (0 rows in 2026
  against 854 / 773 for the stored spellings). Outputs are mapped back to the input convention.
* ``datelimitereponse >= now()`` works for "still open"; award notices have no deadline and are
  therefore excluded by ``open_only``. About 3 % of those "open" notices carry an absurd deadline
  (typos such as 2924-04-15), hence the upper bound ``<= now(years=2)`` (works live).
* ``datelimitereponse`` always carries a ``+00:00`` suffix that is often wrong: notices of the
  national form (FNS) store a naive Paris local time labelled UTC, EU-level (JOUE) ones store
  the true instant. The reliable value is ``gestion`` → ``DATE_LIMITE_REPONSE`` (naive Paris
  time, or with an explicit offset). It is only available inside ``gestion`` (~1 KB per record,
  ~100 KB for a page of 100: still acceptable), so list queries select it and drop it.
* ``etat`` (INITIAL, RECTIFICATIF, MODIFICATION, ANNULATION) tells rectifications from initial
  notices; ``annonce_lie`` (list of ids) and ``annonces_anterieures`` (a JSON text whose
  ``REFERENCE.IDWEB`` values are ids) link them. A tender is cancelled when an ``ANNULATION``
  notice has it in ``annonce_lie``: one extra query per page (ids in chunks of 50) detects it.
  Limit: the cancellation may point to a rectification instead of the initial notice; both the
  notice id and its linked ids are checked, but a link BOAMP does not publish stays invisible.
* ``limit`` is capped at 100 and ``offset + limit`` at 10000 (upstream returns HTTP 400 beyond).
* An unknown ``idweb`` is not a 404: ODS answers ``total_count: 0``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from marches_publics_mcp import cpv as cpv_nomenclature
from marches_publics_mcp.http import HttpClient, NotFoundError, UpstreamError
from marches_publics_mcp.models import (
    TEXT_MAX,
    BoampNotice,
    BoampNoticeDetail,
    BoampSearchResult,
    Lot,
    PageInfo,
    truncate,
)
from marches_publics_mcp.sources.ods import (
    and_all,
    date_literal,
    exclude_clause,
    in_list,
    keyword_clause,
    quote,
)

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
    "nature,nature_libelle,procedure_libelle,type_marche,url_avis,etat,annonce_lie,"
    "annonces_anterieures,gestion"
)
OPEN_MAX_YEARS = 2
CANCEL_CHUNK = 50
PARIS = ZoneInfo("Europe/Paris")
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
    """Return the spellings under which a department may be stored.

    ``'04'`` → ``['4', '04']``; Corsica ``'2A'`` → ``['2A', '20A']`` (BOAMP stores ``20A``).
    """
    clean = dep.strip().upper()
    if not re.fullmatch(r"\d{1,3}|2[AB]|20[AB]", clean):
        raise ValueError(
            f"department {dep!r} is invalid: use a code like '31', '2A' or '971' (no names)."
        )
    if clean[:2] in ("2A", "2B") or clean in ("20A", "20B"):
        letter = clean[-1]
        return [f"2{letter}", f"20{letter}"]
    stripped = clean.lstrip("0") or "0"
    return _dedupe([stripped, clean])


def _department_code(raw: str) -> str:
    """Map a stored code to the input convention (``'8'`` → ``'08'``, ``'20A'`` → ``'2A'``)."""
    code = raw.strip().upper()
    if code in ("20A", "20B"):
        return "2" + code[-1]
    return code.zfill(2) if code.isdigit() else code


def _gestion_deadline(raw: Any) -> str | None:
    """Read ``DATE_LIMITE_REPONSE`` from the ``gestion`` JSON text, or ``None``."""
    data = _parse_donnees(raw)
    return _first_text(data, "DATE_LIMITE_REPONSE")


def _paris_deadline(record: dict[str, Any]) -> str | None:
    """Return the response deadline as an ISO datetime in Europe/Paris time.

    The upstream ``datelimitereponse`` field is mislabelled UTC for many notices, so the value
    of ``gestion.DATE_LIMITE_REPONSE`` wins (naive = Paris local time, otherwise converted).
    Falls back to the upstream field untouched when ``gestion`` has no usable value.
    """
    raw = _gestion_deadline(record.get("gestion"))
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("unparsable deadline", extra={"source": SOURCE, "value": raw})
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=PARIS)
            return parsed.astimezone(PARIS).isoformat()
    fallback = record.get("datelimitereponse")
    return str(fallback) if fallback else None


def _linked_ids(record: dict[str, Any]) -> list[str]:
    """Ids of the notices linked to ``record`` (``annonce_lie`` and ``annonces_anterieures``)."""
    own = str(record.get("idweb", ""))
    ids = [str(i) for i in _as_list(record.get("annonce_lie")) if i]
    for item in _as_list(record.get("annonces_anterieures")):
        if isinstance(item, str) and item.strip().startswith(("{", "[")):
            ids.extend(_text(v) or "" for v in _find_all(_parse_json_any(item), "IDWEB"))
        elif isinstance(item, dict):
            ids.extend(_text(v) or "" for v in _find_all(item, "IDWEB"))
        elif item:
            ids.append(str(item))
    return [i for i in _dedupe(ids) if i != own]


def _parse_json_any(raw: str) -> Any:
    """Decode a JSON text of any shape; ``None`` when invalid."""
    try:
        return json.loads(raw)
    except ValueError:
        return None


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
        deadline=_paris_deadline(record),
        departments=_dedupe(
            [_department_code(str(d)) for d in _as_list(record.get("code_departement"))]
        ),
        notice_type=record.get("nature_libelle"),
        procedure=record.get("procedure_libelle"),
        market_types=[str(t) for t in _as_list(record.get("type_marche"))],
        url=record.get("url_avis"),
        status=record.get("etat"),
        linked_notices=_linked_ids(record),
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
        exclude_keywords: str | None = None,
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
            exclude_keywords: Words of which none may appear in the title (false positives).
            cpv: Exact 8-digit CPV code (``-<digit>`` suffix tolerated) matched as a token in
                the notice body, or a 2-7 digit prefix (``"72"``, ``"7221"``) expanded to every
                official sub-code and OR-ed (ValueError beyond 300 codes, e.g. '45').
            departments: Department codes (``"31"``, ``"2A"``, ``"971"``); any match qualifies.
            published_from: Earliest publication date (inclusive).
            published_to: Latest publication date (inclusive).
            open_only: Keep only notices whose response deadline is in the future and at most
                2 years away (upstream holds absurd deadlines such as year 2924, typos that
                would otherwise pass as "open"). Award notices have no deadline: excluded.
            notice_kind: ``tender`` (APPEL_OFFRE), ``award`` (ATTRIBUTION) or ``any``.
            market_type: ``TRAVAUX``, ``SERVICES`` or ``FOURNITURES``.
            buyer: Words that must appear in the buyer name.
            offset: Records to skip (``offset + limit`` must stay ≤ 10000).
            limit: Page size, 1 to 100.

        Returns:
            One page of notices with pagination info and the ODSQL ``where`` actually sent.
            Each notice has ``status`` (INITIAL, RECTIFICATIF...), ``linked_notices`` and
            ``cancelled`` (true when a BOAMP ANNULATION notice points to it, checked with one
            extra query per 50 notices; null if that check fails). Deadlines are in Europe/Paris
            time (read from the notice management data, because the upstream UTC label is wrong).
            ``departments`` use the input convention (``'08'``, ``'2A'``).

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
            clauses.append(keyword_clause("objet", keywords))
        if exclude_keywords and exclude_keywords.strip():
            clauses.append(exclude_clause("objet", exclude_keywords))
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
            clauses.append(
                "datelimitereponse >= now() "
                f"AND datelimitereponse <= now(years={OPEN_MAX_YEARS})"
            )
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
            notices=await self._with_cancellation([_to_notice(r) for r in records]),
            query=echo,
        )

    async def _cancelled_ids(self, ids: list[str]) -> set[str] | None:
        """Return which of ``ids`` are the target of an ANNULATION notice (``None`` on failure)."""
        found: set[str] = set()
        try:
            for start in range(0, len(ids), CANCEL_CHUNK):
                chunk = ids[start : start + CANCEL_CHUNK]
                where = and_all([f"nature = {quote('ANNULATION')}", in_list("annonce_lie", chunk)])
                params = {"select": "idweb,annonce_lie", "where": where, "limit": MAX_LIMIT}
                body = await self._http.get_json(self._base_url, params, source=SOURCE)
                for rec in body.get("results", []):
                    if isinstance(rec, dict):
                        found.update(str(i) for i in _as_list(rec.get("annonce_lie")))
        except UpstreamError as exc:
            logger.warning("cancellation check failed", extra={"source": SOURCE, "error": str(exc)})
            return None
        return found

    async def _with_cancellation(self, notices: list[BoampNotice]) -> list[BoampNotice]:
        """Set ``cancelled`` on each notice with one extra query per 50 ids."""
        ids = _dedupe([i for n in notices for i in [n.id, *n.linked_notices]])
        if not ids:
            return notices
        cancelled = await self._cancelled_ids(ids)
        if cancelled is None:
            return notices
        for notice in notices:
            notice.cancelled = any(i in cancelled for i in [notice.id, *notice.linked_notices])
        return notices

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
        (base,) = await self._with_cancellation([_to_notice(record)])
        related = base.linked_notices
        return BoampNoticeDetail(
            **base.model_dump(),
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
