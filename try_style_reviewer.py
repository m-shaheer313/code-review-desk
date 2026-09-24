"""Temporary harness: one Style Reviewer run, end to end (FR-3 + FR-4).

Not part of the product. Named `try_*` rather than `test_*` so pytest does not
collect it. The Desk will replace it in Phase 3.

    python try_style_reviewer.py

Deliberately excluded: Security/Tests reviewers, concurrency, Merge, the Desk.
"""

import asyncio
import json
import sys

from agents import RunConfig, Runner
from agents.agent_output import AgentOutputSchema
from agents.exceptions import AgentsException
from openai import APIStatusError
from pydantic import ValidationError

from config import ConfigError, REVIEWER_MAX_TURNS
from diff_utils import split_diff_by_file
from finding import Finding
from review_context import ReviewContext
from reviewers import build_style_instructions, build_style_reviewer

SAMPLE_DIFF = """diff --git a/payments/Processor.py b/payments/Processor.py
index 1111111..2222222 100644
--- a/payments/Processor.py
+++ b/payments/Processor.py
@@ -1,6 +1,14 @@
 import os
+from decimal import *
+
+def ProcessRefund(OrderID, amount=[], retries=3):
+    # legacy path, kept just in case
+    # if order.is_void: return None
+    try:
+        total = Decimal(amount)
+    except:
+        pass
+    if len(OrderID) == 0:
+        return None
+    return total
"""


def _provider_message(exc: APIStatusError) -> str:
    """The provider's own explanation, one line, without the traceback."""
    # Gemini's OpenAI-compatible endpoint returns the error body as a *list* of
    # objects, unlike OpenAI's single object — handle both.
    body = exc.body
    if isinstance(body, list):
        body = body[0] if body else {}
    error = body.get("error") if isinstance(body, dict) else None
    message = (error or {}).get("message") if isinstance(error, dict) else None
    return " ".join((message or str(exc)).split())[:300]


async def main() -> int:
    # The prompts contain em dashes; the Windows console defaults to cp1252.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    chunks, split_error = split_diff_by_file(SAMPLE_DIFF)
    if split_error is not None:
        print(split_error)
        return 1
    chunk = chunks[0]

    review_context = ReviewContext(
        repo="code-review-desk",
        language="Python",
        ruleset_id="python-default",
        strictness="normal",
    )

    # TODO(remove-before-phase-3): resolved-prompt debug print. FR-4's acceptance
    # criterion — the fully assembled instructions are inspectable *before* any
    # model call for this run.
    print("=" * 72)
    print("RESOLVED INSTRUCTIONS (before the model call)")
    print("=" * 72)
    print(build_style_instructions(review_context))
    print()

    # TODO(remove-before-phase-3): same builder, strict context — proves two
    # contexts produce visibly different prompts (FR-4).
    strict_context = ReviewContext(
        repo="code-review-desk",
        language="Python",
        ruleset_id="python-default",
        strictness="strict",
    )
    print("=" * 72)
    print("RESOLVED INSTRUCTIONS — same reviewer, strictness='strict'")
    print("=" * 72)
    print(build_style_instructions(strict_context))
    print()

    # TODO(remove-before-phase-3): FR-3's second acceptance criterion — the
    # generated schema wraps the list in a single-key object, because a strict
    # JSON schema must have an object at its root.
    output_schema = AgentOutputSchema(list[Finding])
    print("=" * 72)
    print("GENERATED OUTPUT SCHEMA for list[Finding] (internal wrapper)")
    print("=" * 72)
    print(json.dumps(output_schema.json_schema(), indent=2))
    print("wrapper key(s):", list(output_schema.json_schema()["properties"]))
    print()

    style_reviewer = build_style_reviewer()
    try:
        result = await Runner.run(
            style_reviewer,
            chunk["diff_text"],
            context=review_context,
            max_turns=REVIEWER_MAX_TURNS,  # plan.md §10
            # FR-13 wires tracing properly; off here so this harness needs no
            # tracing backend. Set at the run level, never globally.
            run_config=RunConfig(tracing_disabled=True),
        )
    except APIStatusError as exc:
        # Rate limits and 5xx from the provider are an expected operating
        # condition, not a defect — one plain line, no traceback (Article VIII.2).
        print(f"the review could not run: the model API returned {exc.status_code}")
        print(f"  {_provider_message(exc)}")
        return 1
    except (AgentsException, ValidationError) as exc:
        print(f"the review could not run: {type(exc).__name__}: {exc}")
        return 1

    findings = result.final_output
    print("=" * 72)
    print("RESULT")
    print("=" * 72)
    print("type(result.final_output):", type(findings))
    print("is a plain list:", isinstance(findings, list))
    criticals = [f for f in findings if f.severity == "critical"]
    print("findings:", len(findings), "| critical findings:", len(criticals))
    for finding in findings:
        print(f"  [{finding.severity:8}] {finding.file}:{finding.line} — {finding.message}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ConfigError as exc:
        print(str(exc))
        sys.exit(1)
