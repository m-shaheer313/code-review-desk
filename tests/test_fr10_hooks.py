"""Structural tests for FR-10 — run-level and agent-level hooks. No live model call.

WHY THIS FILE DOES NOT STUB `Runner.run`. Hooks are fired by the real runner.
Replacing `Runner.run` would bypass them entirely and every hook test would pass
vacuously. Instead, the REAL runner runs the REAL reviewer agents with their REAL
hooks, tools and output schemas; only the *Model* is scripted (`ScriptedModel`),
returning canned responses with known token usage. It is injected through
`RunConfig(model=...)` — FR-7's own run-level override — so no agent definition
is touched.

Because the scripted usage is known exactly, the footer assertions check exact
token totals: a placeholder 0, an estimate, or a mixed-up reviewer would all fail.

Run with `pytest tests/test_fr10_hooks.py`, or standalone: `python tests/test_fr10_hooks.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import asyncio
import json

from agents import RunConfig, Runner, Usage
from agents.items import ModelResponse
from agents.models.interface import Model
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from desk import build_footer
from hooks import ReviewerRunHooks, SecurityAgentHooks
from review_context import ReviewContext
from review_runner import run_all_reviewers
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
    build_security_reviewer,
    build_style_reviewer,
    build_tests_reviewer,
)

DIFF = """diff --git a/billing/refunds.py b/billing/refunds.py
--- a/billing/refunds.py
+++ b/billing/refunds.py
@@ -1,2 +1,4 @@
 import logging
+from decimal import *
+def ProcessRefund(order_id):
+    return None
"""

# Per agent: one (input, output) token pair per model call, in order.
# Security calls get_ruleset (optional for it), then answers; Tests answers
# directly. Style is TWO runs (FR-9a): its lookup step makes the forced
# get_ruleset call, then the Style Reviewer answers from the ruleset it was given.
SCRIPT_USAGE = {
    "security": [(100, 20), (150, 30)],  # 120 + 180 = 300
    "tests": [(80, 10)],  # 90
    "style_lookup": [(90, 15)],  # 105  — run 1 of Style
    "style": [(200, 25)],  # 225        — run 2 of Style; Style total 330
}
EXPECTED_TOKENS = {
    SECURITY_REVIEWER_NAME: 300,
    TESTS_REVIEWER_NAME: 90,
    STYLE_REVIEWER_NAME: 330,
}

FINDINGS = {
    "security": [
        {
            "file": "billing/refunds.py",
            "line": 2,
            "severity": "critical",
            "message": "Hardcoded credential committed.",
            "source_reviewer": "",
        }
    ],
    "tests": [],
    "style": [
        {
            "file": "billing/refunds.py",
            "line": 2,
            "severity": "minor",
            "message": "PY003: no wildcard imports.",
            "source_reviewer": "",
        }
    ],
}


def _which(system_instructions: str | None) -> str:
    text = system_instructions or ""
    if text.startswith("You are a security reviewer"):
        return "security"
    if text.startswith("You are a test coverage reviewer"):
        return "tests"
    if text.startswith("You are a code style reviewer"):
        return "style"
    if text.startswith("You are the ruleset lookup step"):
        return "style_lookup"
    raise AssertionError(f"unexpected agent prompt: {text[:60]!r}")


def _has_tool_output(model_input) -> bool:
    if isinstance(model_input, str):
        return False
    return any(
        (item.get("type") if isinstance(item, dict) else getattr(item, "type", None))
        == "function_call_output"
        for item in model_input
    )


def _usage(input_tokens: int, output_tokens: int) -> Usage:
    return Usage(
        requests=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


class ScriptedModel(Model):
    """A Model that answers from a script instead of the network.

    `fail_after` maps a reviewer key to how many successful calls it gets before
    the next call raises — to test a run that dies mid-way.
    """

    def __init__(self, fail_after: dict[str, int] | None = None):
        self.fail_after = fail_after or {}
        self.calls: dict[str, int] = {"security": 0, "tests": 0, "style": 0, "style_lookup": 0}

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ) -> ModelResponse:
        key = _which(system_instructions)
        index = self.calls[key]
        if key in self.fail_after and index >= self.fail_after[key]:
            raise RuntimeError(f"scripted failure for {key} on call {index + 1}")
        self.calls[key] += 1
        await asyncio.sleep(0.01)

        usage = _usage(*SCRIPT_USAGE[key][index])
        wants_tool_first = key in ("security", "style_lookup")
        if wants_tool_first and not _has_tool_output(input):
            output = [
                ResponseFunctionToolCall(
                    id=f"fc_{key}",
                    call_id=f"call_{key}",
                    name="get_ruleset",
                    arguments="{}",
                    type="function_call",
                )
            ]
        else:
            output = [
                ResponseOutputMessage(
                    id=f"msg_{key}",
                    content=[
                        ResponseOutputText(
                            text=json.dumps({"response": FINDINGS[key]}),
                            type="output_text",
                            annotations=[],
                        )
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ]
        return ModelResponse(output=output, usage=usage, response_id=None)

    def stream_response(self, *args, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError


def context() -> ReviewContext:
    return ReviewContext(
        repo="code-review-desk", language="Python", ruleset_id="python-default"
    )


def run_config(model: ScriptedModel) -> RunConfig:
    return RunConfig(model=model, tracing_disabled=True)


def group_with(model: ScriptedModel):
    return asyncio.run(run_all_reviewers(DIFF, context(), run_config=run_config(model)))


def kinds(events) -> list[str]:
    return [event.kind for event in events]


# ---------------------------------------------------------------------------
# Run-level hooks: real token usage for all three reviewers
# ---------------------------------------------------------------------------


def test_all_three_reviewers_are_observed_by_run_level_hooks() -> None:
    group = group_with(ScriptedModel())
    by_name = {o.reviewer: o for o in group.outcomes}
    assert set(by_name) == set(EXPECTED_TOKENS)
    assert by_name[SECURITY_REVIEWER_NAME].llm_calls == 2
    assert by_name[SECURITY_REVIEWER_NAME].tool_calls == 1
    assert by_name[TESTS_REVIEWER_NAME].llm_calls == 1
    assert by_name[TESTS_REVIEWER_NAME].tool_calls == 0
    assert by_name[STYLE_REVIEWER_NAME].llm_calls == 2
    assert by_name[STYLE_REVIEWER_NAME].tool_calls == 1  # the forced get_ruleset
    assert all(not o.failed for o in group.outcomes)


def test_footer_tokens_are_the_real_per_run_usage() -> None:
    group = group_with(ScriptedModel())
    footer = {row.reviewer: row for row in build_footer(group)}
    assert len(footer) == 3
    for reviewer, expected in EXPECTED_TOKENS.items():
        # Exact: not 0 (the old placeholder), not an estimate, and not another
        # reviewer's number — each run's Usage is its own.
        assert footer[reviewer].tokens == expected, (reviewer, footer[reviewer].tokens)
    # Latency still comes from the manual bracket, and is real too.
    assert all(row.ms > 0 for row in footer.values())


def test_failed_reviewer_keeps_its_row_with_the_tokens_it_really_spent() -> None:
    # Security's first call succeeds (120 tokens, the get_ruleset call); its
    # second call raises. spec.md §4.10: the row must still appear, marked
    # incomplete, with whatever real data exists.
    group = group_with(ScriptedModel(fail_after={"security": 1}))
    by_name = {o.reviewer: o for o in group.outcomes}
    security = by_name[SECURITY_REVIEWER_NAME]
    assert security.failed is True
    assert security.tokens == 120  # partial and real — not 0, not None, not 300

    footer = {row.reviewer: row for row in build_footer(group)}
    assert footer[SECURITY_REVIEWER_NAME].partial is True
    assert footer[SECURITY_REVIEWER_NAME].tokens == 120
    # The other two are unaffected.
    assert footer[TESTS_REVIEWER_NAME].tokens == 90
    assert footer[STYLE_REVIEWER_NAME].tokens == 330


def test_run_that_dies_before_any_model_response_reports_zero_measured() -> None:
    # Tests fails on its very first call: the run context existed (the hooks saw
    # on_agent_start), no response was ever received, so 0 tokens is a genuine
    # measurement here — not the old placeholder.
    group = group_with(ScriptedModel(fail_after={"tests": 0}))
    tests = {o.reviewer: o for o in group.outcomes}[TESTS_REVIEWER_NAME]
    assert tests.failed is True
    assert tests.tokens == 0


def test_unobserved_run_reports_none_not_zero() -> None:
    hooks = ReviewerRunHooks()
    assert hooks.stats.tokens is None  # no context seen -> no number to claim


# ---------------------------------------------------------------------------
# Agent-level hooks: Security only
# ---------------------------------------------------------------------------


def test_only_security_carries_agent_level_hooks() -> None:
    assert isinstance(build_security_reviewer().hooks, SecurityAgentHooks)
    assert build_tests_reviewer().hooks is None
    assert build_style_reviewer().hooks is None
    # Fresh per build: one review's event log cannot leak into the next.
    assert build_security_reviewer().hooks is not build_security_reviewer().hooks


def test_full_review_agent_events_appear_for_security_only() -> None:
    group = group_with(ScriptedModel())
    by_name = {o.reviewer: o for o in group.outcomes}
    assert kinds(by_name[SECURITY_REVIEWER_NAME].agent_events) == [
        "start",
        "llm_start",
        "llm_end",
        "tool_start",
        "tool_end",
        "llm_start",
        "llm_end",
        "end",
    ]
    tool_events = [e for e in by_name[SECURITY_REVIEWER_NAME].agent_events if e.kind == "tool_start"]
    assert [e.detail for e in tool_events] == ["get_ruleset"]
    # Tests and Style both ran (the run-level hooks prove it) yet produced no
    # agent-level events — including Style, which made a tool call.
    assert by_name[TESTS_REVIEWER_NAME].agent_events == []
    assert by_name[STYLE_REVIEWER_NAME].agent_events == []
    assert by_name[STYLE_REVIEWER_NAME].tool_calls == 1


def test_security_alone_fires_agent_events() -> None:
    security = build_security_reviewer()
    asyncio.run(
        Runner.run(security, DIFF, context=context(), run_config=run_config(ScriptedModel()))
    )
    assert kinds(security.hooks.events)[0] == "start"
    assert kinds(security.hooks.events)[-1] == "end"
    assert security.hooks.events[-1].detail == "findings=1"


def test_tests_and_style_alone_fire_no_agent_events() -> None:
    # Security's hooks exist in this process but Security never runs: nothing
    # Tests or Style do may reach them.
    security = build_security_reviewer()
    model = ScriptedModel()
    for agent in (build_tests_reviewer(), build_style_reviewer()):
        run_hooks = ReviewerRunHooks()
        asyncio.run(
            Runner.run(agent, DIFF, context=context(), run_config=run_config(model), hooks=run_hooks)
        )
        assert run_hooks.stats.completed is True, agent.name  # it really ran
    assert security.hooks.events == []


def test_agent_events_never_contain_finding_text() -> None:
    # Article II.4: the event log records kinds, tool names and counts only.
    group = group_with(ScriptedModel())
    security = {o.reviewer: o for o in group.outcomes}[SECURITY_REVIEWER_NAME]
    dumped = " ".join(f"{e.kind} {e.detail}" for e in security.agent_events)
    assert "Hardcoded credential" not in dumped
    assert "PY001" not in dumped  # nor ruleset text from the tool result


# ---------------------------------------------------------------------------
# FR-10's acceptance criterion: what each kind of hook sees
# ---------------------------------------------------------------------------


def test_what_agent_level_hooks_see_that_run_level_hooks_do_not() -> None:
    """The concrete difference, demonstrated rather than asserted in prose.

    SCOPE is the real difference, and it is not granularity of event types. In
    this SDK both hook classes receive the same kinds of callback — agent
    start/end, each model call, each tool call, handoffs:

    - RUN-LEVEL hooks are passed to one `Runner.run(...)` call and see every agent
      that executes *in that run*, and nothing outside it. Our reviewer runs each
      contain exactly one agent, so one run-hooks instance == one reviewer run.
      This is why they are the right place for the footer: one instance per run,
      one Usage per run, three rows.
    - AGENT-LEVEL hooks live on one agent definition and see that agent *in every
      run it takes part in* — and no other agent, even inside the same run. They
      follow the agent, not the run.

    WHAT WE RECORD differs by design, on top of that: the run-level hooks keep
    coarse aggregates (call counts, token totals — enough for a footer row), while
    Security's agent-level hooks keep an ordered event log (start, each model
    call, each named tool call, end with a finding count) — enough to reconstruct
    what Security actually did, step by step.

    The demonstration: run the SAME Security agent object in two separate runs.
    Each run-level hooks instance sees only its own run. The agent-level hooks
    see both, in order — something no single run's hooks can observe.
    """
    security = build_security_reviewer()
    first_run_hooks, second_run_hooks = ReviewerRunHooks(), ReviewerRunHooks()

    async def two_runs():
        await Runner.run(
            security, DIFF, context=context(),
            run_config=run_config(ScriptedModel()), hooks=first_run_hooks,
        )
        await Runner.run(
            security, DIFF, context=context(),
            run_config=run_config(ScriptedModel()), hooks=second_run_hooks,
        )

    asyncio.run(two_runs())

    # Run-level: each instance saw one run — coarse totals, its own run only.
    for run_hooks in (first_run_hooks, second_run_hooks):
        assert run_hooks.stats.llm_calls == 2
        assert run_hooks.stats.tool_calls == 1
        assert run_hooks.stats.tokens == 300
    assert first_run_hooks.stats.usages[0] is not second_run_hooks.stats.usages[0]

    # Agent-level: one log spanning both runs, step by step, in order.
    assert kinds(security.hooks.events).count("start") == 2
    assert kinds(security.hooks.events).count("end") == 2
    assert kinds(security.hooks.events) == [
        "start", "llm_start", "llm_end", "tool_start", "tool_end",
        "llm_start", "llm_end", "end",
    ] * 2
    # And the finer detail the run-level aggregates do not keep: which tool,
    # and per-call token counts in call order.
    llm_ends = [e.detail for e in security.hooks.events if e.kind == "llm_end"]
    assert llm_ends == ["tokens=120", "tokens=180"] * 2


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
