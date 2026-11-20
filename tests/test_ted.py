"""Tests for the TED client (respx-mocked) plus live integration checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from marches_publics_mcp.http import HttpClient, NotFoundError, UpstreamError
from marches_publics_mcp.sources.ted import (
    DEFAULT_BASE_URL,
    TedClient,
    _deadline,
    _pick_lang,
    build_query,
)

Loader = Callable[[str, str], Any]


def _sent(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


# ------------------------------------------------------------------ query building
def test_build_query_all_filters() -> None:
    query = build_query(
        keywords="logiciel maintenance",
        cpv="72000000",
        countries=["fra", "DEU"],
        published_from=date(2026, 11, 1),
        published_to=date(2026, 11, 20),
        open_only=True,
        notice_type="cn-standard",
        today=date(2026, 11, 20),
    )
    assert query == (
        'FT ~ "logiciel" AND FT ~ "maintenance" AND classification-cpv IN (72000000)'
        " AND place-of-performance IN (FRA, DEU) AND publication-date>=20261101"
        " AND publication-date<=20261120 AND deadline-receipt-tender-date-lot>=20261120"
        " AND notice-type IN (cn-standard) SORT BY publication-date DESC"
    )


def test_build_query_escapes_user_values() -> None:
    query = build_query(keywords='a\\b " OR 1=1')
    assert query.startswith('FT ~ "a\\\\b" AND FT ~ "OR" AND FT ~ "1=1"')
    assert '""' not in query


def test_build_query_cpv_check_digit_stripped() -> None:
    assert "classification-cpv IN (72000000)" in build_query(cpv="72000000-5")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cpv": "72 OR 1"},
        {"countries": ["FR"]},
        {"notice_type": "cn) OR (x"},
        {"published_from": date(2026, 2, 1), "published_to": date(2026, 1, 1)},
    ],
)
def test_build_query_rejects_bad_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build_query(**kwargs)


def test_build_query_without_filters_still_valid() -> None:
    assert build_query().startswith("publication-date>=")


def test_pick_lang_prefers_french_then_english() -> None:
    assert _pick_lang({"eng": "Hello", "fra": "Bonjour", "deu": "Hallo"}) == "Bonjour"
    assert _pick_lang({"deu": "Hallo", "eng": ["Hello"]}) == "Hello"
    assert _pick_lang({"deu": ["Hallo"]}) == "Hallo"
    assert _pick_lang({"fra": "Rue &amp;quot;X&amp;quot;"}) == 'Rue "X"'
    assert _pick_lang(None) is None


# ------------------------------------------------------------------ search
@respx.mock
async def test_search_maps_page_and_notices(http: HttpClient, fixture_loader: Loader) -> None:
    route = respx.post(DEFAULT_BASE_URL).mock(
        return_value=httpx.Response(200, json=fixture_loader("ted", "search_page"))
    )
    result = await TedClient(http).search(keywords="spectromètre", countries=["FRA"], limit=3)
    body = _sent(route)
    assert body["limit"] == 3 and body["page"] == 1 and "title-proc" in body["fields"]
    assert result.query == body["query"]
    assert result.page.total == 223
    assert (result.page.offset, result.page.returned, result.page.next_offset) == (0, 3, 3)
    first = result.notices[0]
    assert first.publication_number == "756375-2026"
    assert first.title == "Fourniture d'un spectromètre Gamma avec passeur d'échantillons"
    assert first.buyer == "CEA MARCOULE"
    assert first.buyer_country == "FRA"
    assert first.published == "2026-11-02"
    assert first.deadline == "2026-12-01"
    assert first.notice_type == "cn-standard"
    assert first.cpv == ["38433000"]
    assert first.url == "https://ted.europa.eu/fr/notice/-/detail/756375-2026"


@respx.mock
async def test_search_page_two_offsets_and_last_page(http: HttpClient) -> None:
    payload = {"notices": [], "totalNoticeCount": 25}
    route = respx.post(DEFAULT_BASE_URL).mock(return_value=httpx.Response(200, json=payload))
    client = TedClient(http)
    result = await client.search(page=2, limit=10)
    assert _sent(route)["page"] == 2
    assert (result.page.offset, result.page.next_offset) == (10, 20)
    assert result.next_page == 3
    result = await client.search(page=3, limit=10)
    assert (result.page.offset, result.page.next_offset) == (20, None)
    assert result.next_page is None


@respx.mock
async def test_search_window_limits(http: HttpClient) -> None:
    client = TedClient(http)
    with pytest.raises(ValueError, match="15000"):
        await client.search(page=61, limit=250)
    with pytest.raises(ValueError, match="limit"):
        await client.search(limit=251)
    with pytest.raises(ValueError, match="page"):
        await client.search(page=0)


@respx.mock
async def test_search_upstream_400_keeps_ted_message(http: HttpClient) -> None:
    respx.post(DEFAULT_BASE_URL).mock(
        return_value=httpx.Response(400, json={"message": "Invalid query: unexpected token"})
    )
    with pytest.raises(UpstreamError, match="unexpected token") as info:
        await TedClient(http).search(keywords="x")
    assert info.value.status == 400 and info.value.source == "ted"


@respx.mock
async def test_base_url_override(http: HttpClient) -> None:
    route = respx.post("http://example.test/search").mock(
        return_value=httpx.Response(200, json={"notices": [], "totalNoticeCount": 0})
    )
    await TedClient(http, base_url="http://example.test/search").search()
    assert route.called


# ------------------------------------------------------------------ detail
@respx.mock
async def test_get_notice_contract(http: HttpClient, fixture_loader: Loader) -> None:
    route = respx.post(DEFAULT_BASE_URL).mock(
        return_value=httpx.Response(200, json=fixture_loader("ted", "detail_contract"))
    )
    notice = await TedClient(http).get_notice("757212-2026")
    assert _sent(route)["query"] == "publication-number=757212-2026"
    assert notice.publication_number == "757212-2026"
    assert notice.buyer == "Commissariat à l'Energie Atomique"
    assert notice.title and "infogérance" in notice.title
    assert notice.description and "CEA Grenoble" in notice.description
    assert notice.procedure == "open"
    assert notice.winners == [] and notice.award_value is None
    assert notice.url == "https://ted.europa.eu/fr/notice/-/detail/757212-2026"
    assert notice.pdf_url == "https://ted.europa.eu/fr/notice/757212-2026/pdf"


@respx.mock
async def test_get_notice_award(http: HttpClient, fixture_loader: Loader) -> None:
    respx.post(DEFAULT_BASE_URL).mock(
        return_value=httpx.Response(200, json=fixture_loader("ted", "detail_award"))
    )
    notice = await TedClient(http).get_notice("756387-2026")
    assert notice.notice_type == "can-standard"
    assert notice.winners == ["ORONA SAS"]
    assert notice.award_value == 27000.0
    assert notice.currency == "EUR"
    assert notice.buyer == "DEPARTEMENT DE SAONE-ET-LOIRE"


@respx.mock
async def test_get_notice_estimated_value_from_lots(http: HttpClient) -> None:
    raw = {
        "publication-number": "792845-2026",
        "estimated-value-lot": ["1000", "500"],
        "estimated-value-cur-lot": ["EUR"],
        "estimated-value-proc": "0",
    }
    respx.post(DEFAULT_BASE_URL).mock(
        return_value=httpx.Response(200, json={"notices": [raw], "totalNoticeCount": 1})
    )
    notice = await TedClient(http).get_notice("792845-2026")
    assert (notice.estimated_value, notice.currency) == (1500.0, "EUR")


@pytest.mark.parametrize("bad", ["757212", "abc-2026", "757212-26", "757212-2026 OR 1=1", ""])
async def test_get_notice_bad_id(http: HttpClient, bad: str) -> None:
    with pytest.raises(ValueError, match="757212-2026"):
        await TedClient(http).get_notice(bad)


@respx.mock
async def test_get_notice_empty_is_not_found(http: HttpClient) -> None:
    respx.post(DEFAULT_BASE_URL).mock(
        return_value=httpx.Response(200, json={"notices": [], "totalNoticeCount": 0})
    )
    with pytest.raises(NotFoundError):
        await TedClient(http).get_notice("999999-2026")


# ------------------------------------------------------------------ live
@pytest.mark.integration
async def test_live_search_and_detail() -> None:
    client = HttpClient(cache_ttl=0)
    try:
        ted = TedClient(client)
        result = await ted.search(
            keywords="logiciel",
            countries=["FRA"],
            published_from=date.today().replace(day=1).replace(month=1),
            limit=3,
        )
        assert result.page.total > 0 and result.notices
        first = result.notices[0]
        detail = await ted.get_notice(first.publication_number)
        assert detail.publication_number == first.publication_number
        assert detail.title
    finally:
        await client.aclose()


@pytest.mark.integration
async def test_live_open_cpv_and_award() -> None:
    client = HttpClient(cache_ttl=0)
    try:
        ted = TedClient(client)
        opened = await ted.search(cpv="72000000", countries=["FRA"], open_only=True, limit=2)
        assert all(
            n.deadline is None or n.deadline >= date.today().isoformat() for n in opened.notices
        )
        awards = await ted.search(countries=["FRA"], notice_type="can-standard", limit=2)
        assert awards.notices
        detail = await ted.get_notice(awards.notices[0].publication_number)
        assert detail.notice_type == "can-standard"
        with pytest.raises(UpstreamError):
            await ted.search(notice_type="not-a-type-zz", limit=1)
    finally:
        await client.aclose()


def test_deadline_picks_earliest_upcoming_else_latest() -> None:
    today = date(2026, 11, 20)
    lots = ["2026-11-01+01:00", "2026-12-15+01:00", "2026-11-25+01:00"]
    assert _deadline(lots, today) == "2026-11-25"
    assert _deadline(["2026-11-20+01:00", "2026-11-01+01:00"], today) == "2026-11-20"
    assert _deadline(["2026-10-01+01:00", "2026-11-01+01:00"], today) == "2026-11-01"
    assert _deadline(None, today) is None
    assert _deadline("2026-12-01+01:00", today) == "2026-12-01"


def test_today_is_computed_in_brussels(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from marches_publics_mcp.sources import ted

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            assert tz == ZoneInfo("Europe/Brussels")
            return datetime(2026, 11, 21, 0, 30, tzinfo=tz)

    monkeypatch.setattr(ted, "datetime", FakeDatetime)
    assert ted._today() == date(2026, 11, 21)


@respx.mock
async def test_malformed_upstream_values_do_not_raise_value_error(
    http: HttpClient, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {
        "notices": [{"publication-number": "1-2026", "links": ["bad"], "notice-type": {"x": 1}}],
        "totalNoticeCount": "many",
    }
    respx.post(DEFAULT_BASE_URL).mock(return_value=httpx.Response(200, json=payload))
    with caplog.at_level("WARNING"):
        result = await TedClient(http).search()
    assert result.page.total == 1 and result.notices[0].url is None
    assert result.notices[0].notice_type is None


@respx.mock
async def test_unusable_notices_field_is_upstream_error(http: HttpClient) -> None:
    respx.post(DEFAULT_BASE_URL).mock(
        return_value=httpx.Response(200, json={"notices": "oops", "totalNoticeCount": 1})
    )
    with pytest.raises(UpstreamError, match="unexpected data"):
        await TedClient(http).search()
