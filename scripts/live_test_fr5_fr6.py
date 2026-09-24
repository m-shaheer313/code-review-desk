"""LIVE verification of FR-5 (concurrency) and FR-6 (merge tool + remediation handoff).

This is the one deliberately-delayed live test: it makes real calls to the real
model through `desk.run_review`. Everything else in this repo is verified offline.

    python scripts/live_test_fr5_fr6.py

Cost note: one run is three reviewer runs (Style takes two turns because it calls
get_ruleset), plus Merge, plus Remediation if a critical security finding lands —
roughly 6-8 requests against a free-tier quota of 20/day.

`run_review` is called exactly once. `run_all_reviewers` is wrapped only to capture
the GroupOutcome, so the per-reviewer and group timings FR-5 requires can be
printed; the wrapper delegates to the real function and changes nothing.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import RunConfig  # noqa: E402

import desk  # noqa: E402
from config import CHEAP_MODEL_NAME, ConfigError, MODEL_NAME, build_model, configure_tracing  # noqa: E402
from review_runner import trace_id_for  # noqa: E402
from remediation import decide_needs_remediation  # noqa: E402
from review_context import ReviewContext  # noqa: E402
from review_runner import run_all_reviewers as real_run_all_reviewers  # noqa: E402

# A plausible-looking but entirely fake credential, a new untested function, and
# two real python-default.txt violations (PY003 wildcard import, PY001 naming).
SAMPLE_DIFF = '''diff --git a/billing/refunds.py b/billing/refunds.py
index 3f8a1c2..9b2e4d7 100644
--- a/billing/refunds.py
+++ b/billing/refunds.py
@@ -1,8 +1,29 @@
 import logging
-from decimal import Decimal
+from decimal import *

 logger = logging.getLogger(__name__)
+
+STRIPE_SECRET_KEY = "sk_live_51MZq8vKdR7xWpN3bQfHjLc9TgYeA2sVuX4mB6nD8kJhF0rPzQw"
+
+
+def ProcessRefund(order_id, amount, reason=None):
+    total = Decimal(str(amount))
+    if total <= 0:
+        raise ValueError("refund amount must be positive")
+    response = gateway.post(
+        "/v1/refunds",
+        headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY},
+        json={"order": order_id, "amount": str(total), "reason": reason},
+    )
+    if response.status_code != 200:
+        logger.error("refund failed for order %s: %s", order_id, response.text)
+        return None
+    return response.json()
+
+
+def refund_is_allowed(order):
+    if order.status == "shipped":
+        return False
+    return order.total > 0


 def legacy_refund(order_id):
'''


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    context = ReviewContext(
        repo="billing-service",
        language="Python",
        ruleset_id="python-default",
        strictness="normal",
    )

    captured: dict = {}

    async def capturing_run_all_reviewers(diff_text, ctx, **kwargs):
        group = await real_run_all_reviewers(diff_text, ctx, **kwargs)
        captured["group"] = group
        return group

    desk.run_all_reviewers = capturing_run_all_reviewers

    # `--cheap` routes the whole review through FR-7's run-level override. The
    # free-tier daily quota is per model, so this is the fallback when the mandated
    # model's 20/day is exhausted. It exercises identical wiring; only the model
    # differs, and no agent definition changes (Article I.3).
    use_cheap = "--cheap" in sys.argv
    model_in_use = CHEAP_MODEL_NAME if use_cheap else MODEL_NAME
    # FR-13: tracing ON — this run's trace is the visual proof of FR-5 (the
    # three reviewer spans should overlap). The trace id is printed at the end.
    configure_tracing()
    run_config = RunConfig()
    if use_cheap:
        run_config = RunConfig(model=build_model(CHEAP_MODEL_NAME))

    rule("RUNNING ONE LIVE REVIEW")
    print(f"model: {model_in_use}" + ("  (FR-7 run-level override)" if use_cheap else ""))
    print("model calls are happening now; this takes a few seconds...")
    report, error = await desk.run_review(SAMPLE_DIFF, context, run_config=run_config)

    if error is not None:
        print(f"\nreview could not run: {error}")
        return 1

    group = captured.get("group")

    rule("FR-5 — CONCURRENCY: individual vs group wall-clock")
    if group is None:
        print("group outcome was not captured")
    else:
        for outcome in group.outcomes:
            status = "ok" if not outcome.failed else f"FAILED ({outcome.error})"
            print(
                f"  {outcome.reviewer:17} {outcome.elapsed_ms:6} ms   "
                f"raw findings: {len(outcome.findings):2}   {status}"
            )
        print()
        print(f"  group total (around the gather) : {group.group_elapsed_ms:6} ms")
        print(f"  sum of the three individually   : {group.sum_individual_ms:6} ms")
        print(f"  slowest single reviewer         : {group.slowest_ms:6} ms")
        overhead = group.group_elapsed_ms - group.slowest_ms
        print()
        print(f"  group - slowest = {overhead} ms  (concurrent if small)")
        print(
            f"  group < sum     = {group.group_elapsed_ms < group.sum_individual_ms}  "
            f"(sequential would make these equal)"
        )

    rule("RAW FINDINGS PER REVIEWER (before merge)")
    raw_total = 0
    if group is not None:
        for outcome in group.outcomes:
            print(f"  {outcome.reviewer}: {len(outcome.findings)}")
            for finding in outcome.findings:
                print(
                    f"     [{finding.severity:8}] {finding.file}:{finding.line} "
                    f"— {finding.message}"
                )
            raw_total += len(outcome.findings)
        print(f"\n  raw union total: {raw_total}")

    rule("FR-6 — MERGED FINDINGS (after the merge_findings tool)")
    print(f"  merged count: {len(report.findings)}  (raw union was {raw_total})")
    print(f"  merged <= raw union: {len(report.findings) <= raw_total}")
    order = [f.severity for f in report.findings]
    rank = {"critical": 0, "major": 1, "minor": 2}
    print(f"  severity order: {order}")
    print(
        "  ordered critical-first: "
        f"{all(rank[a] <= rank[b] for a, b in zip(order, order[1:]))}"
    )
    print()
    for finding in report.findings:
        print(
            f"  [{finding.severity:8}] {finding.file}:{finding.line} "
            f"({finding.source_reviewer}) — {finding.message}"
        )

    rule("FR-6 — REMEDIATION HANDOFF")
    fired = decide_needs_remediation(report.findings)
    print(f"  decide_needs_remediation(merged findings): {fired}")
    print(f"  report.remediation_proposed:               {report.remediation_proposed}")
    if report.remediation_proposal:
        print()
        print("  --- Remediation Specialist's proposal (verbatim) ---")
        for line in report.remediation_proposal.splitlines():
            print(f"  {line}")
    else:
        print("  (no proposal text)")

    rule("REPORT NOTES / ERRORS")
    if report.notes:
        for note in report.notes:
            print(f"  - {note}")
    else:
        print("  (none)")
    print(f"\n  report.is_partial: {report.is_partial}")

    rule("FR-13 — TRACE")
    if group is not None:
        trace_id = trace_id_for(group.request_id)
        print(f"  request_id : {group.request_id}")
        print(f"  trace_id   : {trace_id}")
        print(f"  open it at : https://platform.openai.com/traces/trace?trace_id={trace_id}")
        print("  (exported in the background; allow a few seconds before opening)")

    rule("FOOTER")
    for row in report.footer:
        print(
            f"  {row.reviewer:17} ms={row.ms:6} tokens={row.tokens} partial={row.partial}"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ConfigError as exc:
        print(str(exc))
        sys.exit(1)
