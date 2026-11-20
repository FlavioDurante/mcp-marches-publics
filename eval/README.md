# Evaluation

Measures whether an LLM agent can answer procurement-watch questions with the 8 tools of this
server, and whether the tools can answer them at all (oracle baseline). Read
`PREREGISTRATION.md` first: it fixes the protocol and the metrics.

## Files

| File | Role |
|---|---|
| `questions.yaml` | 24 French questions (8 BOAMP, 8 DECP, 5 TED, 3 cross-source / no-result / trick) with ground truth, gold tool calls and the upstream request used to verify each one |
| `PREREGISTRATION.md` | Protocol written before any run |
| `run_eval.py` | Runner: `--check`, `--mode oracle`, `--mode agent` |
| `scoring.py`, `test_scoring.py` | Pure scoring functions and their tests |
| `results/` | Run outputs (gitignored) |

## Run

From the repository root, inside the project venv:

```bash
python eval/run_eval.py --check                  # schema, tool names, protocol consistency
python eval/run_eval.py --mode oracle            # replay gold calls against the live APIs
pytest -q eval/test_scoring.py -p no:cacheprovider -o addopts=""

# Agent mode: PAID model calls. Provider SDKs are not project dependencies.
pip install anthropic                            # or: pip install openai
export ANTHROPIC_API_KEY=...                     # never write keys in a file
python eval/run_eval.py --mode agent --provider anthropic --model <model-id>

export OPENAI_API_KEY=... OPENAI_BASE_URL=https://.../v1
python eval/run_eval.py --mode agent --provider openai-compatible --model <model-id>
```

Options: `--only boamp-01 ted-04` to restrict, `--out DIR`, `--today YYYY-MM-DD`. Missing keys or
SDK give a clear error and exit code 2. Oracle mode needs network access (the server calls the
real APIs); agent mode needs it too.

## Format notes

* `extract`: path read in the result of the last gold call (`page.total`,
  `entries[0].siret`, `entries[*].siret`, `awards[0].amount_eur`); `N:path` selects gold call N;
  a list gathers several paths; a dict gives named values for composite answers.
* Gold call arguments use the tool parameter names of `server.py` (same as the client
  keyword arguments in `CONTRATS.md`, plus `region` on `search_boamp_notices`).
* The ground-truth verification uses the upstream APIs directly, never the project clients.

## What is measured

Task success per answer type and source, tool calls per task, invalid-call rate, tokens and
latency. The oracle gives the upper bound: if the oracle fails an item, the tools (or the
ground truth) are at fault, not the model.

## Honest limits

* Small n (24 questions, one run per model): differences of one or two items are noise.
* Live data drifts: BOAMP/TED keep receiving notices, DECP is published late (a closed 2025
  period can still grow). Counts carry a tolerance and "open now" questions a range, with the
  `as_of` date; re-verify the ground truth when much time has passed.
* DECP amounts are contract ceilings for framework agreements; amount rankings are noisy.
* Some counts depend on a definition (CPV exact token on BOAMP vs hierarchical on TED;
  department = code or postal prefix on DECP). Items state the definition in `note`.
* The questions were written by the tool authors, which favours the tools.
* A negative result is valid only under these conditions (model, prompt, questions, date).

## Contamination

`questions.yaml` is public, answers included. A model trained on this repository after
publication could have seen them: report the model's training cutoff with any agent result,
and refresh the dated questions (new periods, new notice ids) before comparing models.
