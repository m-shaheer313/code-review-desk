"""Structural tests for the Desk as a real Agent (FR-6 wiring + FR-8). No live call.

Only `Runner.run` is stubbed. The real Desk agent is built, so its tools, handoffs,
output type and guardrails are the real objects; the real guardrail runs; the real
Report is assembled.

Run with `python test_desk_agent.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import asyncio
import json

import desk
import main as entry
import review_runner
from agents.exceptions import OutputGuardrailTripwireTriggered
from finding import Finding
from guardrail import REFUSAL_MESSAGE, ReportRefused
from merge import MERGE_INPUT_KEY, MERGE_TOOL_NAME
from remediation import REMEDIATION_SPECIALIST_NAME
from report import Report
from review_context import ReviewContext
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
)

DIFF = """diff --git a/billing/refunds.py b/billing/refunds.py
--- a/billing/refunds.py
+++ b/billing/refunds.py
@@ -1,2 +1,5 @@
 import logging
+from decimal import *
+def ProcessRefund(order_id):
+    return None
"""

SECURITY_RAW = [
    Finding(
        file="billing/refunds.py",
        line=2,
        severity="critical",
        message="A hardcoded credential was committed; rotate it and load from env.",
    )
]
STYLE_RAW = [
    Finding(
        file="billing/refunds.py",
        line=2,
        severity="minor",
        message="PY003: Do not use wildcard imports.",
    )
]
TESTS_RAW = [
    Finding(
        file="billing/refunds.py",
        line=3,
        severity="major",
        message="No test accompanies the new ProcessRefund path.",
    )
]

MERGED = [
    Finding(
        file="billing/refunds.py",
        line=2,
        severity="critical",
        message="A hardcoded credential was committed; rotate it and load from env.",
        source_reviewer=SECURITY_REVIEWER_NAME,
    ),
    Finding(
        file="billing/refunds.py",
        line=3,
        severity="major",
        message="No test accompanies the new ProcessRefund path.",
        source_reviewer=TESTS_REVIEWER_NAME,
    ),
]

PROPOSAL = "PROPOSAL ONLY — nothing applied. Load the credential from the environment and rotate it."
DIRTY_PROPOSAL = 'PROPOSAL ONLY. Replace API_KEY = "sk_live_12345EXAMPLESECRETKEY" with an env lookup.'

calls: list[tuple[str, object]] = []


class FakeResult:
    def __init__(self, final_output, last_agent_name=None):
        self.final_output = final_output
        self.last_agent = type("A", (), {"name": last_agent_name})()


def install_stub(desk_output, reviewer_table=None):
    """Replace the one model boundary. `desk_output` is what the Desk's run returns."""
    calls.clear()
    table = reviewer_table or {
        SECURITY_REVIEWER_NAME: SECURITY_RAW,
        TESTS_REVIEWER_NAME: TESTS_RAW,
        STYLE_REVIEWER_NAME: STYLE_RAW,
    }

    async def fake_run(agent=None, input=None, *, starting_agent=None, **kwargs):
        resolved = starting_agent if agent is None else agent
        sent = kwargs.get("input", input)
        calls.append((resolved.name, sent))
        await asyncio.sleep(0.01)
        if resolved.name == desk.DESK_NAME:
            if isinstance(desk_output, BaseException):
                raise desk_output
            return desk_output
        return FakeResult(table[resolved.name])

    review_runner.Runner.run = fake_run
    desk.Runner.run = fake_run


def context() -> ReviewContext:
    return ReviewContext(
        repo="code-review-desk", language="Python", ruleset_id="python-default"
    )


# --- the agent's own wiring ------------------------------------------------


def test_desk_agent_is_wired_per_plan_md() -> None:
    agent = desk.build_desk()
    assert agent.name == "Desk"
    assert isinstance(agent.instructions, str), "plan.md §2: static instructions"
    assert agent.output_type is Report
    assert [t.name for t in agent.tools] == [MERGE_TOOL_NAME], "merge must be a tool"
    assert len(agent.handoffs) == 1, "remediation must be a handoff, not a tool"
    handoff_target = agent.handoffs[0]
    name = getattr(handoff_target, "name", None) or getattr(
        getattr(handoff_target, "agent", None), "name", None
    )
    assert name == REMEDIATION_SPECIALIST_NAME or REMEDIATION_SPECIALIST_NAME in str(name)
    assert len(agent.output_guardrails) == 1, "FR-8 guardrail must be attached"
    assert agent.model_settings.max_tokens == desk.DESK_MAX_TOKENS


def test_desk_prompt_contains_no_repository_name() -> None:
    # Article IV.2: grepping the prompts finds no repo name.
    assert "code-review-desk" not in desk.DESK_INSTRUCTIONS


# --- the deterministic steps stay deterministic ----------------------------


def test_split_and_gather_run_before_the_desk_and_are_not_tools() -> None:
    install_stub(FakeResult(Report(findings=MERGED, footer=[], remediation_proposed=False)))
    asyncio.run(desk.run_review(DIFF, context()))

    invoked = [name for name, _ in calls]
    # All three reviewers ran before the Desk's own run.
    assert invoked.index(desk.DESK_NAME) > max(
        invoked.index(n)
        for n in (SECURITY_REVIEWER_NAME, TESTS_REVIEWER_NAME, STYLE_REVIEWER_NAME)
    )
    # The Desk has no tool for splitting or for running reviewers — it cannot
    # decline to do either, because both already happened.
    tool_names = [t.name for t in desk.build_desk().tools]
    assert "split_diff_by_file" not in tool_names
    assert "run_all_reviewers" not in tool_names
    assert tool_names == [MERGE_TOOL_NAME]


def test_desk_input_carries_findings_and_the_precomputed_decision() -> None:
    install_stub(FakeResult(Report(findings=MERGED, footer=[], remediation_proposed=False)))
    asyncio.run(desk.run_review(DIFF, context()))

    sent = next(payload for name, payload in calls if name == desk.DESK_NAME)
    parsed = json.loads(sent)
    assert set(parsed) == {MERGE_INPUT_KEY, "critical_security_finding_present"}
    assert parsed["critical_security_finding_present"] is True
    assert len(parsed[MERGE_INPUT_KEY]) == 3
    assert {f["source_reviewer"] for f in parsed[MERGE_INPUT_KEY]} == {
        SECURITY_REVIEWER_NAME,
        TESTS_REVIEWER_NAME,
        STYLE_REVIEWER_NAME,
    }


def test_footer_is_overwritten_from_real_measurements() -> None:
    # The model emits a bogus footer; the run's own numbers must win.
    lying_footer = [
        {"reviewer": "SecurityReviewer", "ms": 999999, "tokens": 12345, "partial": False}
    ]
    install_stub(
        FakeResult(
            Report.model_validate(
                {"findings": [f.model_dump() for f in MERGED], "footer": lying_footer,
                 "remediation_proposed": False}
            )
        )
    )
    report, _ = asyncio.run(desk.run_review(DIFF, context()))

    assert len(report.footer) == 3, "one row per reviewer, from the run"
    # This file stubs Runner.run, so the real runner never fires the run-level
    # hooks: there is no measurement, so the footer claims none (None), and the
    # model's invented 12345 is discarded. Real token flow is tested against the
    # real runner in test_fr10_hooks.py.
    assert all(row.tokens is None for row in report.footer), "Article VII.3: never invented"
    assert all(row.ms != 999999 for row in report.footer)


# --- the handoff path ------------------------------------------------------


def test_handoff_path_yields_a_report_around_the_proposal() -> None:
    install_stub(FakeResult(PROPOSAL, last_agent_name=REMEDIATION_SPECIALIST_NAME))
    report, error = asyncio.run(desk.run_review(DIFF, context()))

    assert error is None
    assert report.remediation_proposed is True
    assert report.remediation_proposal == PROPOSAL
    assert len(report.footer) == 3


def test_missing_remediation_when_critical_present_is_noted() -> None:
    install_stub(FakeResult(Report(findings=MERGED, footer=[], remediation_proposed=False)))
    report, _ = asyncio.run(desk.run_review(DIFF, context()))
    assert any("no remediation proposal" in note for note in report.notes)


# --- failure semantics (ported from the FR-6 orchestration suite) ----------


def test_failed_reviewer_still_produces_a_partial_report() -> None:
    table = {
        SECURITY_REVIEWER_NAME: SECURITY_RAW,
        TESTS_REVIEWER_NAME: TESTS_RAW,
        STYLE_REVIEWER_NAME: STYLE_RAW,
    }
    install_stub(
        FakeResult(PROPOSAL, last_agent_name=REMEDIATION_SPECIALIST_NAME), table
    )

    # Make Style fail while the other two succeed.
    original = review_runner.Runner.run

    async def failing(agent=None, input=None, *, starting_agent=None, **kwargs):
        resolved = starting_agent if agent is None else agent
        if resolved.name == STYLE_REVIEWER_NAME:
            raise RuntimeError("connection reset")
        return await original(agent, input, starting_agent=starting_agent, **kwargs)

    review_runner.Runner.run = failing
    desk.Runner.run = failing

    report, error = asyncio.run(desk.run_review(DIFF, context()))
    assert error is None
    assert report.is_partial is True
    assert any("StyleReviewer did not complete" in note for note in report.notes)
    # The review still reached remediation on Security's critical.
    assert report.remediation_proposed is True


def test_empty_and_malformed_diffs_report_plainly() -> None:
    install_stub(FakeResult(Report(findings=[], footer=[], remediation_proposed=False)))
    for bad in ("", "   \n", "this is not a diff at all"):
        report, error = asyncio.run(desk.run_review(bad, context()))
        assert report is None
        assert isinstance(error, str) and error
    assert calls == [], "no model should be called for an unusable diff"


# --- FR-8 ------------------------------------------------------------------


def test_final_sweep_refuses_a_dirty_proposal() -> None:
    install_stub(FakeResult(DIRTY_PROPOSAL, last_agent_name=REMEDIATION_SPECIALIST_NAME))
    try:
        asyncio.run(desk.run_review(DIFF, context()))
    except ReportRefused as exc:
        assert exc.hits
        return
    raise AssertionError("a credential-shaped proposal was not refused")


def test_clean_report_passes_the_sweep_untouched() -> None:
    clean = Report(findings=MERGED, footer=[], remediation_proposed=False)
    install_stub(FakeResult(clean))
    report, error = asyncio.run(desk.run_review(DIFF, context()))
    assert error is None
    assert [f.message for f in report.findings] == [f.message for f in MERGED]


def test_run_review_does_not_swallow_the_sdk_tripwire() -> None:
    install_stub(OutputGuardrailTripwireTriggered.__new__(OutputGuardrailTripwireTriggered))
    try:
        asyncio.run(desk.run_review(DIFF, context()))
    except OutputGuardrailTripwireTriggered:
        return
    raise AssertionError("the tripwire must reach the entry point, not be caught in desk")


def test_entry_point_catches_both_refusal_types_and_shows_no_findings(capsys=None) -> None:
    import io
    import contextlib

    for raised in (
        ReportRefused([]),
        OutputGuardrailTripwireTriggered.__new__(OutputGuardrailTripwireTriggered),
    ):
        async def boom(*args, **kwargs):
            raise raised

        original = desk.run_review
        desk.run_review = boom
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = asyncio.run(entry._show_or_refuse(DIFF))
            printed = buffer.getvalue()
        finally:
            desk.run_review = original

        assert code == 2, raised
        assert REFUSAL_MESSAGE.split(".")[0] in printed
        # No part of a report may appear alongside a refusal.
        assert "finding(s)" not in printed
        assert "reviewer" not in printed


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
