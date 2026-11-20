"""Company-name resolver backed by recherche-entreprises.api.gouv.fr (free, no API key).

Privacy: only the company display name (``nom_complet``) is read from the response. Officers
(``dirigeants``) and every other field are ignored and never stored or returned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from marches_publics_mcp.http import HttpClient, UpstreamError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://recherche-entreprises.api.gouv.fr"
MAX_CONCURRENCY = 5
_DUPLICATED_NAME = re.compile(r"^(?P<a>.+?) \((?P<b>.+)\)$")


def _clean_name(name: str) -> str:
    """Collapse whitespace and turn ``"ACME (ACME)"`` into ``"ACME"``."""
    clean = " ".join(name.split())
    match = _DUPLICATED_NAME.match(clean)
    if match and match.group("a").casefold() == match.group("b").casefold():
        return match.group("a")
    return clean


class CompanyResolver:
    """Resolve SIRET (14 digits) or SIREN (9 digits) to a company display name."""

    def __init__(self, http: HttpClient, base_url: str | None = None) -> None:
        """Create the resolver.

        Args:
            http: Shared HTTP client.
            base_url: API root; defaults to ``MCP_MP_ENTREPRISES_URL`` or the public API.
        """
        self._http = http
        self._base = (base_url or os.getenv("MCP_MP_ENTREPRISES_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self._memo: dict[str, str | None] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def name_for(self, siret: str) -> str | None:
        """Return the display name of a company, or ``None`` if unknown or on any failure.

        Args:
            siret: A SIRET (14 digits) or SIREN (9 digits); spaces are ignored.

        Returns:
            The company name (``nom_complet``), never anything else. Never raises.
        """
        key = "".join(str(siret).split())
        if not key.isdigit() or len(key) not in (9, 14):
            return None
        if key in self._memo:
            return self._memo[key]
        try:
            async with self._semaphore:
                data = await self._http.get_json(
                    f"{self._base}/search",
                    {"q": key, "page": 1, "per_page": 1},
                    source="entreprises",
                )
        except UpstreamError as exc:
            logger.warning(
                "company lookup failed", extra={"source": "entreprises", "err": str(exc)}
            )
            return None
        except Exception:
            logger.warning("company lookup crashed", extra={"source": "entreprises"}, exc_info=True)
            return None
        name = self._extract_name(data, key)
        self._memo[key] = name
        return name

    async def names_for(self, sirets: list[str]) -> dict[str, str | None]:
        """Resolve several identifiers concurrently (at most 5 in flight).

        Args:
            sirets: SIRET or SIREN values; duplicates are looked up once.

        Returns:
            Mapping of each input value to its name or ``None``.
        """
        unique = list(dict.fromkeys(sirets))
        names = await asyncio.gather(*(self.name_for(s) for s in unique))
        return dict(zip(unique, names, strict=True))

    @staticmethod
    def _extract_name(data: object, key: str) -> str | None:
        """Pick ``nom_complet`` of the first result whose SIREN matches ``key``."""
        if not isinstance(data, dict):
            return None
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        first = results[0]
        if not isinstance(first, dict) or str(first.get("siren", ""))[:9] != key[:9]:
            return None
        name = first.get("nom_complet")
        return _clean_name(name) if isinstance(name, str) and name.strip() else None
