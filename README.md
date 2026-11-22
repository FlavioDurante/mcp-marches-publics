# mcp-marches-publics

[![Licence: MIT](https://img.shields.io/badge/licence-MIT-lightgrey.svg)](LICENSE) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-2f7f84.svg) ![Tests: 225 offline](https://img.shields.io/badge/tests-225%20offline-2f7f84.svg) ![Eval: oracle 24/24](https://img.shields.io/badge/eval-oracle%2024%2F24-2f7f84.svg)

**An AI assistant that does your public-procurement watch for you: it finds the tenders that
concern you, tells you who usually wins them and at what price, in France and across Europe,
from official sources only.**

Plug this MCP server into Claude, Cursor or any MCP client and ask in plain language: open
tenders (BOAMP, TED), awarded contracts (DECP), who buys and who wins what.

No API key. Official sources only. Compact, paginated outputs designed for LLM context windows.
Every claim about quality below is backed by a test suite and a published evaluation set.

```text
You:    Which companies won the most cleaning contracts (CPV 9091) in the Rhône in 2025?
Claude: [calls rank_decp_parties(by="supplier", cpv_prefix="9091", department="69",
         notified_from="2025-01-01", notified_to="2025-12-31", top=3)]
        Out of 115 contracts published in DECP for that filter:
        1. SOC SEGUIGNE ET RUIZ — 10 contracts, 2.6 M€
        2. CONCEPT 3P — 8 contracts, 0.7 M€
        3. SAMSIC II — 7 contracts, 19.4 M€ (framework agreements: maximum amounts)
        Caveat: DECP coverage is incomplete, so this is a lower bound.
```

The output above is a real tool result (2026-11-20, live API), not a mock-up.

## Why

Public buyers in France publish their tenders in BOAMP, their awarded contracts in the DECP
(*données essentielles de la commande publique*), and above EU thresholds in TED. The data is
open but spread across three APIs with three query languages (two Opendatasoft datasets with
ODSQL, TED's expert query syntax), no company names in DECP, and CPV codes that each source
matches differently. This server hides that behind 8 typed tools an agent can use reliably.

## Tools

| Tool | Source | What it answers |
|---|---|---|
| `search_boamp_notices` | BOAMP | French notices by keywords, CPV (exact code or prefix), department / region / department name, publication dates, open-only, tender vs award, works/services/supplies, buyer |
| `get_boamp_notice` | BOAMP | One notice, compact: description, CPV, estimated value, lots, award criteria, winners |
| `search_decp_awards` | DECP | Awarded contracts by keywords, CPV prefix, buyer or supplier SIRET, department, dates, amount range, procedure; buyer and supplier names resolved |
| `rank_decp_parties` | DECP | Top buyers or suppliers by number of contracts or total amount, aggregated upstream, with explicit caveats |
| `search_ted_notices` | TED | EU notices by keywords, CPV (hierarchical), country of performance, dates, open-only, notice type |
| `get_ted_notice` | TED | One TED notice: description, estimated value, procedure, winners, award value, PDF link |
| `lookup_company` | Recherche d'entreprises | SIRET / SIREN to company name |
| `search_cpv_codes` | CPV 2008 | Find CPV codes from words (FR or EN), e.g. "nettoyage" |

All tools are read-only (`readOnlyHint`, `idempotentHint`, `openWorldHint`). Resources:
`marches://regions` (region to departments), `marches://sources` (sources, licences, freshness).
Prompt: `veille_marches(cpv, region)`, a ready-made monitoring workflow.

Design choices, following Anthropic's
[Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
and [MCP tool-description research](https://arxiv.org/abs/2602.14878):

- **Few, task-shaped tools** rather than one tool per endpoint; each description says when to use
  it and when to use its sibling instead.
- **Strict input schemas** (pydantic): CPV and department patterns, ISO dates, bounded limits.
  Invalid calls fail fast with a message that tells the model how to fix the call.
- **Compact structured output**: titles and descriptions truncated, no raw upstream blobs, a
  `page` block with `total` and `next_offset`, and a `hint` when a search returns nothing.
- **Honest aggregates**: rankings carry their caveats (framework-agreement maxima, first
  co-contractor only, incomplete coverage).

### Conventions

- Dates are ISO (`YYYY-MM-DD`). CPV codes are digits; an optional check digit (`90910000-9`) is ignored.
- Lists are paginated: pass the `next_offset` of a result as `offset` (TED: `next_page` as `page`). Default limit 10, maximum 50.
- DECP carries only SIRET identifiers; names are resolved through the company directory and may be `null`.
- Use BOAMP for French questions (fresher, filters by department), TED for EU-wide or foreign notices; DECP lists awarded contracts only, never open tenders.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

### Claude Code

```bash
claude mcp add marches-publics -- uvx mcp-marches-publics
```

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "marches-publics": { "command": "uvx", "args": ["mcp-marches-publics"] }
  }
}
```

### Cursor

`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "marches-publics": { "command": "uvx", "args": ["mcp-marches-publics"] }
  }
}
```

Until the package is on PyPI, replace `uvx mcp-marches-publics` with
`uvx --from git+https://github.com/FlavioDurante/mcp-marches-publics mcp-marches-publics`, or with a local checkout:
`uv run --directory /path/to/mcp-marches-publics mcp-marches-publics`.

### Streamable HTTP

```bash
mcp-marches-publics --transport streamable-http --host 127.0.0.1 --port 8000
# endpoint: http://127.0.0.1:8000/mcp
```

A `Dockerfile` is provided for the HTTP transport. The server has no authentication of its own:
bind it to localhost or put it behind an authenticated reverse proxy, never expose it directly.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MCP_MP_LOG_LEVEL` | `INFO` | Log level (JSON lines on stderr; stdout is the protocol) |
| `MCP_MP_BOAMP_URL` | BOAMP Opendatasoft records endpoint | Override (mirror, tests) |
| `MCP_MP_DECP_URL` | DECP Opendatasoft records endpoint | Override |
| `MCP_MP_TED_URL` | `https://api.ted.europa.eu/v3/notices/search` | Override |
| `MCP_MP_ENTREPRISES_URL` | `https://recherche-entreprises.api.gouv.fr/search` | Override |

No variable holds a secret: none of the sources requires a key.

## Sources, licences and limits

| Source | API | Licence | Rate limit (as documented or observed) |
|---|---|---|---|
| BOAMP (DILA) | [Opendatasoft Explore v2.1](https://boamp-datadila.opendatasoft.com/explore/dataset/boamp/) | Licence Ouverte / Etalab | Header `X-RateLimit-Limit: 2000000` per day on the domain; 100 records per page, 10,000 records reachable per query |
| DECP, format of the *arrêté du 22/12/2022* | [data.economie.gouv.fr `decp-2022-marches-valides`](https://data.economie.gouv.fr/explore/dataset/decp-2022-marches-valides/) | Licence Ouverte v2.0 (Etalab) | Header `X-RateLimit-Limit: 50000` per day on the domain (40x lower than BOAMP); same 100 / 10,000 windows |
| TED (EU Publications Office) | [TED API v3 search](https://docs.ted.europa.eu/api/latest/search.html), anonymous | Free reuse, commercial or not, under [Commission Decision 2011/833/EU](https://ted.europa.eu/en/legal-notice) | [Fair usage](https://ted.europa.eu/en/simap/developers-corner-for-reusers): 700 requests per minute per IP; 15,000 results reachable per query |
| Recherche d'entreprises (DINUM) | [recherche-entreprises.api.gouv.fr](https://recherche-entreprises.api.gouv.fr/) | Licence Ouverte | [7 calls per second](https://www.data.gouv.fr/dataservices/api-recherche-dentreprises/), may be lowered under load; the client paces itself |
| CPV 2008 nomenclature | see `src/marches_publics_mcp/data/README.md` | EU reuse policy | bundled, offline |

The client retries on network errors, 429 and 5xx with exponential backoff and jitter, honours
`Retry-After`, paces per host, and caches successful responses for 5 minutes.

Known limits, stated plainly:

- **BOAMP has no CPV field.** CPV codes live inside the notice payload; an 8-digit code matches
  that exact code, a 2-7 digit prefix is expanded to the codes of the official nomenclature
  (up to 300 codes: "72" works, "45" asks for a longer prefix). TED matches CPV hierarchically.
- **BOAMP departments are diffusion departments**, not the place of performance: a notice is
  returned if any of its departments matches (about 15% of Occitanie tenders in October 2026
  also list departments outside the region). Corsica is stored as `20A` / `20B` upstream and
  mapped from `2A` / `2B`.
- **BOAMP data quality.** Some deadlines are typos (year 2924); `open_only` ignores deadlines
  more than two years ahead. Rectifications are separate notices (`status`, `linked_notices`); a tender cancelled by a
  later notice is flagged `cancelled: true`.
  Deadlines are given in Europe/Paris time.
- **DECP has no names.** Buyer and supplier names come from the company directory by SIRET, so a
  name can be missing. Amounts of framework agreements are maxima, not money spent, and a framework agreement
  and its subsequent contracts can both be counted in a total. Not every
  buyer publishes, so rankings are lower bounds. A more exhaustive consolidation exists
  ([decp.info / colibre.fr](https://colibre.fr), 277 MB Parquet) but has no query API; this
  server uses the official dataset that does.
- **TED counts by place of performance** (`countries`), which differs from the buyer's country
  for cross-border buyers.
- Live data drifts: numbers in this README and in the evaluation are dated.
- This is open data, not legal advice; the official notice remains the reference.

## Evaluation

`eval/` holds 24 realistic questions in French (8 BOAMP, 10 DECP, 5 TED, 1 cross-source), each with
a ground truth computed **directly against the official APIs, independently of this code**, the
upstream request used to verify it, and the date it was verified. The protocol was written before
any run (`eval/PREREGISTRATION.md`).

| Run | What it shows | Result |
|---|---|---|
| Oracle (gold tool calls replayed through the MCP server, live APIs, 2026-11-20) | The tools *can* answer every question | 24 / 24 |
| Agent (an LLM that only sees the tool list and the question) | Whether a model *does* find the answer | not run yet: it needs a paid model API |

```bash
python eval/run_eval.py --check
python eval/run_eval.py --mode oracle
python eval/run_eval.py --mode agent --provider anthropic --model <model-id>   # needs ANTHROPIC_API_KEY
```

See `eval/README.md` for metrics (success per answer type, tool calls, invalid-call rate, tokens,
latency) and limits (small n, drifting live data).

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest -q                    # unit + end-to-end MCP tests, no network (recorded fixtures)
pytest -q -m integration     # live API tests, opt-in
ruff check . && black --check .
```

Layout: `src/marches_publics_mcp/http.py` (shared async client), `sources/` (one client per API,
returning the models of `models.py`), `server.py` (MCP tools, resources, prompt), `geo.py`
(regions and departments), `cpv.py` (nomenclature). End-to-end tests drive the server through
the official MCP client, in memory and over stdio.

Prior art and what this server adds: `docs/STATE_OF_THE_ART.md`.

## Roadmap

- Agent-mode evaluation across several models, with pass^k over repeated runs.
- Alerts: persist a query and return only new notices since the last call.
- DECP concessions dataset; modification notices.
- Other national portals (PLACE, regional profiles) where an open API exists.
- Publication on PyPI and the MCP registry.

## Licence

MIT (code). Data belongs to its publishers under the licences listed above.
