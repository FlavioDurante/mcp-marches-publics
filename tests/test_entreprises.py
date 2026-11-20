"""Tests for the company-name resolver."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from marches_publics_mcp.http import HttpClient
from marches_publics_mcp.sources.entreprises import CompanyResolver

BASE = "https://recherche-entreprises.test"
PARIS = "21750001600019"


@pytest.fixture
def resolver(http: HttpClient) -> CompanyResolver:
    return CompanyResolver(http, base_url=BASE)


@respx.mock
async def test_name_for_siret_and_memo(
    resolver: CompanyResolver, fixture_loader: Callable[[str, str], Any]
) -> None:
    route = respx.get(f"{BASE}/search").respond(json=fixture_loader("entreprises", PARIS))
    assert await resolver.name_for(PARIS) == "VILLE DE PARIS"
    assert await resolver.name_for(PARIS) == "VILLE DE PARIS"
    first = route.calls[0].request
    assert first.url.params["q"] == PARIS
    assert first.url.params["per_page"] == "1"
    # the SIRET lookup was memoised: only one call for PARIS
    assert sum(1 for c in route.calls if c.request.url.params["q"] == PARIS) == 1


@respx.mock
async def test_name_for_siren(
    resolver: CompanyResolver, fixture_loader: Callable[[str, str], Any]
) -> None:
    respx.get(f"{BASE}/search").respond(json=fixture_loader("entreprises", PARIS))
    assert await resolver.name_for(PARIS[:9]) == "VILLE DE PARIS"


@respx.mock
async def test_duplicated_name_is_collapsed(resolver: CompanyResolver) -> None:
    body = {"results": [{"siren": "411018781", "nom_complet": "INFRANEO (INFRANEO)"}]}
    respx.get(f"{BASE}/search").respond(json=body)
    assert await resolver.name_for("41101878100079") == "INFRANEO"


@respx.mock
async def test_unknown_and_invalid_inputs(
    resolver: CompanyResolver, fixture_loader: Callable[[str, str], Any]
) -> None:
    route = respx.get(f"{BASE}/search").respond(json=fixture_loader("entreprises", "empty"))
    assert await resolver.name_for("00000000000000") is None
    assert await resolver.name_for("abc") is None
    assert await resolver.name_for("123") is None
    assert route.call_count == 1


@respx.mock
async def test_mismatched_siren_is_rejected(resolver: CompanyResolver) -> None:
    respx.get(f"{BASE}/search").respond(
        json={"results": [{"siren": "999999999", "nom_complet": "OTHER"}]}
    )
    assert await resolver.name_for(PARIS) is None


@respx.mock
async def test_failure_returns_none_and_logs(
    resolver: CompanyResolver, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(f"{BASE}/search").respond(500, json={"message": "boom"})
    with caplog.at_level("WARNING"):
        assert await resolver.name_for(PARIS) is None
    assert any("company lookup failed" in r.message for r in caplog.records)


@respx.mock
async def test_network_error_returns_none(resolver: CompanyResolver) -> None:
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("down"))
    assert await resolver.name_for(PARIS) is None


@respx.mock
async def test_dirigeants_never_exposed(resolver: CompanyResolver) -> None:
    body = {
        "results": [
            {
                "siren": PARIS[:9],
                "nom_complet": "VILLE DE PARIS",
                "dirigeants": [{"nom": "REDACTED", "prenoms": "REDACTED"}],
            }
        ]
    }
    respx.get(f"{BASE}/search").respond(json=body)
    result = await resolver.names_for([PARIS])
    assert "REDACTED" not in json.dumps(result)
    assert result == {PARIS: "VILLE DE PARIS"}


@respx.mock
async def test_names_for_bounded_concurrency(http: HttpClient) -> None:
    resolver = CompanyResolver(http, base_url=BASE)
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        siret = request.url.params["q"]
        return httpx.Response(200, json={"results": [{"siren": siret[:9], "nom_complet": "N"}]})

    respx.get(f"{BASE}/search").mock(side_effect=handler)
    sirets = [f"{i:09d}00012" for i in range(1, 13)]
    result = await resolver.names_for(sirets + sirets[:3])
    assert len(result) == 12
    assert all(v == "N" for v in result.values())
    assert 1 < peak <= 5


@pytest.mark.integration
async def test_live_lookup() -> None:
    client = HttpClient()
    try:
        assert await CompanyResolver(client).name_for(PARIS) == "VILLE DE PARIS"
    finally:
        await client.aclose()
