"""Structural tests for FR-9's three controls. No live model call.

9a — the Style Reviewer's get_ruleset call is forced, by name, and only for Style.
9b — get_ruleset absorbs every foreseeable failure; nothing raises into the runner.
     (list_changed_files / get_file_diff do not exist — see the note in 9b below.)
9c — REVIEWER_MAX_TURNS reaches every reviewer run, and a ceiling hit becomes a
     partial review in the assembled Report rather than a crash.

Only `Runner.run` is stubbed, and only in 9c. Run with `python test_fr9.py`.
"""

import asyncio
from pathlib import Path

from agents.exceptions import MaxTurnsExceeded
from agents.models.chatcmpl_converter import Converter
from agents.tool_context import ToolContext

import desk
import review_runner
import tools
from config import REVIEWER_MAX_TURNS
from finding import Finding
from report import Report
from review_context import ReviewContext
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
    build_security_reviewer,
    build_style_instructions,
    build_style_reviewer,
    build_tests_reviewer,
)
from test_desk_agent import DIFF, FakeResult, SECURITY_RAW, STYLE_RAW, TESTS_RAW


def context(ruleset_id: str = "python-default") -> ReviewContext:
    return ReviewContext(
        repo="code-review-desk", language="Python", ruleset_id=ruleset_id
    )


# ---------------------------------------------------------------------------
# 9a — required tool call
# ---------------------------------------------------------------------------


def test_9a_style_forces_get_ruleset_by_name() -> None:
    style = build_style_reviewer()
    choice = style.model_settings.tool_choice
    # Pinned to the one tool — not "required" (any tool would satisfy it) and
    # not "auto" (optional).
    assert choice == "get_ruleset", choice
    assert choice not in ("required", "auto", "none")
    # And it names a tool the agent actually has.
    assert choice in [t.name for t in style.tools]


def test_9a_forced_choice_reaches_the_wire_as_a_named_function() -> None:
    # What the SDK actually sends to a Chat Completions endpoint for this setting.
    wire = Converter.convert_tool_choice(build_style_reviewer().model_settings.tool_choice)
    assert wire == {"type": "function", "function": {"name": "get_ruleset"}}, wire


def test_9a_forced_choice_resets_after_the_first_call() -> None:
    # Without this, the model is forced to call get_ruleset every turn, never
    # emits findings, and every Style run ends in MaxTurnsExceeded.
    assert build_style_reviewer().reset_tool_choice is True


def test_9a_style_settings_otherwise_unchanged() -> None:
    # Replacing ModelSettings wholesale must not drop the explicit settings
    # Article I.4 / VI.1 require.
    settings = build_style_reviewer().model_settings
    assert settings.temperature == 0.1
    assert settings.max_tokens == 4096


def test_9a_security_and_tests_keep_get_ruleset_optional() -> None:
    for agent in (build_security_reviewer(), build_tests_reviewer()):
        assert agent.model_settings.tool_choice is None, agent.name
        assert "get_ruleset" in [t.name for t in agent.tools], agent.name


# ---------------------------------------------------------------------------
# 9b — dedicated error handling for diff/ruleset-reading tools
#
# get_ruleset is the ONLY tool in this codebase. list_changed_files and
# get_file_diff (plan.md §3) were never built: every reviewer receives the full
# diff text as its run input, so there is no per-file lookup tool to fail.
#
# FR-2's get_ruleset checks were run as one-off inline scripts and never saved,
# so there was no existing test to point to. These persist that coverage,
# invoking the real FunctionTool through `on_invoke_tool` — the same path the
# runner uses — so "never raises into the runner" is tested at the boundary.
# ---------------------------------------------------------------------------


def invoke_get_ruleset(review_context) -> str:
    tool_context = ToolContext(
        review_context,
        tool_name="get_ruleset",
        tool_call_id="call_test",
        tool_arguments="{}",
    )
    return asyncio.run(tools.get_ruleset.on_invoke_tool(tool_context, "{}"))


def test_9b_known_ruleset_returns_its_text() -> None:
    text = invoke_get_ruleset(context())
    assert "PY001" in text
    assert not text.startswith(tools.UNAVAILABLE_PREFIX)


def test_9b_unknown_empty_and_traversal_ids_report_unavailable() -> None:
    for ruleset_id in ("nope", "", "../.env", "..\\.env", "python-default.txt"):
        text = invoke_get_ruleset(context(ruleset_id))
        assert text.startswith(tools.UNAVAILABLE_PREFIX), (ruleset_id, text)


def test_9b_missing_context_reports_unavailable() -> None:
    assert invoke_get_ruleset(None).startswith(tools.UNAVAILABLE_PREFIX)


def _with_read_text(replacement, call):
    original = Path.read_text
    Path.read_text = replacement
    try:
        return call()
    finally:
        Path.read_text = original


def test_9b_unreadable_file_reports_unavailable() -> None:
    def denied(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    text = _with_read_text(denied, lambda: invoke_get_ruleset(context()))
    assert text.startswith(tools.UNAVAILABLE_PREFIX)
    assert "could not be read" in text


def test_9b_undecodable_file_reports_unavailable() -> None:
    def garbled(self, *args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    text = _with_read_text(garbled, lambda: invoke_get_ruleset(context()))
    assert text.startswith(tools.UNAVAILABLE_PREFIX)


def test_9b_empty_file_reports_unavailable() -> None:
    text = _with_read_text(lambda self, *a, **k: "  \n", lambda: invoke_get_ruleset(context()))
    assert text.startswith(tools.UNAVAILABLE_PREFIX)
    assert "is empty" in text


def test_9b_deleted_ruleset_file_still_yields_a_usable_review_setup() -> None:
    # spec.md §4.9's acceptance edge case: the ruleset is deleted. Simulated by
    # pointing the tool at a directory that does not exist — nothing is deleted
    # from the working tree.
    original = tools.RULESETS_DIR
    tools.RULESETS_DIR = original / "__does_not_exist__"
    try:
        assert tools.available_ruleset_ids() == []
        text = invoke_get_ruleset(context())
        assert text.startswith(tools.UNAVAILABLE_PREFIX)
        # The model gets an actionable sentence, not a bare error.
        assert "general best practice" in text
        # The Style prompt still builds, with the neutral fallback and no
        # fabricated ruleset description (spec.md §4.4).
        prompt = build_style_instructions(context())
        assert "No ruleset could be resolved" in prompt
        assert "'python-default'" not in prompt
    finally:
        tools.RULESETS_DIR = original


# ---------------------------------------------------------------------------
# 9c — ceiling reported as a partial review
# ---------------------------------------------------------------------------

calls: list[dict] = []


def install_stub(failures: dict, desk_output) -> None:
    """Stub the one model boundary. `failures` maps reviewer name -> exception."""
    calls.clear()
    table = {
        SECURITY_REVIEWER_NAME: SECURITY_RAW,
        TESTS_REVIEWER_NAME: TESTS_RAW,
        STYLE_REVIEWER_NAME: STYLE_RAW,
    }

    async def fake_run(agent=None, input=None, *, starting_agent=None, **kwargs):
        resolved = starting_agent if agent is None else agent
        calls.append({"agent": resolved.name, "max_turns": kwargs.get("max_turns")})
        await asyncio.sleep(0.01)
        if resolved.name == desk.DESK_NAME:
            return desk_output
        if resolved.name in failures:
            raise failures[resolved.name]
        return FakeResult(table[resolved.name])

    review_runner.Runner.run = fake_run
    desk.Runner.run = fake_run


def ceiling() -> MaxTurnsExceeded:
    return MaxTurnsExceeded(f"Max turns ({REVIEWER_MAX_TURNS}) exceeded")


def test_9c_every_reviewer_run_carries_the_ceiling() -> None:
    install_stub({}, desk_output=None)
    asyncio.run(review_runner.run_all_reviewers(DIFF, context()))
    reviewer_calls = [c for c in calls if c["agent"] != desk.DESK_NAME]
    assert len(reviewer_calls) == 3
    assert {c["agent"] for c in reviewer_calls} == {
        SECURITY_REVIEWER_NAME,
        TESTS_REVIEWER_NAME,
        STYLE_REVIEWER_NAME,
    }
    assert all(c["max_turns"] == REVIEWER_MAX_TURNS == 6 for c in reviewer_calls)


def test_9c_ceiling_marks_only_that_reviewer_failed() -> None:
    install_stub({TESTS_REVIEWER_NAME: ceiling()}, desk_output=None)
    group = asyncio.run(review_runner.run_all_reviewers(DIFF, context()))

    by_name = {o.reviewer: o for o in group.outcomes}
    hit = by_name[TESTS_REVIEWER_NAME]
    assert hit.failed is True
    assert hit.findings == []
    assert "ceiling" in hit.error and "partial" in hit.error
    assert str(REVIEWER_MAX_TURNS) in hit.error

    # The other two are untouched — same findings they would have produced alone,
    # stamped with their own names.
    for name, raw in ((SECURITY_REVIEWER_NAME, SECURITY_RAW), (STYLE_REVIEWER_NAME, STYLE_RAW)):
        outcome = by_name[name]
        assert outcome.failed is False, name
        assert outcome.error is None, name
        assert [f.message for f in outcome.findings] == [f.message for f in raw], name
        assert all(f.source_reviewer == name for f in outcome.findings), name

    assert group.is_partial is True
    assert group.failed_reviewers == [TESTS_REVIEWER_NAME]


def test_9c_report_surfaces_the_ceiling_as_a_partial_review() -> None:
    # The Desk itself is stubbed to return a Report (no handoff path); run_review
    # then overwrites footer and notes from the real measurements, which is what
    # this test is checking.
    desk_report = Report(
        findings=[
            Finding(
                file="billing/refunds.py",
                line=2,
                severity="critical",
                message="A hardcoded credential was committed; rotate it and load from env.",
                source_reviewer=SECURITY_REVIEWER_NAME,
            )
        ],
        footer=[],
        remediation_proposed=False,
    )
    install_stub(
        {TESTS_REVIEWER_NAME: ceiling()},
        desk_output=FakeResult(desk_report, last_agent_name=desk.DESK_NAME),
    )

    report, error = asyncio.run(desk.run_review(DIFF, context()))

    assert error is None
    assert isinstance(report, Report), "a ceiling hit must still produce a report"
    assert report.is_partial is True

    rows = {row.reviewer: row for row in report.footer}
    assert rows[TESTS_REVIEWER_NAME].partial is True
    assert rows[SECURITY_REVIEWER_NAME].partial is False
    assert rows[STYLE_REVIEWER_NAME].partial is False

    ceiling_notes = [n for n in report.notes if n.startswith(TESTS_REVIEWER_NAME)]
    assert len(ceiling_notes) == 1, report.notes
    assert "ceiling" in ceiling_notes[0] and "partial review" in ceiling_notes[0]
    # Neither of the successful reviewers is mentioned as failing.
    assert not any(n.startswith(SECURITY_REVIEWER_NAME) for n in report.notes)
    assert not any(n.startswith(STYLE_REVIEWER_NAME) for n in report.notes)


def test_9c_ceiling_on_security_suppresses_remediation_and_says_why() -> None:
    # If Security is the reviewer that ran out of turns, its critical never exists,
    # so no handoff happens — and the report has to say Security is missing, or a
    # reader would take "no remediation" to mean "no security problem".
    desk_report = Report(findings=[], footer=[], remediation_proposed=False)
    install_stub(
        {SECURITY_REVIEWER_NAME: ceiling()},
        desk_output=FakeResult(desk_report, last_agent_name=desk.DESK_NAME),
    )

    report, error = asyncio.run(desk.run_review(DIFF, context()))

    assert error is None
    assert report.remediation_proposed is False
    assert report.is_partial is True
    assert any(
        n.startswith(SECURITY_REVIEWER_NAME) and "ceiling" in n for n in report.notes
    ), report.notes


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
