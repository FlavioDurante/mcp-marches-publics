"""Unit tests for the shared HttpClient (retries, errors, Retry-After, cache, timeouts)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from marches_publics_mcp import http as http_module
from marches_publics_mcp.http import HttpClient, NotFoundError, UpstreamError

URL = "https://api.example.test/data"


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record asyncio.sleep delays inside http.py instead of waiting."""
    calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        calls.append(delay)

    monkeypatch.setattr(http_module.asyncio, "sleep", fake_sleep)
    return calls


@respx.mock
async def test_retry_on_503_then_success(http: HttpClient) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    assert await http.get_json(URL, source="t") == {"ok": True}
    assert route.call_count == 2


@respx.mock
async def test_retries_exhausted_raises_upstream_error(http: HttpClient) -> None:
    route = respx.get(URL).respond(503, json={"message": "down"})
    with pytest.raises(UpstreamError) as exc:
        await http.get_json(URL, source="t")
    assert exc.value.status == 503 and "down" in exc.value.message
    assert route.call_count == 3  # first attempt + max_retries=2


@respx.mock
async def test_no_retry_on_400(http: HttpClient) -> None:
    route = respx.get(URL).respond(400, json={"message": "bad query"})
    with pytest.raises(UpstreamError, match="bad query") as exc:
        await http.get_json(URL, source="t")
    assert exc.value.status == 400 and not isinstance(exc.value, NotFoundError)
    assert route.call_count == 1


@respx.mock
async def test_404_is_not_found_without_retry(http: HttpClient) -> None:
    route = respx.get(URL).respond(404, json={"error": "nope"})
    with pytest.raises(NotFoundError) as exc:
        await http.get_json(URL, source="t")
    assert exc.value.status == 404 and exc.value.source == "t"
    assert route.call_count == 1


@respx.mock
async def test_retry_after_is_honoured(http: HttpClient, sleeps: list[float]) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    assert await http.get_json(URL, source="t") == {"ok": 1}
    assert sleeps == [7.0]


@respx.mock
async def test_retry_after_is_capped(http: HttpClient, sleeps: list[float]) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "9999"}),
            httpx.Response(200, json={}),
        ]
    )
    await http.get_json(URL, source="t")
    assert sleeps == [30.0]


@respx.mock
async def test_cache_hit_avoids_second_call() -> None:
    client = HttpClient(cache_ttl=60)
    try:
        route = respx.get(URL).respond(json={"n": 1})
        assert await client.get_json(URL, {"q": "a"}, source="t") == {"n": 1}
        assert await client.get_json(URL, {"q": "a"}, source="t") == {"n": 1}
        assert route.call_count == 1
        await client.get_json(URL, {"q": "b"}, source="t")  # different params: no hit
        assert route.call_count == 2
    finally:
        await client.aclose()


@respx.mock
async def test_errors_are_not_cached() -> None:
    client = HttpClient(cache_ttl=60, max_retries=0)
    try:
        route = respx.get(URL).mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": 1})]
        )
        with pytest.raises(UpstreamError):
            await client.get_json(URL, source="t")
        assert await client.get_json(URL, source="t") == {"ok": 1}
        assert route.call_count == 2
    finally:
        await client.aclose()


@respx.mock
async def test_timeout_becomes_upstream_error(http: HttpClient) -> None:
    route = respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(UpstreamError, match="timed out") as exc:
        await http.get_json(URL, source="t")
    assert exc.value.status is None
    assert route.call_count == 3  # timeouts are retried too


@respx.mock
async def test_network_error_becomes_upstream_error(http: HttpClient) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(UpstreamError, match="network error"):
        await http.get_json(URL, source="t")


@respx.mock
async def test_invalid_json_body(http: HttpClient) -> None:
    respx.get(URL).respond(200, content=b"<html>not json</html>")
    with pytest.raises(UpstreamError, match="invalid JSON"):
        await http.get_json(URL, source="t")


@respx.mock
async def test_none_params_are_dropped_and_post_works(http: HttpClient) -> None:
    get_route = respx.get(URL).respond(json={})
    await http.get_json(URL, {"a": "1", "b": None}, source="t")
    assert dict(get_route.calls.last.request.url.params) == {"a": "1"}
    post_route = respx.post(URL).respond(json={"done": True})
    assert await http.post_json(URL, {"x": 1}, source="t") == {"done": True}
    assert json.loads(post_route.calls.last.request.content) == {"x": 1}


async def test_pacing_waits_between_calls_to_same_host(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = HttpClient(cache_ttl=0, min_interval={"api.example.test": 5.0})
    try:
        with respx.mock:
            respx.get(URL).respond(json={})
            await client.get_json(URL, source="t")
            await client.get_json(URL, source="t")
        assert len(waits) == 1 and 0 < waits[0] <= 5.0
    finally:
        await client.aclose()
