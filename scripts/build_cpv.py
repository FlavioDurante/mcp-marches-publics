"""Build ``src/marches_publics_mcp/data/cpv_2008.json.gz`` from the EU Publications Office.

Source: EU Vocabularies CPV 2008 concept scheme (``http://data.europa.eu/cpv/cpv``), queried on
the official SPARQL endpoint. Run with network access: ``python scripts/build_cpv.py``.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from datetime import date
from pathlib import Path

import httpx

ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
OUT = Path(__file__).resolve().parent.parent / "src" / "marches_publics_mcp" / "data"
DIVISIONS = [f"{i:02d}" for i in range(3, 99)]
QUERY = """PREFIX skos:<http://www.w3.org/2004/02/skos/core#>
SELECT ?n ?lab (lang(?lab) AS ?l) WHERE {{
  ?c skos:inScheme <http://data.europa.eu/cpv/cpv> ; skos:notation ?n ; skos:prefLabel ?lab .
  FILTER(STRSTARTS(?n, "{division}") && lang(?lab) IN ("fr","en"))
}}"""


def fetch_rows() -> list[dict[str, dict[str, str]]]:
    """Fetch every (code, language, label) binding, one query per 2-digit division."""
    rows: list[dict[str, dict[str, str]]] = []
    with httpx.Client(timeout=300) as http:
        for division in DIVISIONS:
            resp = http.post(
                ENDPOINT,
                data={"query": QUERY.format(division=division)},
                headers={"Accept": "application/sparql-results+json"},
            )
            resp.raise_for_status()
            rows.extend(resp.json()["results"]["bindings"])
    return rows


def main() -> None:
    """Write the compact ``[code, label_fr, label_en]`` list."""
    codes: dict[str, dict[str, str]] = {}
    for row in fetch_rows():
        code = re.sub(r"-\d$", "", row["n"]["value"].strip())
        if re.fullmatch(r"\d{8}", code):
            codes.setdefault(code, {})[row["l"]["value"]] = row["lab"]["value"]
    data = [[c, v.get("fr", ""), v.get("en", "")] for c, v in sorted(codes.items())]
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"retrieved": date.today().isoformat(), "codes": data}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # mtime=0 keeps the archive reproducible.
    (OUT / "cpv_2008.json.gz").write_bytes(gzip.compress(raw, 9, mtime=0))
    sys.stderr.write(f"{len(data)} codes written\n")


if __name__ == "__main__":
    main()
