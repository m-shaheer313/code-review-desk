"""LIVE verification of FR-9a: the Style Reviewer's forced get_ruleset call.

    python scripts/live_test_fr9a.py

Runs ONE Style review through the production path, `review_runner.run_reviewer`,
against the mandated model (gemini-3.6-flash). About two requests: the forced
lookup call, then the findings call.

What it proves, from the real API:
- phase 1 (lookup) made a forced get_ruleset call, and the tool actually ran;
- phase 2 (Style) received that ruleset text and returned a plain list[Finding]
  — the request that used to fail with HTTP 400 now succeeds;
- the footer's token count sums both runs.

`Runner.run` is wrapped only to observe each run; the wrapper delegates to the
real one unchanged.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import RunConfig, Runner  # noqa: E402
from agents.exceptions import AgentsException  # noqa: E402
from agents.items import ToolCallItem, ToolCallOutputItem  # noqa: E402
from openai import APIStatusError  # noqa: E402

import review_runner  # noqa: E402
from config import ConfigError, MODEL_NAME, configure_tracing  # noqa: E402
from hooks import ReviewerRunHooks  # noqa: E402
from review_context import ReviewContext  # noqa: E402
from reviewers import build_style_reviewer  # noqa: E402

DIFF = """--- a/billing/fees.py
+++ b/billing/fees.py
@@ -1,3 +1,6 @@
 import logging
+from math import *
+
+def CalculateFee(Amount, rate=0.05):
+    return Amount * rate
"""

observed: list[dict] = []
_real_run = Runner.run


async def observing_run(agent, input, **kwargs):
    result = await _real_run(agent, input, **kwargs)
    items = getattr(result, "new_items", [])
    observed.append(
        {
            "agent": agent.name,
            "tool_choice": agent.model_settings.tool_choice,
            "output_type": agent.output_type,
            "tool_calls": [
                getattr(i.raw_item, "name", "?") for i in items if isinstance(i, ToolCallItem)
            ],
            "tool_outputs": [i.output for i in items if isinstance(i, ToolCallOutputItem)],
            "input_head": input[:60] if isinstance(input, str) else type(input).__name__,
            "final_output": result.final_output,
        }
    )
    return result


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    configure_tracing()
    Runner.run = observing_run

    context = ReviewContext(
        repo="billing-service", language="Python", ruleset_id="python-default"
    )
    hooks = ReviewerRunHooks()
    rule(f"ONE LIVE STYLE REVIEW — model {MODEL_NAME}")
    try:
        result = await review_runner.run_reviewer(
            build_style_reviewer(), DIFF, context, RunConfig(), hooks
        )
    except APIStatusError as exc:
        print(f"FAILED: model API returned {exc.status_code}: {exc}")
        return 1
    except (AgentsException, review_runner.RulesetNotConsulted) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1

    for n, run in enumerate(observed, 1):
        rule(f"RUN {n}: {run['agent']}")
        print(f"  tool_choice          : {run['tool_choice']!r}")
        print(f"  output_type          : {run['output_type']}")
        print(f"  input starts with    : {run['input_head']!r}")
        print(f"  tool calls made      : {run['tool_calls']}")
        for out in run["tool_outputs"]:
            print(f"  tool output (head)   : {str(out).splitlines()[0]!r} … ({len(str(out))} chars)")
        out = run["final_output"]
        print(f"  final_output type    : {type(out).__name__}")

    rule("FINDINGS (phase 2's final_output)")
    findings = result.final_output
    print(f"  type: {type(findings).__name__}   count: {len(findings)}")
    for f in findings:
        print(f"  [{f.severity:8}] {f.file}:{f.line} — {f.message}")

    rule("FR-9a VERDICT")
    lookup, style = observed[0], observed[-1]
    checks = {
        "phase 1 forced get_ruleset by name": lookup["tool_choice"] == "get_ruleset",
        "phase 1 actually called get_ruleset": lookup["tool_calls"] == ["get_ruleset"],
        "the tool really ran (output present)": bool(lookup["tool_outputs"]),
        "phase 2 had no forced tool_choice": style["tool_choice"] is None,
        "phase 2 input began with the RULESET": str(style["input_head"]).startswith("RULESET"),
        "phase 2 returned a plain list": type(findings) is list,
        "findings cite the ruleset's rule ids": any("PY0" in f.message for f in findings),
    }
    for label, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print(f"\n  tokens (both runs, from the run-level hooks): {hooks.stats.tokens}")
    print(f"  model requests: {hooks.stats.requests}   tool calls: {hooks.stats.tool_calls}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ConfigError as exc:
        print(str(exc))
        sys.exit(1)
