"""Tests for the MCP server layer, with fake source clients (no network)."""

from __future__ import annotations

import sys
from datetime import date
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from marches_publics_mcp.http import NotFoundError, UpstreamError
from marches_publics_mcp.models import (
    BoampNoticeDetail,
    BoampSearchResult,
    DecpAward,
    DecpRanking,
    DecpSearchResult,
    PageInfo,
    RankEntry,
    TedNoticeDetail,
    TedSearchResult,
)
from marches_publics_mcp.server import build_server

TOOLS = {
    "search_boamp_notices",
    "get_boamp_notice",
    "search_decp_awards",
    "rank_decp_parties",
    "search_ted_notices",
    "get_ted_notice",
    "lookup_company",
    "search_cpv_codes",
}


def _page(n: int) -> PageInfo:
    return PageInfo(total=n, offset=0, returned=n)


class FakeBoamp:
    def __init__(self, empty: bool = False, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.empty, self.error = empty, error

    async def search(self, **kwargs: Any) -> BoampSearchResult:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.empty:
            return BoampSearchResult(page=_page(0), notices=[])
        from marches_publics_mcp.models import BoampNotice

        notice = BoampNotice(id="26-1", title="Maintenance", buyer="Ville", published=None)
        return BoampSearchResult(page=_page(1), notices=[notice])

    async def get_notice(self, notice_id: str) -> BoampNoticeDetail:
        if notice_id == "26-404":
            raise NotFoundError("boamp", "no such notice", 404)
        return BoampNoticeDetail(id=notice_id, title="T", buyer="B", published=date(2026, 1, 2))


class FakeDecp:
    def __init__(self, empty: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.empty = empty

    async def search(self, **kwargs: Any) -> DecpSearchResult:
        self.calls.append(kwargs)
        if self.empty:
            return DecpSearchResult(page=_page(0), awards=[])
        award = DecpAward(id="a1", title="Marché", buyer_siret="1" * 14, amount_eur=1000.0)
        return DecpSearchResult(page=_page(1), awards=[award])

    async def rank(self, **kwargs: Any) -> DecpRanking:
        self.calls.append(kwargs)
        entries = [] if self.empty else [RankEntry(rank=1, siret="2" * 14, contracts=3)]
        return DecpRanking(
            by=kwargs["by"], order=kwargs["order"], total_contracts=3, entries=entries
        )


class FakeTed:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> TedSearchResult:
        self.calls.append(kwargs)
        return TedSearchResult(page=_page(0), notices=[])

    async def get_notice(self, publication_number: str) -> TedNoticeDetail:
        return TedNoticeDetail(publication_number=publication_number, title="EU")


class FakeResolver:
    async def lookup(self, siret: str) -> str | None:
        if siret.startswith("5"):
            raise UpstreamError("entreprises", "HTTP 503", 503)
        return "ACME SAS" if siret.startswith("3") else None

    async def name_for(self, siret: str) -> str | None:
        try:
            return await self.lookup(siret)
        except UpstreamError:
            return None


def _server(**overrides: Any) -> Any:
    clients: dict[str, Any] = {
        "boamp": FakeBoamp(),
        "decp": FakeDecp(),
        "ted": FakeTed(),
        "resolver": FakeResolver(),
    }
    clients.update(overrides)
    return build_server(**clients), clients


async def test_list_tools_annotations_and_schema_descriptions() -> None:
    server, _ = _server()
    async with Client(server) as c:
        tools = (await c.list_tools()).tools
        assert {t.name for t in tools} == TOOLS
        for tool in tools:
            ann = tool.annotations
            assert ann is not None and ann.read_only_hint and ann.open_world_hint
            assert ann.idempotent_hint
            assert tool.description and "Do NOT" in tool.description
            for name, prop in tool.input_schema.get("properties", {}).items():
                assert prop.get("description"), f"{tool.name}.{name} lacks a description"
        assert c.instructions and "search_boamp_notices" in c.instructions


async def test_boamp_happy_path_and_region_expansion() -> None:
    server, clients = _server()
    async with Client(server) as c:
        res = await c.call_tool(
            "search_boamp_notices",
            {"region": "Corse", "departments": ["Paris"], "cpv": "72000000", "limit": 5},
        )
    assert not res.is_error
    data = res.structured_content
    assert data["notices"][0]["id"] == "26-1"
    assert data["departments_used"] == ["2A", "2B", "75"]
    assert data["hint"] is None
    call = clients["boamp"].calls[0]
    assert call["departments"] == ["2A", "2B", "75"]
    assert call["cpv"] == "72000000" and call["limit"] == 5


async def test_boamp_empty_result_has_hint() -> None:
    server, _ = _server(boamp=FakeBoamp(empty=True))
    async with Client(server) as c:
        res = await c.call_tool("search_boamp_notices", {"keywords": "zzz"})
    assert "Widen" in res.structured_content["hint"]


async def test_get_boamp_notice_and_not_found() -> None:
    server, _ = _server()
    async with Client(server) as c:
        ok = await c.call_tool("get_boamp_notice", {"notice_id": "26-112058"})
        missing = await c.call_tool("get_boamp_notice", {"notice_id": "26-404"})
    assert ok.structured_content["id"] == "26-112058"
    assert missing.is_error and "nothing found" in missing.content[0].text


async def test_decp_search_rank_and_empty_hint() -> None:
    server, clients = _server()
    async with Client(server) as c:
        s = await c.call_tool("search_decp_awards", {"cpv_prefix": "72", "department": "31"})
        r = await c.call_tool("rank_decp_parties", {"by": "supplier", "order": "amount"})
    assert s.structured_content["awards"][0]["amount_eur"] == 1000.0
    assert clients["decp"].calls[0]["department"] == "31"
    assert r.structured_content["entries"][0]["rank"] == 1
    assert clients["decp"].calls[1]["top"] == 10

    empty_server, _ = _server(decp=FakeDecp(empty=True))
    async with Client(empty_server) as c:
        s = await c.call_tool("search_decp_awards", {})
        r = await c.call_tool("rank_decp_parties", {"by": "buyer"})
    assert s.structured_content["hint"] and r.structured_content["hint"]


async def test_ted_and_lookup() -> None:
    server, clients = _server()
    async with Client(server) as c:
        t = await c.call_tool("search_ted_notices", {"countries": ["deu"], "page": 2})
        g = await c.call_tool("get_ted_notice", {"publication_number": "757212-2026"})
        found = await c.call_tool("lookup_company", {"identifier": "3" * 14})
        unknown = await c.call_tool("lookup_company", {"identifier": "9" * 9})
    assert t.structured_content["hint"]
    assert clients["ted"].calls[0]["countries"] == ["DEU"] and clients["ted"].calls[0]["page"] == 2
    assert g.structured_content["publication_number"] == "757212-2026"
    assert found.structured_content["name"] == "ACME SAS"
    assert unknown.structured_content["name"] is None and unknown.structured_content["hint"]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search_boamp_notices", {"cpv": "7"}),
        ("search_cpv_codes", {"text": "x"}),
        ("search_boamp_notices", {"published_from": "yesterday"}),
        ("search_boamp_notices", {"limit": 99}),
        ("search_decp_awards", {"cpv_prefix": "7"}),
        ("search_decp_awards", {"department": "20"}),
        ("rank_decp_parties", {"by": "nobody"}),
        ("lookup_company", {"identifier": "123"}),
    ],
)
async def test_validation_errors_are_readable(tool: str, args: dict[str, Any]) -> None:
    server, clients = _server()
    async with Client(server) as c:
        res = await c.call_tool(tool, args)
    assert res.is_error
    assert res.content[0].text
    assert not clients["boamp"].calls and not clients["decp"].calls


async def test_unknown_region_and_value_error_are_tool_errors() -> None:
    server, _ = _server(boamp=FakeBoamp(error=ValueError("published_from is after published_to")))
    async with Client(server) as c:
        bad_region = await c.call_tool("search_boamp_notices", {"region": "Atlantide"})
        bad_dates = await c.call_tool("search_boamp_notices", {})
        bad_ted = await c.call_tool("search_ted_notices", {"countries": ["FR"]})
    assert bad_region.is_error and "Valid regions" in bad_region.content[0].text
    assert bad_dates.is_error and "published_from" in bad_dates.content[0].text
    assert bad_ted.is_error and "alpha-3" in bad_ted.content[0].text


async def test_upstream_error_names_the_source() -> None:
    err = UpstreamError("boamp", "HTTP 503: down", 503)
    server, _ = _server(boamp=FakeBoamp(error=err))
    async with Client(server) as c:
        res = await c.call_tool("search_boamp_notices", {})
    assert res.is_error
    assert "boamp" in res.content[0].text and "503" in res.content[0].text


async def test_resources_and_prompt() -> None:
    server, _ = _server()
    async with Client(server) as c:
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        regions = await c.read_resource("marches://regions")
        prompt = await c.get_prompt("veille_marches", {"cpv": "72000000", "region": "Occitanie"})
    assert uris == {"marches://regions", "marches://sources"}
    assert "Occitanie" in regions.contents[0].text
    assert "72000000" in prompt.messages[0].content.text


async def test_stdio_smoke() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "marches_publics_mcp"])
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == TOOLS


async def test_lookup_company_upstream_failure_is_not_unknown_identifier() -> None:
    server, _ = _server()
    async with Client(server) as c:
        res = await c.call_tool("lookup_company", {"identifier": "5" * 9})
    assert res.is_error
    assert "unavailable, retry later" in res.content[0].text
    assert "Unknown identifier" not in res.content[0].text


async def test_default_http_client_paces_recherche_entreprises() -> None:
    from marches_publics_mcp.server import _Clients

    clients = _Clients(None, None, None, None, None)
    try:
        assert clients.http._min_interval == {"recherche-entreprises.api.gouv.fr": 0.15}
    finally:
        await clients.aclose()


async def test_offset_bound_matches_client_window() -> None:
    server, _ = _server()
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    schema = tools["search_decp_awards"].input_schema["properties"]["offset"]
    assert schema["maximum"] == 9999
    assert "next_page" in (tools["search_ted_notices"].description or "")


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"cpv": "abc", "region": "Occitanie"}, "cpv"),
        ({"cpv": "72000000", "region": "Atlantide"}, "Atlantide"),
    ],
)
async def test_prompt_validates_arguments(args: dict[str, str], fragment: str) -> None:
    from mcp.shared.exceptions import MCPError

    server, _ = _server()
    async with Client(server) as c:
        with pytest.raises(MCPError) as exc:
            await c.get_prompt("veille_marches", args)
    assert fragment in str(exc.value)


async def test_search_cpv_codes_uses_bundled_nomenclature() -> None:
    server, _ = _server()
    async with Client(server) as c:
        hit = await c.call_tool("search_cpv_codes", {"text": "nettoyage", "limit": 5})
        miss = await c.call_tool("search_cpv_codes", {"text": "zzzzqqq"})
    matches = hit.structured_content["matches"]
    assert matches and all(m["code"].isdigit() and len(m["code"]) == 8 for m in matches)
    assert any("nettoyage" in m["label"].lower() for m in matches)
    assert miss.structured_content["matches"] == [] and miss.structured_content["hint"]


async def test_boamp_accepts_cpv_prefix() -> None:
    server, clients = _server()
    async with Client(server) as c:
        res = await c.call_tool("search_boamp_notices", {"cpv": "72"})
    assert not res.is_error
    assert clients["boamp"].calls[0]["cpv"] == "72"
