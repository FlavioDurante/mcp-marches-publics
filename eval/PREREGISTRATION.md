# Pre-registration: evaluation of mcp-marches-publics

Written before any run of the agent mode, on 2026-11-20. It fixes what is measured and how,
so that the result cannot be re-interpreted afterwards. Changing anything below after a run
requires a dated amendment at the bottom of this file, and earlier runs stay reported under
the old protocol.

## Question under test

Can an LLM agent, given only the 7 MCP tools of this server, answer realistic public
procurement watch questions (BOAMP, DECP, TED) correctly and cheaply?

## What the agent under test receives (and nothing else)

1. The system prompt below (`{today}` = the date of the run, passed with `--today`).
2. The tool list exactly as the MCP server exposes it through `list_tools` (name, description,
   input schema), converted mechanically to the provider format. No extra hint, no example.
3. One question from `eval/questions.yaml` (the `question` field only), as the user message.
4. Tool results exactly as the server returns them (text content, capped at 30000 characters).

System prompt:

~~~~text
You are a procurement-watch assistant for French and European public tenders. Answer the user's question using the tools provided. Never invent figures, names or identifiers: if the data does not contain the answer, say so. Today's date is {today}.
You may make at most 12 tool calls. When you are done, finish your message with exactly one JSON block of the form:
```json
{"answer": <value>}
```
where <value> is a number for a count or an amount (plain number, no unit), a string for a single identifier or fact, a list of strings for several identifiers, or an object keyed by the names given in the question for composite questions. If the honest answer is that no data exists, answer 0 for a count, or a string saying so.
~~~~

(The runner checks with `--check` that this text matches `SYSTEM_PROMPT` in `run_eval.py`.
The fenced `json` block inside is part of the literal prompt.)

## What the agent does NOT receive

Expected answers, tolerances, `gold_calls`, `extract`, `verification`, `note`, the tool
argument values of the gold calls, the other questions, any previous run, and the source code
of the server. One question per conversation (no memory between questions).

## Ground truth

Computed on 2026-11-20 by querying the official upstream APIs directly (ODSQL on BOAMP and
DECP, TED expert query, recherche-entreprises for names), independently of the project code.
The exact request is stored in each item (`verification`). Closed past periods are used when
possible; "open now" questions use `count_range` and an `as_of` date. No natural-person name
appears in any question or answer.

## Metrics

| Metric | Definition |
|---|---|
| Task success | Per item, by `answer_type`: `exact` (normalised string equality), `set` (same elements, order ignored), `number_tolerance` (absolute or % tolerance; composite answers: every key), `contains` (all expected facts present in the answer, accents and case ignored), `count_range` (inside [low, high]). Reported overall and per answer type and per source. |
| Tool calls per task | Mean and distribution of calls made before the final answer (cap 12). |
| Invalid-call rate | Calls rejected by the server (schema validation error or tool error) / all calls. |
| Tokens | Input and output tokens summed over the loop, per task and total. |
| Latency | Wall-clock seconds per task (includes upstream API time and model time). |

A missing or unparsable JSON answer block counts as a failure (except `contains`, where the
free text is searched too).

## Baseline: oracle runner

`--mode oracle` replays the `gold_calls` deterministically (no model) against the same
in-process server and extracts the answer with the item's `extract` path. It is an upper bound
that shows the tools CAN answer each question. Oracle failures are server or ground-truth
problems, not model problems, and are fixed or documented before any agent run. Live data
drifts: the oracle is re-run right before an agent run, and items whose oracle fails are
excluded from the agent score (and listed).

## Reporting rule

A negative result (low success, many invalid calls) is stated "under these conditions only":
this model, this system prompt, these 24 questions, these tool descriptions, this date. It is
not a statement about the model in general, nor about the tools in general. A positive result
is bounded the same way (n = 24, one run per model unless stated, no confidence claim beyond
the counts). Per-item results are published with the summary.

## Amendments

(none)
