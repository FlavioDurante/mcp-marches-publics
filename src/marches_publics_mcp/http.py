"""Shared async HTTP client: timeouts, retries with exponential backoff, TTL cache, pacing."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

import httpx

from marches_publics_mcp import __version__

logger = logging.getLogger(__name__)

USER_AGENT = f"mcp-marches-publics/{__version__} (+https://github.com/; open-source MCP server)"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class UpstreamError(Exception):
    """An upstream API failed in a way the caller should report to the model.

    Attributes:
        source: Short source name (``boamp``, ``decp``, ``ted``, ``entreprises``).
        status: HTTP status code, or ``None`` for network errors.
        message: Human-readable explanation, safe to show to the model.
    """

    def __init__(self, source: str, message: str, status: int | None = None) -> None:
        super().__init__(f"[{source}] {message}")
        self.source = source
        self.status = status
        self.message = message


class NotFoundError(UpstreamError):
    """The requested object does not exist upstream (HTTP 404 or empty lookup)."""


class _TTLCache:
    """Small in-memory LRU cache with a per-entry time-to-live."""

    def __init__(self, ttl: float, max_entries: int) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        if self._ttl <= 0 or self._max <= 0:
            return
        self._data[key] = (time.monotonic() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


class HttpClient:
    """Async JSON client shared by all sources.

    Retries on network errors and on 408/425/429/5xx with exponential backoff and jitter,
    honours ``Retry-After``, paces requests per host (``min_interval``), and caches
    successful responses for ``cache_ttl`` seconds.
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        cache_ttl: float = 300.0,
        cache_size: int = 256,
        min_interval: dict[str, float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the client.

        Args:
            timeout: Total timeout per request, in seconds.
            max_retries: Retries after the first attempt for retryable failures.
            backoff_base: Base delay in seconds; attempt n waits ``base * 2**n`` (+ jitter).
            cache_ttl: Seconds a successful response stays cached (0 disables the cache).
            cache_size: Maximum number of cached responses.
            min_interval: Minimum seconds between two requests to the same host.
            transport: Optional httpx transport (tests).
        """
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._cache = _TTLCache(cache_ttl, cache_size)
        self._min_interval = min_interval or {}
        self._last_call: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_json(self, url: str, params: dict[str, Any] | None = None, *, source: str) -> Any:
        """GET ``url`` and decode JSON.

        Args:
            url: Absolute URL.
            params: Query parameters (``None`` values are dropped).
            source: Source name used in errors and logs.

        Returns:
            The decoded JSON body.

        Raises:
            NotFoundError: On HTTP 404.
            UpstreamError: On any other failure after retries.
        """
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        return await self._request("GET", url, source=source, params=clean)

    async def post_json(self, url: str, body: dict[str, Any], *, source: str) -> Any:
        """POST a JSON body to ``url`` and decode the JSON response.

        Args:
            url: Absolute URL.
            body: JSON-serialisable request body.
            source: Source name used in errors and logs.

        Returns:
            The decoded JSON body.

        Raises:
            NotFoundError: On HTTP 404.
            UpstreamError: On any other failure after retries.
        """
        return await self._request("POST", url, source=source, json_body=body)

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def _pace(self, host: str) -> None:
        interval = self._min_interval.get(host, 0.0)
        if interval <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last_call.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call[host] = time.monotonic()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        source: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        key = json.dumps([method, url, params, json_body], sort_keys=True, default=str)
        cached = self._cache.get(key)
        if cached is not None:
            logger.debug("cache hit", extra={"source": source, "url": url})
            return cached

        host = urlsplit(url).netloc
        last_error: UpstreamError | None = None
        for attempt in range(self._max_retries + 1):
            await self._pace(host)
            started = time.monotonic()
            try:
                resp = await self._client.request(method, url, params=params, json=json_body)
            except httpx.TimeoutException:
                last_error = UpstreamError(source, "the upstream API timed out")
            except httpx.TransportError as exc:
                last_error = UpstreamError(source, f"network error ({type(exc).__name__})")
            else:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                logger.info(
                    "upstream call",
                    extra={
                        "source": source,
                        "method": method,
                        "url": url,
                        "status": resp.status_code,
                        "elapsed_ms": elapsed_ms,
                        "attempt": attempt,
                    },
                )
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise UpstreamError(source, "invalid JSON in response", 200) from exc
                    self._cache.set(key, data)
                    return data
                detail = _error_detail(resp)
                if resp.status_code == 404:
                    raise NotFoundError(source, f"not found: {detail}", 404)
                last_error = UpstreamError(
                    source, f"HTTP {resp.status_code}: {detail}", resp.status_code
                )
                if resp.status_code not in RETRYABLE_STATUS:
                    raise last_error
                retry_after = _retry_after(resp)
                if retry_after is not None and attempt < self._max_retries:
                    await asyncio.sleep(min(retry_after, 30.0))
                    continue
            if attempt < self._max_retries:
                delay = self._backoff_base * (2**attempt) * (1 + random.random() / 2)
                logger.warning(
                    "retrying upstream call",
                    extra={"source": source, "url": url, "attempt": attempt, "delay_s": delay},
                )
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error


def _error_detail(resp: httpx.Response) -> str:
    """Extract a short error message from an upstream error response."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    if isinstance(body, dict):
        for key in ("message", "error", "detail", "title"):
            if body.get(key):
                return str(body[key])[:300]
    return json.dumps(body, ensure_ascii=False)[:300]


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
