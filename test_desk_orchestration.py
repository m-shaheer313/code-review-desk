"""Structural test for the Desk's orchestration (FR-1 → FR-6). No live model call.

WHAT IS AND IS NOT STUBBED. Exactly one thing is replaced: `Runner.run`, the
single boundary where this codebase hands work to a model. Merge and Remediation
are NOT stubbed as components — the real `as_merge_tool()` FunctionTool is built
and invoked, its real `custom_output_extractor` serializes the result, the real
`parse_merged_findings` parses it, the real `decide_needs_remediation` decides,
and the real `Report` is assembled. Only the text a model would have generated is
canned, because generating it is the one part that requires the API.

Run with `python test_desk_orchestration.py`.
"""

import asyncio

import desk
import review_runner
from finding import Finding
from merge import MERGE_INPUT_KEY, MERGE_SPECIALIST_NAME
from remediation import REMEDIATION_SPECIALIST_NAME
from report import Report
from review_context import ReviewContext
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
)

DIFF = """diff --git a/payments/processor.py b/payments/processor.py
index 1111111..2222222 100644
--- a/payments/processor.py
+++ b/payments/processor.py
@@ -1,3 +1,6 @@
 import os
+API_KEY = "sk-live-REDACTED-SHAPE"
+def refund(order_id, amount):
+    return db.execute("SELECT * FROM orders WHERE id = " + order_id)
"""

PROPOSAL = (
    "PROPOSAL ONLY — nothing has been applied, committed, or pushed.\n"
    "1. Move the key out of source into an environment variable.\n"
    "2. Treat the committed key as compromised and rotate it.\n"
    "3. Use a parameterized query instead of string concatenation."
)


class FakeResult:
    """Stand-in for RunResult — only `final_output` is read by the code."""

    def __init__(self, final_output):
        self.final_output = final_output


# Two OVERLAPPING findings (same file, adjacent lines, same underlying issue,
# different reviewers) plus one critical Security finding.
SECURITY_FINDINGS = [
    Finding(
        file="payments/processor.py",
        line=2,
        severity="critical",
        message="Hardcoded API credential committed in source.",
        source_reviewer="the model made this up",  # must be overwritten by the runner
    ),
    Finding(
        file="payments/processor.py",
        line=5,
        severity="major",
        message="SQL built by string concatenation from a caller-supplied id.",
    ),
]
STYLE_FINDINGS = [
    # Overlaps the Security critical: same file, one line away, same issue.
    Finding(
        file="payments/processor.py",
        line=2,
        severity="minor",
        message="Module-level constant holds a secret value.",
    ),
]
TESTS_FINDINGS = [
    Finding(
        file="payments/processor.py",
        line=4,
        severity="major",
        message="No test accompanies the new refund() path.",
    ),
]

# What a correct Merge would return: the duplicate pair collapsed to the higher
# severity, ordered critical → major → minor.
MERGED = [
    Finding(
        file="payments/processor.py",
        line=2,
        severity="critical",
        message="Hardcoded API credential committed in source.",
        source_reviewer=SECURITY_REVIEWER_NAME,
    ),
    Finding(
        file="payments/processor.py",
        line=5,
        severity="major",
        message="SQL built by string concatenation from a caller-supplied id.",
        source_reviewer=SECURITY_REVIEWER_NAME,
    ),
    Finding(
        file="payments/processor.py",
        line=4,
        severity="major",
        message="No test accompanies the new refund() path.",
        source_reviewer=TESTS_REVIEWER_NAME,
    ),
]

BY_AGENT = {
    SECURITY_REVIEWER_NAME: FakeResult(SECURITY_FINDINGS),
    TESTS_REVIEWER_NAME: FakeResult(TESTS_FINDINGS),
    STYLE_REVIEWER_NAME: FakeResult(STYLE_FINDINGS),
    MERGE_SPECIALIST_NAME: FakeResult(MERGED),
    REMEDIATION_SPECIALIST_NAME: FakeResult(PROPOSAL),
}

# Everything the stub saw, so the test can assert on what was actually sent.
calls: list[tuple[str, str]] = []


def install_stub(by_agent=None):
    """Replace the one model boundary. Handles both call styles: reviewers pass
    the agent positionally, `as_tool` passes it as `starting_agent=`."""
    table = BY_AGENT if by_agent is None else by_agent
    calls.clear()

    async def fake_run(agent=None, input=None, *, starting_agent=None, **kwargs):
        resolved = starting_agent if agent is None else agent
        sent = kwargs.get("input", input)
        calls.append((resolved.name, sent if isinstance(sent, str) else repr(sent)))
        await asyncio.sleep(0.01)
        result = table[resolved.name]
        if isinstance(result, BaseException):
            raise result
        return result

    review_runner.Runner.run = fake_run  # same Runner class object everywhere
    desk.Runner.run = fake_run


def context() -> ReviewContext:
    return ReviewContext(
        repo="code-review-desk", language="Python", ruleset_id="python-default"
    )


def test_full_flow_reaches_remediation_and_builds_report() -> None:
    install_stub()
    report, error = asyncio.run(desk.run_review(DIFF, context()))

    assert error is None, error
    assert isinstance(report, Report)

    # (f)+(g) the remediation step was reached and the Report records it.
    assert report.remediation_proposed is True
    assert report.remediation_proposal == PROPOSAL
    assert "PROPOSAL ONLY" in report.remediation_proposal

    # (d) merged findings came back through the real tool + parser round trip.
    assert len(report.findings) == 3
    assert [f.severity for f in report.findings] == ["critical", "major", "major"]
    assert all(isinstance(f, Finding) for f in report.findings)

    # (g) footer has one row per reviewer, with real latency and stub tokens.
    assert len(report.footer) == 3
    assert {row.reviewer for row in report.footer} == {
        SECURITY_REVIEWER_NAME,
        TESTS_REVIEWER_NAME,
        STYLE_REVIEWER_NAME,
    }
    assert all(row.tokens == 0 for row in report.footer)
    assert all(row.partial is False for row in report.footer)
    assert report.is_partial is False
    assert report.notes == []


def test_every_stage_was_actually_invoked() -> None:
    install_stub()
    asyncio.run(desk.run_review(DIFF, context()))

    invoked = [name for name, _ in calls]
    for expected in (
        SECURITY_REVIEWER_NAME,
        TESTS_REVIEWER_NAME,
        STYLE_REVIEWER_NAME,
        MERGE_SPECIALIST_NAME,
        REMEDIATION_SPECIALIST_NAME,
    ):
        assert expected in invoked, f"{expected} was never invoked: {invoked}"
    # Ordering: all three reviewers precede Merge, which precedes Remediation.
    assert invoked.index(MERGE_SPECIALIST_NAME) > max(
        invoked.index(n)
        for n in (SECURITY_REVIEWER_NAME, TESTS_REVIEWER_NAME, STYLE_REVIEWER_NAME)
    )
    assert invoked.index(REMEDIATION_SPECIALIST_NAME) > invoked.index(
        MERGE_SPECIALIST_NAME
    )


def test_merge_received_the_documented_json_contract() -> None:
    install_stub()
    asyncio.run(desk.run_review(DIFF, context()))

    sent = next(payload for name, payload in calls if name == MERGE_SPECIALIST_NAME)
    import json

    parsed = json.loads(sent)
    assert list(parsed) == [MERGE_INPUT_KEY], parsed
    assert len(parsed[MERGE_INPUT_KEY]) == 4  # 2 security + 1 tests + 1 style
    for item in parsed[MERGE_INPUT_KEY]:
        assert set(item) == {
            "file",
            "line",
            "severity",
            "message",
            "source_reviewer",
        }, item
    # The runner's stamp reached Merge — including over the model's invented value.
    assert {item["source_reviewer"] for item in parsed[MERGE_INPUT_KEY]} == {
        SECURITY_REVIEWER_NAME,
        TESTS_REVIEWER_NAME,
        STYLE_REVIEWER_NAME,
    }


def test_remediation_briefed_with_only_the_qualifying_findings() -> None:
    install_stub()
    asyncio.run(desk.run_review(DIFF, context()))

    sent = next(
        payload for name, payload in calls if name == REMEDIATION_SPECIALIST_NAME
    )
    import json

    briefing = json.loads(sent)["critical_security_findings"]
    assert len(briefing) == 1
    assert briefing[0]["severity"] == "critical"
    assert briefing[0]["source_reviewer"] == SECURITY_REVIEWER_NAME


def test_no_critical_security_finding_skips_remediation() -> None:
    merged_without_critical = [
        f.model_copy(update={"severity": "major"}) for f in MERGED
    ]
    table = dict(BY_AGENT)
    table[SECURITY_REVIEWER_NAME] = FakeResult(
        [f.model_copy(update={"severity": "major"}) for f in SECURITY_FINDINGS]
    )
    table[MERGE_SPECIALIST_NAME] = FakeResult(merged_without_critical)
    install_stub(table)

    report, error = asyncio.run(desk.run_review(DIFF, context()))
    assert error is None
    assert report.remediation_proposed is False
    assert report.remediation_proposal is None
    assert REMEDIATION_SPECIALIST_NAME not in [name for name, _ in calls]


def test_failed_reviewer_still_produces_a_partial_report() -> None:
    table = dict(BY_AGENT)
    table[STYLE_REVIEWER_NAME] = RuntimeError("connection reset")
    install_stub(table)

    report, error = asyncio.run(desk.run_review(DIFF, context()))
    assert error is None
    assert report.is_partial is True
    assert [row.partial for row in report.footer if row.reviewer == STYLE_REVIEWER_NAME] == [True]
    assert any("StyleReviewer did not complete" in note for note in report.notes)
    # The review still reached remediation on Security's critical.
    assert report.remediation_proposed is True


def test_empty_and_malformed_diffs_report_plainly() -> None:
    install_stub()
    for bad in ("", "   \n", "this is not a diff at all"):
        report, error = asyncio.run(desk.run_review(bad, context()))
        assert report is None
        assert isinstance(error, str) and error
    assert calls == [], "no model should be called for an unusable diff"


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
