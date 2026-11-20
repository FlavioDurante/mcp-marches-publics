"""Tests for the DECP awards client."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
import respx

from marches_publics_mcp.http import HttpClient, UpstreamError
from marches_publics_mcp.sources.decp import DecpClient
from marches_publics_mcp.sources.entreprises import CompanyResolver

URL = "https://decp.test/records"
ENT = "https://ent.test"
Loader = Callable[[str, str], Any]


@pytest.fixture
def client(http: HttpClient) -> DecpClient:
    return DecpClient(http, base_url=URL)


def _where(route: respx.Route, index: int = 0) -> str | None:
    return route.calls[index].request.url.params.get("where")


@respx.mock
async def test_search_parses_records_and_cdl(client: DecpClient, fixture_loader: Loader) -> None:
    respx.get(URL).respond(json=fixture_loader("decp", "search"))
    res = await client.search()
    assert res.page.total == 3 and res.page.returned == 3 and res.page.next_offset is None
    first, last = res.awards[0], res.awards[-1]
    assert first.supplier_sirets == ["39297563700043", "81328921200074", "87927138500012"]
    assert first.amount_eur == 771116.0 and first.offers_received == 14
    assert first.notified == date(2026, 11, 16) and first.duration_months == 12
    assert first.place_code == "75012" and first.cpv == "80530000-8"
    # CDL placeholders are dropped
    assert last.supplier_sirets == ["97921149700016"]
    assert last.supplier_names == [None]


@respx.mock
async def test_search_query_building(client: DecpClient) -> None:
    route = respx.get(URL).respond(json={"total_count": 0, "results": []})
    res = await client.search(
        keywords="nettoyage",
        cpv_prefix="9091",
        buyer_siret="217 500 016 00019",
        supplier_siret="41101878100079",
        department="31",
        notified_from=date(2026, 1, 1),
        notified_to=date(2026, 6, 30),
        min_amount=1000,
        max_amount=250000.5,
        procedure="Procédure adaptée",
        order_by="amount",
        offset=20,
        limit=5,
    )
    where = _where(route)
    assert where is not None and res.query == where
    for part in (
        'search(objet, "nettoyage")',
        'startswith(codecpv, "9091")',
        'acheteur_id = "21750001600019"',
        'titulaire_id_1 = "41101878100079" OR titulaire_id_2 = "41101878100079"',
        'startswith(lieuexecution_code, "31")',
        "datenotification >= date'2026-01-01'",
        "datenotification <= date'2026-06-30'",
        "montant >= 1000",
        "montant <= 250000.5",
        'procedure = "Procédure adaptée"',
    ):
        assert part in where
    params = route.calls[0].request.url.params
    assert params["order_by"] == "montant desc"
    assert params["limit"] == "5" and params["offset"] == "20"


@respx.mock
async def test_search_no_filter_and_default_order(client: DecpClient) -> None:
    route = respx.get(URL).respond(json={"total_count": 0, "results": []})
    await client.search()
    assert "where" not in route.calls[0].request.url.params
    assert route.calls[0].request.url.params["order_by"] == "datenotification desc"


@respx.mock
async def test_corsica_department(client: DecpClient) -> None:
    route = respx.get(URL).respond(json={"total_count": 0, "results": []})
    await client.search(department="2a")
    where = _where(route) or ""
    assert '"2A"' in where and '"200"' in where and '"201"' in where


@respx.mock
async def test_pagination_next_offset(client: DecpClient, fixture_loader: Loader) -> None:
    data = fixture_loader("decp", "search")
    data["total_count"] = 50
    respx.get(URL).respond(json=data)
    res = await client.search(offset=10, limit=3)
    assert res.page.next_offset == 13 and res.page.offset == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cpv_prefix": "abc"},
        {"cpv_prefix": "1"},
        {"buyer_siret": "123"},
        {"department": "Haute-Garonne"},
        {"notified_from": date(2026, 2, 1), "notified_to": date(2026, 1, 1)},
        {"min_amount": 10, "max_amount": 5},
        {"limit": 0},
        {"limit": 101},
        {"offset": 9995, "limit": 10},
        {"offset": -1},
    ],
)
async def test_search_validation(client: DecpClient, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await client.search(**kwargs)


@respx.mock
async def test_cpv_with_check_digit_is_trimmed(client: DecpClient) -> None:
    route = respx.get(URL).respond(json={"total_count": 0, "results": []})
    await client.search(cpv_prefix="90910000-9")
    assert 'startswith(codecpv, "90910000")' in (_where(route) or "")


@respx.mock
async def test_upstream_error(client: DecpClient) -> None:
    respx.get(URL).respond(400, json={"message": "bad ODSQL"})
    with pytest.raises(UpstreamError):
        await client.search(keywords="x")


@respx.mock
async def test_rank_buyer(client: DecpClient, fixture_loader: Loader) -> None:
    def handler(request: Any) -> Any:
        import httpx

        if "group_by" in request.url.params:
            return httpx.Response(200, json=fixture_loader("decp", "rank_buyer"))
        return httpx.Response(200, json={"total_count": 748548, "results": []})

    route = respx.get(URL).mock(side_effect=handler)
    rank = await client.rank(
        by="buyer", cpv_prefix="9091", department="75", notified_from=date(2026, 1, 1), top=3
    )
    agg = next(c.request for c in route.calls if "group_by" in c.request.url.params)
    p = agg.url.params
    assert p["group_by"] == "acheteur_id" and p["limit"] == "3" and p["order_by"] == "n desc"
    assert p["select"] == "acheteur_id, count(*) as n, sum(montant) as total"
    assert 'acheteur_id != "CDL"' in p["where"] and 'startswith(codecpv, "9091")' in p["where"]
    assert rank.total_contracts == 748548
    assert [e.rank for e in rank.entries] == [1, 2, 3]
    assert rank.entries[0].contracts == 6844 and rank.entries[0].total_amount_eur == 14966777568.9
    assert rank.filters == {"cpv_prefix": "9091", "department": "75", "notified_from": "2026-01-01"}
    text = " ".join(rank.caveats).lower()
    assert "maximum" in text and "first co-contractor" in text and "incomplete" in text
    assert "idaccordcadre" in text and "counted twice" in text and "lieuexecution_code" in text


@respx.mock
async def test_rank_supplier_by_amount(client: DecpClient, fixture_loader: Loader) -> None:
    route = respx.get(URL).respond(json=fixture_loader("decp", "rank_supplier"))
    rank = await client.rank(by="supplier", order="amount")
    p = route.calls[0].request.url.params
    assert p["group_by"] == "titulaire_id_1" and p["order_by"] == "total desc"
    assert 'titulaire_typeidentifiant_1 = "SIRET"' in p["where"]
    assert rank.order == "amount" and rank.by == "supplier"


@respx.mock
async def test_rank_skips_placeholder_rows_and_validates(client: DecpClient) -> None:
    respx.get(URL).respond(
        json={
            "total_count": 2,
            "results": [
                {"acheteur_id": "CDL", "n": 9, "total": 1.0},
                {"acheteur_id": "21750001600019", "n": 2, "total": None},
            ],
        }
    )
    rank = await client.rank(by="buyer")
    assert [e.siret for e in rank.entries] == ["21750001600019"]
    assert rank.entries[0].total_amount_eur is None
    with pytest.raises(ValueError):
        await client.rank(by="buyer", top=0)
    with pytest.raises(ValueError):
        await client.rank(by="nope")  # type: ignore[arg-type]


@respx.mock
async def test_names_wired_into_search_and_rank(http: HttpClient, fixture_loader: Loader) -> None:
    import httpx

    def ent(request: httpx.Request) -> httpx.Response:
        siret = request.url.params["q"]
        return httpx.Response(
            200, json={"results": [{"siren": siret[:9], "nom_complet": f"CO {siret[:9]}"}]}
        )

    respx.get(f"{ENT}/search").mock(side_effect=ent)
    respx.get(URL).mock(
        side_effect=lambda r: httpx.Response(
            200,
            json=fixture_loader("decp", "rank_buyer" if "group_by" in r.url.params else "search"),
        )
    )
    client = DecpClient(http, base_url=URL, resolver=CompanyResolver(http, base_url=ENT))
    res = await client.search()
    assert res.awards[0].buyer_name == "CO 824228142"
    assert res.awards[0].supplier_names == ["CO 392975637", "CO 813289212", "CO 879271385"]
    rank = await client.rank(by="buyer")
    assert rank.entries[0].name == "CO 217500016"


@respx.mock
async def test_resolver_failure_leaves_names_empty(
    http: HttpClient, fixture_loader: Loader
) -> None:
    respx.get(f"{ENT}/search").respond(500)
    respx.get(URL).respond(json=fixture_loader("decp", "search"))
    client = DecpClient(http, base_url=URL, resolver=CompanyResolver(http, base_url=ENT))
    res = await client.search()
    assert res.awards[0].buyer_name is None
    assert res.awards[0].supplier_names == [None, None, None]


@pytest.mark.integration
async def test_live_search_and_rank() -> None:
    http = HttpClient()
    try:
        client = DecpClient(http)
        res = await client.search(cpv_prefix="9091", department="31", limit=3)
        assert res.page.total > 0 and res.awards
        assert all((a.cpv or "").startswith("9091") for a in res.awards)
        rank = await client.rank(by="buyer", cpv_prefix="9091", top=3)
        assert 0 < len(rank.entries) <= 3 and rank.total_contracts > 0
        assert rank.entries[0].contracts >= rank.entries[-1].contracts
    finally:
        await http.aclose()


@respx.mock
async def test_department_filter_excludes_region_and_country_codes(client: DecpClient) -> None:
    route = respx.get(URL).respond(json={"total_count": 0, "results": []})
    await client.search(department="75")
    where = _where(route) or ""
    assert 'lieuexecution_typecode IN ("Code postal", "Code département", "Code commune")' in where
    assert "Code région" not in where and "Code pays" not in where
    assert 'startswith(lieuexecution_code, "75")' in where


@respx.mock
async def test_malformed_row_values_become_none(
    client: DecpClient, caplog: pytest.LogCaptureFixture
) -> None:
    row = {
        "id": "x1",
        "montant": "n/a",
        "datenotification": "not-a-date",
        "dureemois": "twelve",
        "objet": "Marché",
    }
    respx.get(URL).respond(json={"total_count": "oops", "results": [row]})
    with caplog.at_level("WARNING"):
        res = await client.search()
    award = res.awards[0]
    assert (award.amount_eur, award.notified, award.duration_months) == (None, None, None)
    assert res.page.total == 1
    assert any("malformed upstream value" in r.message for r in caplog.records)


@respx.mock
async def test_unusable_response_is_upstream_error(client: DecpClient) -> None:
    respx.get(URL).respond(json=["not", "a", "dict"])
    with pytest.raises(UpstreamError, match="unexpected data"):
        await client.search()


@respx.mock
async def test_next_offset_never_exceeds_window(client: DecpClient) -> None:
    rows = [{"id": str(i)} for i in range(50)]
    respx.get(URL).respond(json={"total_count": 50000, "results": rows})
    res = await client.search(offset=9900, limit=50)
    assert res.page.next_offset == 9950  # 9950 + 50 == 10000: still a valid request
    res = await client.search(offset=9950, limit=50)
    assert res.page.next_offset is None  # the next page would leave the upstream window
