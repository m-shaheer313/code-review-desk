"""LIVE verification of FR-7: one agent object, two models, no agent mutation.

    python scripts/live_test_fr7.py

Run 1 uses the agent's own configured model (MODEL_NAME). Run 2 uses
`run_reviewer_with_override` with CHEAP_MODEL_NAME. The same agent object serves
both, and its `model` attribute is checked by identity before, between, and after.

Each run is one or two requests (Style calls get_ruleset), so ~4 total.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import RunConfig, Runner  # noqa: E402
from agents.exceptions import AgentsException  # noqa: E402
from openai import APIStatusError  # noqa: E402

from config import (  # noqa: E402
    CHEAP_MODEL_NAME,
    ConfigError,
    MODEL_NAME,
    REVIEWER_MAX_TURNS,
    configure_tracing,
)
from review_context import ReviewContext  # noqa: E402
from review_runner import run_reviewer_with_override  # noqa: E402
from reviewers import build_style_reviewer  # noqa: E402

DIFF = """--- a/billing/fees.py
+++ b/billing/fees.py
@@ -1,3 +1,8 @@
 import logging
+from math import *
+
+def CalculateFee(Amount, rate=0.05):
+    return Amount * rate
"""


def provider_message(exc: APIStatusError) -> str:
    body = exc.body
    if isinstance(body, list):
        body = body[0] if body else {}
    error = body.get("error") if isinstance(body, dict) else None
    message = (error or {}).get("message") if isinstance(error, dict) else None
    return " ".join((message or str(exc)).split())[:200]


def show(label: str, model_name: str, outcome) -> None:
    print(f"  {label}")
    print(f"    model: {model_name}")
    if isinstance(outcome, str):
        print(f"    FAILED: {outcome}")
        return
    print(f"    findings: {len(outcome)}")
    for finding in outcome:
        print(f"      [{finding.severity:8}] {finding.file}:{finding.line} — {finding.message}")


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    configure_tracing()  # FR-13: tracing on

    context = ReviewContext(
        repo="billing-service",
        language="Python",
        ruleset_id="python-default",
        strictness="normal",
    )

    # ONE agent object for both runs.
    agent = build_style_reviewer()
    model_before = agent.model

    print("=" * 78)
    print("FR-7 — SAME AGENT OBJECT, TWO MODELS")
    print("=" * 78)
    print(f"agent.model object id before any run : {id(model_before)}")
    print(f"agent.model.model before any run     : {model_before.model}")
    print()

    # Run 1 — the agent's own configured model. No override anywhere.
    try:
        result = await Runner.run(
            agent,
            DIFF,
            context=context,
            max_turns=REVIEWER_MAX_TURNS,
            run_config=RunConfig(),
        )
        own_outcome = result.final_output
    except APIStatusError as exc:
        own_outcome = f"{exc.status_code} — {provider_message(exc)}"
    except AgentsException as exc:
        own_outcome = f"{type(exc).__name__}: {exc}"

    model_between = agent.model
    print(f"agent.model object id after run 1    : {id(model_between)}")
    print()

    # Run 2 — the SAME agent object, model overridden at the run level.
    try:
        result = await run_reviewer_with_override(
            agent, DIFF, context, CHEAP_MODEL_NAME
        )
        override_outcome = result.final_output
    except APIStatusError as exc:
        override_outcome = f"{exc.status_code} — {provider_message(exc)}"
    except AgentsException as exc:
        override_outcome = f"{type(exc).__name__}: {exc}"

    model_after = agent.model

    print("=" * 78)
    print("RESULTS SIDE BY SIDE")
    print("=" * 78)
    show("RUN 1 — agent's own model", MODEL_NAME, own_outcome)
    print()
    show("RUN 2 — run-level override", CHEAP_MODEL_NAME, override_outcome)

    print()
    print("=" * 78)
    print("FR-7 ACCEPTANCE CRITERION — agent.model never reassigned")
    print("=" * 78)
    print(f"  object id before   : {id(model_before)}")
    print(f"  object id between  : {id(model_between)}")
    print(f"  object id after    : {id(model_after)}")
    print(f"  model_before is model_between : {model_before is model_between}")
    print(f"  model_before is model_after   : {model_before is model_after}")
    print(f"  agent.model.model still       : {agent.model.model}")
    print(f"  (override model name used)    : {CHEAP_MODEL_NAME}")
    identical = model_before is model_between is model_after
    print()
    print(f"  IDENTICAL ACROSS BOTH RUNS: {identical}")
    return 0 if identical else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ConfigError as exc:
        print(str(exc))
        sys.exit(1)
