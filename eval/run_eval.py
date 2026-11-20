"""Evaluation runner for mcp-marches-publics.

Modes:
    --check   validate eval/questions.yaml (schema, tool names, PREREGISTRATION consistency).
    --mode oracle   replay the gold calls in-process against the MCP server and score them.
    --mode agent    let an LLM drive the MCP tools (needs API keys from the environment; paid).

Run from the repository root, e.g. ``.venv/bin/python eval/run_eval.py --check``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from scoring import ANSWER_TYPES, extract_path, parse_answer_block, score_answer, summarize

EVAL_DIR = Path(__file__).parent
DEFAULT_QUESTIONS = EVAL_DIR / "questions.yaml"
PREREGISTRATION = EVAL_DIR / "PREREGISTRATION.md"
RESULTS_DIR = EVAL_DIR / "results"

TOOL_NAMES = frozenset(
    {
        "search_boamp_notices",
        "get_boamp_notice",
        "search_decp_awards",
        "rank_decp_parties",
        "search_ted_notices",
        "get_ted_notice",
        "lookup_company",
    }
)
SOURCES = frozenset({"boamp", "decp", "ted"})
REQUIRED_KEYS = (
    "id",
    "question",
    "sources",
    "answer_type",
    "expected",
    "as_of",
    "gold_calls",
    "extract",
    "verification",
)
MAX_TOOL_CALLS = 12
MAX_TOOL_RESULT_CHARS = 30000

# Must appear verbatim in PREREGISTRATION.md (checked by --check). {today} is substituted.
SYSTEM_PROMPT = """\
You are a procurement-watch assistant for French and European public tenders. Answer the \
user's question using the tools provided. Never invent figures, names or identifiers: if the \
data does not contain the answer, say so. Today's date is {today}.
You may make at most 12 tool calls. When you are done, finish your message with exactly one \
JSON block of the form:
```json
{{"answer": <value>}}
```
where <value> is a number for a count or an amount (plain number, no unit), a string for a \
single identifier or fact, a list of strings for several identifiers, or an object keyed by \
the names given in the question for composite questions. If the honest answer is that no data \
exists, answer 0 for a count, or a string saying so."""


# ---------------------------------------------------------------- loading and validation
def load_items(path: Path) -> list[dict[str, Any]]:
    """Load the question items from a YAML file.

    Args:
        path: Path of the questions file.

    Returns:
        The list under the ``questions`` key.
    """
    with path.open(encoding="utf-8") as handle:
        return list(yaml.safe_load(handle)["questions"])


def validate_items(items: list[dict[str, Any]]) -> list[str]:
    """Check schema, tool names and extract/expected shapes of every item.

    Args:
        items: Items loaded from the questions file.

    Returns:
        A list of human-readable errors (empty when everything is valid).
    """
    errors: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item.get("id", "<no id>"))
        for key in REQUIRED_KEYS:
            if key not in item:
                errors.append(f"{name}: missing key {key!r}")
        if name in seen:
            errors.append(f"{name}: duplicate id")
        seen.add(name)
        atype = item.get("answer_type")
        if atype not in ANSWER_TYPES:
            errors.append(f"{name}: bad answer_type {atype!r}")
        if not set(item.get("sources", [])) <= SOURCES or not item.get("sources"):
            errors.append(f"{name}: sources must be a non-empty subset of {sorted(SOURCES)}")
        calls = item.get("gold_calls") or []
        if not calls:
            errors.append(f"{name}: no gold_calls")
        for call in calls:
            if call.get("tool") not in TOOL_NAMES:
                errors.append(f"{name}: unknown tool {call.get('tool')!r}")
            if not isinstance(call.get("args", {}), dict):
                errors.append(f"{name}: gold call args must be a mapping")
        expected = item.get("expected")
        if atype == "count_range" and not (
            isinstance(expected, list) and len(expected) == 2 and expected[0] <= expected[1]
        ):
            errors.append(f"{name}: count_range expects [low, high]")
        if atype == "number_tolerance" and not ("tolerance" in item or "tolerance_pct" in item):
            errors.append(f"{name}: number_tolerance needs tolerance or tolerance_pct")
        if atype in ("set", "contains") and not isinstance(expected, list):
            errors.append(f"{name}: {atype} expects a list")
        if isinstance(expected, dict) and not isinstance(item.get("extract"), dict):
            errors.append(f"{name}: dict expected needs a dict extract")
        if "@" in json.dumps(item, ensure_ascii=False):
            errors.append(f"{name}: contains '@' (possible e-mail / personal data)")
        try:
            datetime.strptime(str(item.get("as_of")), "%Y-%m-%d")
        except ValueError:
            errors.append(f"{name}: as_of must be YYYY-MM-DD")
    return errors


def check(path: Path) -> int:
    """Run the ``--check`` mode; print a report on stdout.

    Args:
        path: Questions file.

    Returns:
        Process exit code (0 when valid).
    """
    items = load_items(path)
    errors = validate_items(items)
    if PREREGISTRATION.exists():
        # Each prompt line must appear in the pre-registration file.
        text = PREREGISTRATION.read_text(encoding="utf-8")
        shown = SYSTEM_PROMPT.format(today="{today}")
        missing = [ln for ln in shown.splitlines() if ln.strip() and ln not in text]
        if missing:
            errors.append("PREREGISTRATION.md does not contain the system prompt verbatim")
    else:
        errors.append("PREREGISTRATION.md is missing")
    counts: dict[str, int] = {}
    for item in items:
        key = "+".join(item.get("sources", []))
        counts[key] = counts.get(key, 0) + 1
    print(f"{len(items)} questions; by sources: {counts}")
    for err in errors:
        print(f"ERROR {err}")
    print("OK" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


# ---------------------------------------------------------------- MCP plumbing
def tool_payload(result: Any) -> Any:
    """Return the JSON payload of an MCP tool result (structured content, else text)."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    text = result.content[0].text if getattr(result, "content", None) else ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def result_text(result: Any) -> str:
    """Return the text an LLM would see for a tool result (capped)."""
    if getattr(result, "content", None):
        text = "\n".join(getattr(part, "text", "") for part in result.content)
    else:
        text = json.dumps(tool_payload(result), ensure_ascii=False)
    return text[:MAX_TOOL_RESULT_CHARS]


def pick_extract(spec: Any, payloads: list[Any]) -> Any:
    """Apply an ``extract`` spec (str | list | dict) to the gold call payloads."""
    if isinstance(spec, dict):
        return {key: pick_extract(sub, payloads) for key, sub in spec.items()}
    if isinstance(spec, list):
        return [pick_extract(sub, payloads) for sub in spec]
    index, path = len(payloads) - 1, str(spec)
    head, sep, tail = path.partition(":")
    if sep and head.isdigit():
        index, path = int(head), tail
    return extract_path(payloads[index], path)


def flatten(value: Any) -> Any:
    """Flatten nested lists of scalars (``contains`` extractors may return lists of lists)."""
    if isinstance(value, list):
        out: list[Any] = []
        for part in value:
            flat = flatten(part)
            out.extend(flat if isinstance(flat, list) else [flat])
        return out
    return value


def score_item(item: dict[str, Any], actual: Any) -> tuple[bool, str]:
    """Score ``actual`` against an item with its answer type and tolerances."""
    outcome = score_answer(
        item["answer_type"],
        item["expected"],
        actual,
        tolerance=float(item.get("tolerance", 0.0)),
        tolerance_pct=float(item.get("tolerance_pct", 0.0)),
    )
    return outcome.passed, outcome.detail


def write_results(mode: str, label: str, rows: list[dict[str, Any]], out_dir: Path) -> Path:
    """Write per-item rows and the summary into ``out_dir`` (gitignored)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{mode}-{label}-{stamp}.json"
    payload = {
        "mode": mode,
        "label": label,
        "run_at": stamp,
        "summary": summarize(rows),
        "items": rows,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------- oracle mode
async def run_oracle(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replay the gold calls in-process and score the extracted answers."""
    from mcp import Client

    from marches_publics_mcp.server import build_server

    rows: list[dict[str, Any]] = []
    async with Client(build_server()) as client:
        for item in items:
            started = time.perf_counter()
            payloads: list[Any] = []
            invalid = 0
            error: str | None = None
            for call in item["gold_calls"]:
                try:
                    result = await client.call_tool(call["tool"], call.get("args", {}))
                except Exception as exc:  # protocol-level error (e.g. schema validation)
                    invalid += 1
                    error = f"{call['tool']}: {exc}"
                    break
                if getattr(result, "is_error", False):
                    invalid += 1
                    error = f"{call['tool']}: {result_text(result)[:300]}"
                    break
                payloads.append(tool_payload(result))
            actual: Any = None
            passed, detail = False, error or ""
            if error is None:
                try:
                    actual = flatten(pick_extract(item["extract"], payloads))
                    passed, detail = score_item(item, actual)
                except KeyError as exc:
                    detail = f"extractor failed: {exc}"
            rows.append(
                {
                    "id": item["id"],
                    "answer_type": item["answer_type"],
                    "passed": passed,
                    "detail": detail,
                    "expected": item["expected"],
                    "actual": actual,
                    "tool_calls": len(item["gold_calls"]),
                    "invalid_calls": invalid,
                    "latency_s": round(time.perf_counter() - started, 3),
                }
            )
    return rows


# ---------------------------------------------------------------- agent mode
class ProviderError(RuntimeError):
    """Raised when a provider is misconfigured (missing key, missing SDK)."""


def require_env(name: str) -> str:
    """Return an environment variable or raise a clear error."""
    value = os.environ.get(name)
    if not value:
        raise ProviderError(f"{name} is not set: export it in the environment (never in a file).")
    return value


class AnthropicAgent:
    """Tool-use loop on the Anthropic Messages API."""

    def __init__(self, model: str) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError("pip install anthropic") from exc
        self.client = anthropic.AsyncAnthropic(api_key=require_env("ANTHROPIC_API_KEY"))
        self.model = model

    @staticmethod
    def schema(tools: list[Any]) -> list[dict[str, Any]]:
        """Convert MCP tools to Anthropic tool definitions."""
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in tools
        ]

    async def run(
        self, system: str, question: str, tools: list[Any], call_tool: Any
    ) -> dict[str, Any]:
        """Run one question; returns final text and counters."""
        defs = self.schema(tools)
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        stats = {"tool_calls": 0, "invalid_calls": 0, "input_tokens": 0, "output_tokens": 0}
        while True:
            budget_left = stats["tool_calls"] < MAX_TOOL_CALLS
            kwargs: dict[str, Any] = {} if budget_left else {"tool_choice": {"type": "none"}}
            reply = await self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=system,
                tools=defs,
                messages=messages,
                **kwargs,
            )
            stats["input_tokens"] += reply.usage.input_tokens
            stats["output_tokens"] += reply.usage.output_tokens
            uses = [b for b in reply.content if b.type == "tool_use"]
            if not uses or not budget_left:
                text = "".join(b.text for b in reply.content if b.type == "text")
                return {"text": text, **stats}
            messages.append({"role": "assistant", "content": reply.content})
            results = []
            for use in uses:
                stats["tool_calls"] += 1
                text, is_error = await call_tool(use.name, dict(use.input))
                stats["invalid_calls"] += int(is_error)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": text,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})


class OpenAICompatAgent:
    """Tool-call loop on an OpenAI-compatible chat completions endpoint."""

    def __init__(self, model: str) -> None:
        try:
            import openai
        except ImportError as exc:
            raise ProviderError("pip install openai") from exc
        self.client = openai.AsyncOpenAI(
            api_key=require_env("OPENAI_API_KEY"), base_url=require_env("OPENAI_BASE_URL")
        )
        self.model = model

    @staticmethod
    def schema(tools: list[Any]) -> list[dict[str, Any]]:
        """Convert MCP tools to OpenAI function definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    async def run(
        self, system: str, question: str, tools: list[Any], call_tool: Any
    ) -> dict[str, Any]:
        """Run one question; returns final text and counters."""
        defs = self.schema(tools)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]
        stats = {"tool_calls": 0, "invalid_calls": 0, "input_tokens": 0, "output_tokens": 0}
        while True:
            budget_left = stats["tool_calls"] < MAX_TOOL_CALLS
            reply = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=defs,
                tool_choice="auto" if budget_left else "none",
            )
            if reply.usage:
                stats["input_tokens"] += reply.usage.prompt_tokens
                stats["output_tokens"] += reply.usage.completion_tokens
            message = reply.choices[0].message
            if not message.tool_calls or not budget_left:
                return {"text": message.content or "", **stats}
            messages.append(message.model_dump(exclude_none=True))
            for tc in message.tool_calls:
                stats["tool_calls"] += 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    text, is_error = "Invalid JSON arguments.", True
                else:
                    text, is_error = await call_tool(tc.function.name, args)
                stats["invalid_calls"] += int(is_error)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})


async def run_agent(
    items: list[dict[str, Any]], provider: str, model: str, today: str
) -> list[dict[str, Any]]:
    """Let the model under test answer every question through the MCP tools."""
    from mcp import Client

    from marches_publics_mcp.server import build_server

    agent: AnthropicAgent | OpenAICompatAgent
    if provider == "anthropic":
        agent = AnthropicAgent(model)
    elif provider == "openai-compatible":
        agent = OpenAICompatAgent(model)
    else:
        raise ProviderError(f"unknown provider {provider!r}")
    system = SYSTEM_PROMPT.format(today=today)
    rows: list[dict[str, Any]] = []
    async with Client(build_server()) as client:
        tools = (await client.list_tools()).tools

        async def call_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
            try:
                result = await client.call_tool(name, args)
            except Exception as exc:  # schema validation / unknown tool
                return f"Error: {exc}", True
            return result_text(result), bool(getattr(result, "is_error", False))

        for item in items:
            started = time.perf_counter()
            out = await agent.run(system, item["question"], tools, call_tool)
            found, value = parse_answer_block(out["text"])
            if item["answer_type"] == "contains":
                # Free text is acceptable here: look for the facts in the answer and the text.
                value = [value, out["text"]] if found else out["text"]
                passed, detail = score_item(item, value)
            elif found:
                passed, detail = score_item(item, value)
            else:
                passed, detail = False, "no JSON answer block"
            rows.append(
                {
                    "id": item["id"],
                    "answer_type": item["answer_type"],
                    "passed": passed,
                    "detail": detail,
                    "expected": item["expected"],
                    "actual": value,
                    "final_text": out["text"],
                    "tool_calls": out["tool_calls"],
                    "invalid_calls": out["invalid_calls"],
                    "input_tokens": out["input_tokens"],
                    "output_tokens": out["output_tokens"],
                    "latency_s": round(time.perf_counter() - started, 3),
                }
            )
    return rows


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate the questions file and exit")
    parser.add_argument("--mode", choices=["oracle", "agent"])
    parser.add_argument("--provider", choices=["anthropic", "openai-compatible"])
    parser.add_argument("--model", help="model id (agent mode)")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--only", nargs="*", help="restrict to these question ids")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    parser.add_argument("--today", default=date.today().isoformat(), help="date given to the agent")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    if args.check:
        return check(args.questions)
    if not args.mode:
        print("choose --check or --mode oracle|agent", file=sys.stderr)
        return 2
    items = load_items(args.questions)
    errors = validate_items(items)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    if args.only:
        items = [i for i in items if i["id"] in set(args.only)]
    if args.mode == "oracle":
        rows = asyncio.run(run_oracle(items))
        label = "gold"
    else:
        if not args.provider or not args.model:
            print("agent mode needs --provider and --model", file=sys.stderr)
            return 2
        try:
            rows = asyncio.run(run_agent(items, args.provider, args.model, args.today))
        except ProviderError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        label = args.model.replace("/", "_")
    path = write_results(args.mode, label, rows, args.out)
    summary = summarize(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row in rows:
        print(f"{'PASS' if row['passed'] else 'FAIL'} {row['id']}: {row['detail']}")
    print(f"results -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
