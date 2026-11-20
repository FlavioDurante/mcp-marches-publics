# State of the art: MCP servers for public procurement data (France BOAMP, DECP, EU TED)

Status: research note, 2026-11-20. Scope: open-source MCP servers and data tools covering French public procurement (BOAMP notices, DECP award data) and EU procurement (TED). Method: GitHub metadata via `gh api` (read-only), MCP registry API, awesome-mcp-servers README, PyPI metadata, primary docs, and arXiv papers (abstracts, plus PDF pages for two papers). Stars and dates are read from the GitHub API on the day of the check. Anything not checked is marked **not verified**.

## Summary

- No open-source MCP server found that covers BOAMP, DECP and TED together, with tests and a published evaluation.
- Closest existing work: `tenderapi-mcp` (BOAMP + TED, but depends on a keyed third-party REST API), `AgentIA1984-cmd/mcp-marches-publics` (BOAMP + DECP + company lookup, no TED, no tests in CI), and the official `datagouv-mcp` (general data.gouv.fr, not procurement-specific).
- No procurement-specific task benchmark with ground truth was found. General MCP benchmarks exist (MCP-Bench, MCP-Atlas, MCPToolBench++).
- Name collision: `AgentIA1984-cmd/mcp-marches-publics` has the same repository name as this project.

## Part A: existing work

### A.1 Projects and servers

| Project | URL | Stars (gh api) | Last push (gh api) | Coverage | Transport | Tests | Eval | Notes |
|---|---|---|---|---|---|---|---|---|
| AgentIA1984-cmd/mcp-marches-publics | https://github.com/AgentIA1984-cmd/mcp-marches-publics | 0 | 2026-07-06 (one commit, "Initial commit") | BOAMP search and notice detail, DECP awarded contracts, Recherche d'entreprises lookup (4 tools) | stdio and streamable-http (README) | `test_local.py` calls live APIs; no pytest suite or CI found | no | Licence: none found (`license` null). Sources: BOAMP and DECP via Opendatasoft, company lookup via recherche-entreprises |
| datagouv/datagouv-mcp (official) | https://github.com/datagouv/datagouv-mcp | 1637 | 2026-11-20 | data.gouv.fr platform in general (datasets, dataservices, metrics). No BOAMP/DECP-specific tool found in README | Remote `https://mcp.data.gouv.fr/mcp` and local Python | yes (`tests/` directory, pytest) | no eval found | MIT. Not procurement-specific |
| IDNSIDNS/tenderapi-mcp (PyPI `tenderapi-mcp` 0.10.2, MIT) | https://github.com/IDNSIDNS/tenderapi-mcp | 0 | 2026-11-15 | BOAMP and TED via third-party REST API tenderapi.fr: `search_tenders`, `get_tender`, `search_awards`, `get_award` | Python, stdio (`mcp.run()`) | no tests directory found | no | Requires `TENDERAPI_KEY` (README: free key). Closest to "FR + EU in one surface" |
| pipeworx-io/mcp-france-boamp | https://github.com/pipeworx-io/mcp-france-boamp | 0 | 2026-11-09 | BOAMP: `france_search_tenders`, `france_get_notice` | npm wrapper around hosted gateway `gateway.pipeworx.io` | no | no | MIT. README: "unofficial", hosted service |
| pipeworx-io/mcp-ted-eu | https://github.com/pipeworx-io/mcp-ted-eu | 1 | 2026-11-09 | TED: `search_notices`, `get_notice`, `ted_search_awards`, `ted_supplier_history`, `ted_buyer_profile`, `cpv_lookup` | Same hosted gateway (`gateway.pipeworx.io/ted-eu/mcp`) | no | no | MIT. Hosted dependency |
| lexsocket/mcp-tenders | https://github.com/lexsocket/mcp-tenders | 0 | 2026-03-19 | EU tenders (per description); tool list not verified | stdio proxy to hosted `mcp.lexsocket.ai` | no | no | Free tier 25 requests/hour (README) |
| stefw/boamp-server | https://github.com/stefw/boamp-server | 3 | 2025-03-06 | BOAMP: `get_public_markets`, `get_market_details` | stdio (TypeScript) | no | no | No licence field found |
| thomas-servais/mcp-recherche-entreprise | https://github.com/thomas-servais/mcp-recherche-entreprise | 1 | 2025-07-20 | Company search (recherche-entreprises) | TypeScript | no | no | Not procurement-specific |
| atlasprzetargow/mcp-server | https://github.com/atlasprzetargow/mcp-server | not verified | not verified | Polish BZP and TED (awesome-mcp-servers description) | not verified | not verified | not verified | Poland-focused, listed in awesome-mcp-servers |
| fr.publicoffre/marches-publics (MCP registry, v3.31.0 latest) | https://www.publicoffre.fr/ (no repo in registry) | not verified | not verified | "open tenders, who wins contracts, company references, local finances" | not verified | not verified | not verified | Source code not verified; likely a commercial service |
| fr.tenderwatch/marches-publics (MCP registry, 1.0.0) | https://tenderwatch.fr (no repo in registry) | not verified | not verified | French open tenders, lexicon | not verified | not verified | not verified | Source code not verified |

### A.2 Data tools (not MCP servers)

| Project | URL | Stars | Last push | Relevance |
|---|---|---|---|---|
| ColinMaudry/decp-processing | https://github.com/ColinMaudry/decp-processing | 11 | 2026-11-17 (last commit 2026-11-14) | Cleans and publishes DECP and BOAMP-derived data (parquet, CSV, SQLite) on data.gouv.fr, daily. Has `src/tasks/boamp/` and a CI test workflow. Best reference for DECP field semantics. Licence NOASSERTION. Not an MCP server |
| ColinMaudry/boamp-rdf | https://github.com/ColinMaudry/boamp-rdf | 2 | 2017-10-24 | BOAMP to RDF tooling, old. GPL-3.0 |
| JMousqueton/boamp-extractor | https://github.com/JMousqueton/boamp-extractor | 4 | 2023-07-10, archived | BOAMP extraction script, archived |
| JMousqueton/boamp-alert | https://github.com/JMousqueton/boamp-alert | 2 | 2024-05-23 | Teams and Mattermost alert script for BOAMP notices |

Hosted, paid TED scrapers exist on Apify (not verified in detail, not open source).

### A.3 Official sources (for the design, from primary docs)

- BOAMP dataset on Opendatasoft (`boamp-datadila.opendatasoft.com`): licence Etalab 2.0; record count and update date not shown on the info page; API present (https://boamp-datadila.opendatasoft.com/explore/dataset/boamp/information/).
- DECP dataset `decp-v3-marches-valides` on data.economie.gouv.fr: Licence Ouverte 2.0; record count and update date not shown (https://data.economie.gouv.fr/explore/dataset/decp-v3-marches-valides/information/).
- TED Search API: `POST /v3/notices/search`, expert query syntax, no authentication per the docs page. **Conflict:** a search snippet said a key is needed from the TED Developer Portal. Not resolved. Pagination cap of 15,000 notices in page mode comes from a secondary workshop Q&A, not verified against the Swagger spec (https://docs.ted.europa.eu/api/latest/search.html).
- recherche-entreprises.api.gouv.fr: the docs page fetched contained only a title. Key, rate limit and licence **not verified**.

### A.4 Gap analysis

What exists:
- BOAMP only: stefw, pipeworx, and a BOAMP part of tenderapi-mcp.
- TED only: pipeworx-io/mcp-ted-eu, lexsocket (hosted), atlasprzetargow (Poland).
- BOAMP + DECP: AgentIA1984 (no TED).
- BOAMP + TED: tenderapi-mcp (third-party API, key required).

What was not found in any checked source:
- A self-hostable, keyless server covering BOAMP, DECP and TED together.
- Tests plus a published evaluation for any procurement MCP server.
- A procurement-specific benchmark with verified ground truth.
- Compact, paginated output design documented in a procurement server.

Caveats: the search covered GitHub repo search, the MCP registry, awesome-mcp-servers and PyPI. Private repos, Glama and Smithery listings, and npm search were not checked in full.

### A.5 What this server adds (checked against the build, 2026-11-20)

1. **Three sources, one keyless surface.** BOAMP (notices), DECP (awarded contracts) and TED (EU
   notices and results) behind 8 typed tools. The closest open project
   (AgentIA1984-cmd/mcp-marches-publics) covers BOAMP + DECP + company lookup, without TED.
2. **Current DECP data.** That project queries `decp-v3-marches-valides`, whose most recent
   notification date is 2024-02-08 (checked live: `max(datenotification)`). This server uses
   `decp-2022-marches-valides` (arrêté du 22/12/2022 format), up to date to within days.
3. **Award analytics with names.** `rank_decp_parties` aggregates upstream (count or amount) and
   joins SIRET to company names through recherche-entreprises, with explicit caveats
   (framework-agreement maxima, first co-contractor only, incomplete coverage).
4. **CPV handled the way users think.** BOAMP has no CPV field; the server bundles the official
   CPV 2008 nomenclature (9,454 codes) to expand a prefix ("72" = all IT services) and to find
   codes from words (`search_cpv_codes`). Measured: open IT tenders in Occitanie, exact code
   72000000 → 0 notices, prefix 72 → 16.
5. **A published evaluation.** 24 questions with ground truth computed directly against the
   APIs, a pre-registered protocol, and an oracle run (24/24) proving the tools can answer
   them. The agent run (which model finds the answer) is prepared but not run yet.
6. **Engineering basics most servers in the table lack.** Recorded-fixture unit tests, MCP
   end-to-end tests in memory and over stdio, retries with backoff, pacing, caching, JSON logs.

## Part B: best practices

Research done with the local papers tool first (`papers search`, `papers get`; Semantic Scholar returned HTTP 429 and was skipped). Then Anthropic's engineering post. PDFs downloaded to the scratchpad for the two papers read in full.

### B.1 Design rules for our tools

1. **Consolidate and avoid "list everything".** Anthropic: "More tools don't always lead to better outcomes"; build tools for high-impact workflows that match evaluation tasks, and prefer search tools returning only relevant results. Source: https://www.anthropic.com/engineering/writing-tools-for-agents (Sep 11, 2025).
2. **Concise and detailed response modes.** Anthropic recommends a `response_format` enum (concise, detailed), natural-language names instead of UUIDs or MIME types, and exposing IDs only when a follow-up call needs them. Same source.
3. **Pagination and truncation with a hint.** Default limits on any tool that can return large output; when truncating, say how to narrow the request. Anthropic notes Claude Code caps tool responses at 25,000 tokens by default. Same source.
4. **Descriptions and schemas.** Unambiguous parameter names (`user_id`, not `user`), strict input and output schemas, and annotations for open-world or destructive tools. Anthropic cites SWE-bench gains from description edits. Same source.
5. **Tool descriptions are an empirical quality issue.** Hasan et al. (arXiv:2602.14878) scanned 856 tool descriptions from 103 MCP servers: 97.1% contain at least one "smell", 56% fail to state their purpose clearly. Augmenting all six components improved median task success by 5.85 percentage points, but increased execution steps by 67.46% and regressed 16.67% of cases. Compact variants often preserved behavioural reliability while cutting tokens. Rule: measure each description change on the eval; do not assume it helps. PDF read: pages 1 to 3 (abstract and introduction).
6. **Evaluate on realistic, multi-step tasks with verifiable outcomes.** MCP-Bench (arXiv:2508.20453): 28 live MCP servers, 250 tools, 104 tasks, 20 LLMs; tasks are rewritten to a fuzzy, instruction-minimal form so the agent must pick tools itself; two-tier scoring (rule-based checks plus LLM-as-judge), with prompt shuffling and score averaging for stability. PDF read: pages 1 to 3.
7. **Score claims, not trajectories.** MCP-Atlas (arXiv:2602.00933): 36 servers, 220 tools, 1,000 tasks (500 public, 500 private); answers are scored against atomic factual claims grounded in tool outputs, pass rate at 0.75 claim coverage, so valid alternative tool-call paths still earn credit. Best fit for our ground truth built from the open data. Abstract only.
8. **Report reliability, not only success.** τ-bench (arXiv:2406.12045): outcomes checked by comparing final database state to an annotated goal state; pass^k metric; gpt-4o succeeds on fewer than 50% of tasks; pass^8 below 25% in the retail domain. Implication: run each task several times and report pass^k alongside pass@1. Abstract via arXiv page.
9. **Track operational metrics.** Anthropic's list: accuracy (with a held-out set), tool-call count, runtime, total tokens, and tool error rate; frequent invalid-parameter errors point to unclear descriptions. Same source.
10. **Benchmarks and tool-use context.** ToolLLM (arXiv:2307.16789, 16,000+ APIs; 1,974 citations per OpenAlex) is the reference for large API sets. MCPToolBench++ (arXiv:2508.07575) reports over 4k MCP servers in 40+ categories but no task counts in the abstract. Per-tool numbers for these beyond the abstract: **not verified**.

### B.2 Eval design for this project (built: see `eval/`)

- Task set: questions answerable from the BOAMP, DECP and TED data alone, each with a ground truth derived from the raw records (not from a model answer).
- Scoring: rule-based comparison of claims (MCP-Atlas style), with exact-match on identifiers (notice ID, SIRET, amounts).
- Metrics: task success, pass^k over repeated runs, tool-call count, total tokens, tool error rate.
- Held-out split from the start, per Anthropic.
- Ablations on tool descriptions and response formats, measured separately (Hasan et al. show gains can be negative).

### B.3 Papers and sources read

- Hasan et al., "MCP Tool Descriptions Are Smelly!", arXiv:2602.14878 (https://arxiv.org/abs/2602.14878).
- Wang et al., "MCP-Bench", arXiv:2508.20453 (https://arxiv.org/abs/2508.20453).
- Bandi et al., "MCP-Atlas", arXiv:2602.00933 (https://arxiv.org/abs/2602.00933).
- Fan et al., "MCPToolBench++", arXiv:2508.07575 (https://arxiv.org/abs/2508.07575).
- Yao et al., "τ-bench", arXiv:2406.12045 (https://arxiv.org/abs/2406.12045).
- Qin et al., "ToolLLM", arXiv:2307.16789 (https://arxiv.org/abs/2307.16789).
- Anthropic, "Writing effective tools for AI agents", https://www.anthropic.com/engineering/writing-tools-for-agents.
- Also surfaced, not read: BFCL and EASYTOOL (arXiv:2401.06201, no figures in abstract).

## Sources

- https://github.com/AgentIA1984-cmd/mcp-marches-publics
- https://github.com/datagouv/datagouv-mcp
- https://github.com/IDNSIDNS/tenderapi-mcp
- https://pypi.org/pypi/tenderapi-mcp/json
- https://github.com/pipeworx-io/mcp-france-boamp
- https://github.com/pipeworx-io/mcp-ted-eu
- https://github.com/lexsocket/mcp-tenders
- https://github.com/stefw/boamp-server
- https://github.com/thomas-servais/mcp-recherche-entreprise
- https://github.com/ColinMaudry/decp-processing
- https://github.com/ColinMaudry/boamp-rdf
- https://github.com/JMousqueton/boamp-extractor
- https://registry.modelcontextprotocol.io/v0/servers?search=boamp
- https://registry.modelcontextprotocol.io/v0/servers?search=ted
- https://registry.modelcontextprotocol.io/v0/servers?search=marches
- https://github.com/punkpeye/awesome-mcp-servers
- https://boamp-datadila.opendatasoft.com/explore/dataset/boamp/information/
- https://data.economie.gouv.fr/explore/dataset/decp-v3-marches-valides/information/
- https://docs.ted.europa.eu/api/latest/search.html
- https://recherche-entreprises.api.gouv.fr/docs/ (title only)
- https://www.anthropic.com/engineering/writing-tools-for-agents
- https://arxiv.org/abs/2602.14878, https://arxiv.org/abs/2508.20453, https://arxiv.org/abs/2602.00933, https://arxiv.org/abs/2508.07575, https://arxiv.org/abs/2406.12045, https://arxiv.org/abs/2307.16789, https://arxiv.org/abs/2401.06201
