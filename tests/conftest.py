"""Shared pytest fixtures."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from marches_publics_mcp.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


def _load(source: str, name: str) -> Any:
    path = FIXTURES / source / (name if name.endswith(".json") else f"{name}.json")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def fixture_loader() -> Callable[[str, str], Any]:
    """Return ``load(source, name)`` reading ``tests/fixtures/<source>/<name>.json``."""
    return _load


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    """An HttpClient with no cache, no pacing and fast retries (mock it with respx)."""
    client = HttpClient(cache_ttl=0, max_retries=2, backoff_base=0.0)
    yield client
    await client.aclose()
