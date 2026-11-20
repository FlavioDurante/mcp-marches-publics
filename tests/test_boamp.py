"""Tests for the BOAMP client (respx-mocked HTTP, plus live integration tests)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
import respx

from marches_publics_mcp.http import HttpClient, NotFoundError, UpstreamError
from marches_publics_mcp.sources.boamp import DEFAULT_BASE_URL, BoampClient, parse_donnees

Loader = Callable[[str, str], Any]


def _where(route: respx.Route) -> str:
    return route.calls.last.request.url.params["where"]


@pytest.fixture
def client(http: HttpClient) -> BoampClient:
    return BoampClient(http)


# ---------------------------------------------------------------- query building
@respx.mock
async def test_search_without_filters_has_no_where(
    client: BoampClient, fixture_loader: Loader
) -> None:
    route = respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "search_page"))
    await client.search()
    params = route.calls.last.request.url.params
    assert "where" not in params
    assert params["order_by"] == "dateparution desc, idweb desc"
    assert params["limit"] == "10"
    assert "donnees" not in params["select"].split(",")


@respx.mock
async def test_search_builds_every_filter(client: BoampClient, fixture_loader: Loader) -> None:
    route = respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "search_page"))
    result = await client.search(
        keywords='logiciel "ERP"',
        cpv="72000000-5",
        departments=["04", "2a"],
        published_from=date(2026, 11, 1),
        published_to=date(2026, 11, 20),
        open_only=True,
        notice_kind="tender",
        market_type="SERVICES",
        buyer="Toulouse",
    )
    where = _where(route)
    assert 'search(objet, "logiciel \\"ERP\\"")' in where
    assert 'search(donnees, "72000000")' in where
    assert 'code_departement IN ("4", "04", "2A")' in where
    assert "dateparution >= date'2026-11-01'" in where
    assert "dateparution <= date'2026-11-20'" in where
    assert "datelimitereponse >= now()" in where
    assert 'nature = "APPEL_OFFRE"' in where
    assert 'type_marche IN ("SERVICES")' in where
    assert 'search(nomacheteur, "Toulouse")' in where
    assert result.query == where


@respx.mock
async def test_notice_kind_award_and_any(client: BoampClient, fixture_loader: Loader) -> None:
    route = respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "search_page"))
    await client.search(notice_kind="award")
    assert _where(route) == '(nature = "ATTRIBUTION")'
    await client.search(notice_kind="any")
    assert "nature" not in route.calls.last.request.url.params.get("where", "")


@respx.mock
async def test_base_url_override(http: HttpClient, fixture_loader: Loader) -> None:
    route = respx.get("https://example.test/records").respond(
        json=fixture_loader("boamp", "search_page")
    )
    await BoampClient(http, "https://example.test/records").search()
    assert route.called


@respx.mock
async def test_cpv_prefix_expands_to_or_of_subcodes(
    client: BoampClient, fixture_loader: Loader
) -> None:
    route = respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "search_page"))
    await client.search(cpv="72212")
    where = _where(route)
    assert where.count("(search(donnees,") == 3  # grouped in chunks of 50
    assert 'search(donnees, "72212000")' in where
    assert 'search(donnees, "72212100")' in where
    assert where.count("search(donnees,") == 139
    assert 'search(donnees, "72000000")' not in where


@respx.mock
async def test_cpv_prefix_star_and_combined_filters(
    client: BoampClient, fixture_loader: Loader
) -> None:
    route = respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "search_page"))
    await client.search(cpv="72*", departments=["31"], open_only=True)
    where = _where(route)
    assert 'search(donnees, "72212000")' in where
    assert where.count("search(donnees,") == 252
    assert 'AND (code_departement IN ("31"))' in where


@respx.mock
async def test_cpv_exact_code_unchanged(client: BoampClient, fixture_loader: Loader) -> None:
    route = respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "search_page"))
    await client.search(cpv="72212000")
    assert _where(route) == '(search(donnees, "72212000"))'


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"cpv": "7"}, "CPV code"),
        ({"cpv": "72x"}, "CPV code"),
        ({"cpv": "99"}, "no CPV code starts with"),
        ({"cpv": "45"}, "longer prefix"),
        ({"departments": ["Haute-Garonne"]}, "department"),
        ({"published_from": date(2026, 2, 1), "published_to": date(2026, 1, 1)}, "on or before"),
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"offset": -1}, "offset"),
    ],
)
async def test_invalid_input_raises_helpful_value_error(
    client: BoampClient, kwargs: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(ValueError, match=fragment):
        await client.search(**kwargs)


async def test_window_overflow_message(client: BoampClient) -> None:
    with pytest.raises(ValueError, match="Narrow the search"):
        await client.search(offset=9995, limit=10)


# ---------------------------------------------------------------- mapping and pagination
@respx.mock
async def test_search_maps_records_and_paginates(
    client: BoampClient, fixture_loader: Loader
) -> None:
    page = fixture_loader("boamp", "search_page")
    respx.get(DEFAULT_BASE_URL).respond(json=page)
    result = await client.search(limit=4, offset=8)
    first = result.notices[0]
    raw = page["results"][0]
    assert first.id == raw["idweb"]
    assert first.title == " ".join(raw["objet"].split())
    assert first.published == date.fromisoformat(raw["dateparution"])
    assert first.departments == raw["code_departement"]
    assert first.notice_type == raw["nature_libelle"]
    assert first.url == raw["url_avis"]
    assert result.page.total == page["total_count"]
    assert result.page.returned == 4
    assert result.page.next_offset == 12


@respx.mock
async def test_last_page_has_null_next_offset(client: BoampClient, fixture_loader: Loader) -> None:
    page = fixture_loader("boamp", "search_page")
    page["total_count"] = 12
    respx.get(DEFAULT_BASE_URL).respond(json=page)
    result = await client.search(limit=4, offset=8)
    assert result.page.next_offset is None


@respx.mock
async def test_next_offset_never_crosses_window(client: BoampClient) -> None:
    body = {"total_count": 50000, "results": [{"idweb": "26-1", "dateparution": "2026-01-01"}]}
    respx.get(DEFAULT_BASE_URL).respond(json=body)
    result = await client.search(limit=1, offset=9999)
    assert result.page.next_offset is None


@respx.mock
async def test_empty_result(client: BoampClient) -> None:
    respx.get(DEFAULT_BASE_URL).respond(json={"total_count": 0, "results": []})
    result = await client.search(keywords="zzzz")
    assert result.notices == []
    assert result.page.next_offset is None


@respx.mock
async def test_upstream_400_becomes_upstream_error(client: BoampClient) -> None:
    respx.get(DEFAULT_BASE_URL).respond(400, json={"message": "ODSQL syntax exception"})
    with pytest.raises(UpstreamError, match="ODSQL"):
        await client.search(keywords="x")


# ---------------------------------------------------------------- get_notice
@respx.mock
async def test_get_notice_unknown_id_is_not_found(
    client: BoampClient, fixture_loader: Loader
) -> None:
    route = respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "notice_unknown"))
    with pytest.raises(NotFoundError, match="26-000"):
        await client.get_notice("26-000")
    assert _where(route) == 'idweb = "26-000"'


async def test_get_notice_empty_id(client: BoampClient) -> None:
    with pytest.raises(ValueError, match="notice_id"):
        await client.get_notice("  ")


@respx.mock
async def test_get_notice_eforms_tender(client: BoampClient, fixture_loader: Loader) -> None:
    respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "notice_eforms_tender"))
    notice = await client.get_notice("26-112177")
    assert notice.eforms is True
    assert notice.id == "26-112177"
    assert notice.description
    assert notice.cpv == ["45112500", "45233293", "45112710"]
    assert [lot.number for lot in notice.lots] == ["LOT-0001", "LOT-0002"]
    assert notice.lots[0].title == "Voirie et réseaux divers"
    assert notice.lots[0].estimated_value == 6_800_000
    assert notice.estimated_value == 9_600_000  # sum of the lot estimates
    assert notice.award_criteria and "prix" in notice.award_criteria.lower()
    assert notice.winners == []


@respx.mock
async def test_get_notice_eforms_award_has_winners(
    client: BoampClient, fixture_loader: Loader
) -> None:
    respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "notice_eforms_award"))
    notice = await client.get_notice("26-112069")
    assert notice.winners == ["VENDREDI 13", "DIMO", "JORDENEN"]
    assert notice.estimated_value == 720_000
    assert notice.currency == "EUR"
    assert notice.cpv == ["39294100"]


@respx.mock
async def test_get_notice_legacy_fns(client: BoampClient, fixture_loader: Loader) -> None:
    respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "notice_legacy_fns_tender"))
    notice = await client.get_notice("26-112779")
    assert notice.eforms is False
    assert notice.cpv == ["79311000"]
    assert notice.description and notice.description.startswith("Restauration du pont-vieux")
    assert notice.award_criteria and "65 %" in notice.award_criteria
    assert notice.lots == []


@respx.mock
async def test_get_notice_legacy_joue_award(client: BoampClient, fixture_loader: Loader) -> None:
    respx.get(DEFAULT_BASE_URL).respond(json=fixture_loader("boamp", "notice_legacy_joue_award"))
    notice = await client.get_notice("23-26865")
    assert notice.winners == ["Eurovia ile de france", "Bouygues energies et services"]
    assert [lot.number for lot in notice.lots] == ["1", "2", "3"]
    assert notice.cpv == ["45233140", "45112710", "34993000"]
    assert notice.estimated_value == 6_327_883
    assert notice.currency == "EUR"
    assert notice.award_criteria == "Valeur technique 60%; Prix 40%"


# ---------------------------------------------------------------- parser robustness
def test_eforms_winners_resolved_from_notice_result(fixture_loader: Loader) -> None:
    """Without the top-level ``titulaire`` field, winners come from the eForms result block."""
    record = fixture_loader("boamp", "notice_eforms_award")["results"][0]
    assert parse_donnees(record["donnees"])["winners"] == ["VENDREDI 13", "DIMO", "JORDENEN"]


@pytest.mark.parametrize(
    "raw", [None, "", "not json", "[]", "{}", '{"EFORMS": {}}', '{"EFORMS": 3}']
)
def test_parser_never_crashes_on_odd_payloads(raw: Any) -> None:
    parsed = parse_donnees(raw)
    assert parsed["cpv"] == [] and parsed["lots"] == [] and parsed["winners"] == []
    assert parsed["estimated_value"] is None and parsed["award_criteria"] is None


def test_fixtures_contain_no_personal_contact_data(fixture_loader: Loader) -> None:
    for name in ("notice_eforms_tender", "notice_eforms_award", "notice_legacy_fns_tender"):
        text = str(fixture_loader("boamp", name))
        assert (
            "ElectronicMail" not in text and "Telephone" not in text and "cac:Contact" not in text
        )


# ---------------------------------------------------------------- integration (live API)
@pytest.mark.integration
async def test_live_search_and_get_notice() -> None:
    http = HttpClient(cache_ttl=0)
    try:
        boamp = BoampClient(http)
        result = await boamp.search(notice_kind="tender", published_from=date(2026, 11, 1), limit=3)
        assert result.page.total > 0 and len(result.notices) == 3
        assert result.notices[0].published is not None
        detail = await boamp.get_notice(result.notices[0].id)
        assert detail.id == result.notices[0].id
        assert detail.description
        with pytest.raises(NotFoundError):
            await boamp.get_notice("00-0")
    finally:
        await http.aclose()


@pytest.mark.integration
async def test_live_cpv_department_and_open_filters() -> None:
    http = HttpClient(cache_ttl=0)
    try:
        boamp = BoampClient(http)
        result = await boamp.search(
            cpv="72000000", departments=["31"], open_only=True, notice_kind="tender", limit=5
        )
        assert result.page.total >= 0
        for notice in result.notices:
            assert "31" in notice.departments
            assert notice.deadline is not None
    finally:
        await http.aclose()


@pytest.mark.integration
async def test_live_cpv_prefix_finds_more_than_exact_code() -> None:
    http = HttpClient(cache_ttl=0)
    try:
        boamp = BoampClient(http)
        common = {"departments": ["31"], "open_only": True, "limit": 1}
        exact = await boamp.search(cpv="72000000", **common)
        prefix = await boamp.search(cpv="72", **common)
        assert prefix.page.total >= exact.page.total
        assert prefix.page.total > 0
        rhone = await boamp.search(
            cpv="9091", departments=["69"], published_from=date(2026, 1, 1), limit=1
        )
        assert rhone.page.total >= 0
    finally:
        await http.aclose()


async def test_prefix_query_echo_is_compact(http, fixture_loader) -> None:
    """An expanded CPV prefix must not flood the model with ~12 KB of OR clauses."""
    import respx

    from marches_publics_mcp.sources.boamp import BoampClient

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(url__startswith="https://").respond(json={"total_count": 0, "results": []})
        result = await BoampClient(http).search(cpv="72", departments=["31"])
    sent = route.calls[0].request.url.params["where"]
    assert sent.count("search(donnees") > 100
    assert result.query is not None and len(result.query) < 400
    assert "<cpv prefix 72:" in result.query and "code_departement" in result.query
