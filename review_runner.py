"""Concurrent reviewer orchestration (FR-5).

The three reviewers are launched together in ONE `asyncio.gather` and awaited as a
group — never started and awaited one after another (Article V.1). A sequential
implementation that produces correct findings does not satisfy this requirement.

`return_exceptions=True` is load-bearing (plan.md §5): without it, the first
reviewer to raise would cancel the `gather` and take the other two in-flight
reviewers down with it, violating spec.md §4.5's isolation rule. Each result is
inspected individually afterwards; a failed reviewer contributes an empty findings
list and is marked partial rather than aborting the review.

Timing is recorded twice, because FR-5's acceptance criterion needs both numbers:
the group's wall-clock span around the `gather`, and each reviewer's own elapsed
time. Concurrency shows up as group ≈ slowest reviewer, while the sum of the three
is clearly larger.
"""

import asyncio
import time
from dataclasses import dataclass, field, replace

from agents import Agent, RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded

from config import REVIEWER_MAX_TURNS, build_model
from finding import Finding
from hooks import AgentEvent, ReviewerRunHooks
from review_context import ReviewContext
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
    build_security_reviewer,
    build_style_reviewer,
    build_tests_reviewer,
)

# Single source of truth for these names is `reviewers.py`, where the agents are
# defined — `Finding.source_reviewer` and FR-6's handoff decision both compare
# against them.
SECURITY = SECURITY_REVIEWER_NAME
TESTS = TESTS_REVIEWER_NAME
STYLE = STYLE_REVIEWER_NAME


@dataclass
class ReviewerOutcome:
    """One reviewer's contribution to the review."""

    reviewer: str
    findings: list[Finding] = field(default_factory=list)
    # Authoritative latency: the manual monotonic bracket around Runner.run, the
    # same span the group's gather is measured over (FR-5). See _timed_run.
    elapsed_ms: int = 0
    failed: bool = False
    # Short, user-safe description of the failure — the exception type and its
    # message, never a traceback (Article VIII.2). None when the run succeeded.
    error: str | None = None
    # FR-10, from the run-level hooks — real usage read from the run context.
    # None means the hooks never saw the run's context, NOT "zero tokens".
    tokens: int | None = None
    llm_calls: int = 0
    tool_calls: int = 0
    # FR-10's agent-level event log. Populated only for an agent that carries
    # agent-level hooks (Security); empty for the other two by design.
    agent_events: list[AgentEvent] = field(default_factory=list)


@dataclass
class GroupOutcome:
    """The result of one concurrent reviewer group."""

    outcomes: list[ReviewerOutcome]
    group_elapsed_ms: int

    @property
    def sum_individual_ms(self) -> int:
        """The "sum of three" figure to contrast with `group_elapsed_ms`."""
        return sum(outcome.elapsed_ms for outcome in self.outcomes)

    @property
    def slowest_ms(self) -> int:
        return max((outcome.elapsed_ms for outcome in self.outcomes), default=0)

    @property
    def failed_reviewers(self) -> list[str]:
        return [outcome.reviewer for outcome in self.outcomes if outcome.failed]

    @property
    def is_partial(self) -> bool:
        """True when at least one reviewer failed — the report built from this
        group must be marked partial (spec.md failure semantics, Article VIII.3)."""
        return bool(self.failed_reviewers)

    @property
    def all_findings(self) -> list[Finding]:
        """Every finding from every reviewer that completed, unmerged and
        unordered — Merge (FR-6) is what dedupes and orders them."""
        return [f for outcome in self.outcomes for f in outcome.findings]


async def run_reviewer_with_override(
    agent: Agent[ReviewContext],
    diff_text: str,
    context: ReviewContext,
    model_name: str,
    run_config: RunConfig | None = None,
):
    """Re-run one reviewer under a different model, at the RUN level (FR-7).

    This is the single sanctioned exception to "configured at the agent level"
    (Article I.3), and it is sanctioned only because it happens here: the model
    travels in the run's configuration. `agent.model` is never read and never
    assigned — the same agent object can be run under its own model and under this
    override, and nothing about the agent differs between the two runs.

    Returns a fresh `RunResult`: an entirely independent second review, not a
    mutation of the first (spec.md §4.7's edge case).

    The override is passed as a Model object rather than the name string, because
    `RunConfig.model` resolves a bare string through `RunConfig.model_provider` —
    which defaults to OpenAI's provider and would send the run to the wrong API.
    """
    override_model = build_model(model_name)
    if run_config is None:
        # Matches the rest of the codebase: tracing off until FR-13 supplies a
        # configured RunConfig. Set per run, never globally.
        run_config = RunConfig(tracing_disabled=True)
    # `replace` keeps any trace grouping the caller set up and swaps only the model.
    run_config = replace(run_config, model=override_model)

    return await Runner.run(
        agent,
        diff_text,
        context=context,
        max_turns=REVIEWER_MAX_TURNS,  # a cheaper model is not a looser ceiling
        run_config=run_config,
    )


async def _timed_run(
    reviewer: str,
    agent: Agent[ReviewContext],
    diff_text: str,
    context: ReviewContext,
    run_config: RunConfig,
    elapsed: dict[str, float],
    hooks: ReviewerRunHooks,
):
    """Run one reviewer, recording its own elapsed time.

    The timing lives in `try/finally` so a reviewer that raises still reports how
    long it ran, while the exception itself still propagates to `gather` — which
    is what makes `return_exceptions=True` meaningful rather than decorative.

    This manual timer stays the footer's source of latency even now that run-level
    hooks exist (FR-10). The hooks' first event fires after `Runner.run` has
    started and `on_agent_end` never fires when a run raises, so a hook-based
    elapsed time would be missing for exactly the failed reviewers spec.md §4.10
    says must still get a footer row — and it would bracket a different span from
    the group's gather, breaking FR-5's group-vs-sum comparison. The hooks own
    token usage; this timer owns latency.
    """
    start = time.monotonic()
    try:
        return await Runner.run(
            agent,
            diff_text,
            context=context,
            max_turns=REVIEWER_MAX_TURNS,  # plan.md §10, per reviewer, not shared
            run_config=run_config,
            hooks=hooks,
        )
    finally:
        elapsed[reviewer] = time.monotonic() - start


def _describe_failure(exc: BaseException) -> str:
    """User-facing reason a reviewer contributed nothing (no traceback).

    A turn-ceiling hit is called out by name as a partial review (spec.md §4.9's
    9c, Article VI.2) rather than surfacing as a bare exception string — the user
    should read "this reviewer ran out of turns", not "something broke".
    """
    if isinstance(exc, MaxTurnsExceeded):
        return (
            f"stopped at its {REVIEWER_MAX_TURNS}-turn ceiling before producing "
            "findings (MaxTurnsExceeded); its findings are missing from this "
            "partial review"
        )
    return f"{type(exc).__name__}: {exc}".strip()


def _findings_from(result, reviewer: str) -> tuple[list[Finding], str | None]:
    """Extract `list[Finding]` from a RunResult, defensively, stamping the source.

    `output_type=list[Finding]` means `final_output` is already a plain list, but
    a reviewer that somehow returns something else must not corrupt the merge —
    it is treated as a failed reviewer instead.

    `source_reviewer` is *overwritten*, not filled in only when blank: this run is
    the authority on which reviewer produced these findings, so whatever the model
    put in that field is discarded (FR-6's handoff decides on it).
    """
    output = getattr(result, "final_output", None)
    if isinstance(output, list) and all(isinstance(f, Finding) for f in output):
        stamped = [
            finding.model_copy(update={"source_reviewer": reviewer})
            for finding in output
        ]
        return stamped, None
    return [], f"reviewer returned {type(output).__name__}, expected list[Finding]"


async def run_all_reviewers(
    diff_text: str,
    context: ReviewContext,
    run_config: RunConfig | None = None,
) -> GroupOutcome:
    """Launch all three reviewers concurrently over the same diff (FR-5).

    Never raises for a reviewer-level failure: a reviewer that raises, times out,
    or exceeds its turn ceiling contributes an empty findings list and is marked
    failed, while the other two run to completion.
    """
    # FR-13 will pass a configured RunConfig carrying the review's trace
    # grouping. Until then, default to tracing off so this needs no backend —
    # set at the run level, never as a global (Article I.2).
    if run_config is None:
        run_config = RunConfig(tracing_disabled=True)

    agents_by_name: list[tuple[str, Agent[ReviewContext]]] = [
        (SECURITY, build_security_reviewer()),
        (TESTS, build_tests_reviewer()),
        (STYLE, build_style_reviewer()),
    ]

    elapsed: dict[str, float] = {}
    # One run-level hooks instance per reviewer run (plan.md §7), so each
    # reviewer's usage is read from its own run context, never a shared counter.
    run_hooks = {name: ReviewerRunHooks() for name, _agent in agents_by_name}

    group_start = time.monotonic()
    results = await asyncio.gather(
        *(
            _timed_run(
                name, agent, diff_text, context, run_config, elapsed, run_hooks[name]
            )
            for name, agent in agents_by_name
        ),
        # Required by plan.md §5: one reviewer's exception must not cancel the
        # other two in-flight reviewers.
        return_exceptions=True,
    )
    group_elapsed = time.monotonic() - group_start

    outcomes: list[ReviewerOutcome] = []
    for (name, agent), result in zip(agents_by_name, results):
        elapsed_ms = round(elapsed.get(name, 0.0) * 1000)
        # Read after the run, success or failure: the hooks hold a live reference
        # to the run's Usage, so a reviewer that raised mid-run still reports the
        # tokens it genuinely spent (spec.md §4.10's failed-reviewer edge case).
        stats = run_hooks[name].stats
        observed = {
            "tokens": stats.tokens,
            "llm_calls": stats.llm_calls,
            "tool_calls": stats.tool_calls,
            "agent_events": list(getattr(agent.hooks, "events", [])),
        }
        if isinstance(result, BaseException):
            outcomes.append(
                ReviewerOutcome(
                    reviewer=name,
                    findings=[],
                    elapsed_ms=elapsed_ms,
                    failed=True,
                    error=_describe_failure(result),
                    **observed,
                )
            )
            continue
        findings, problem = _findings_from(result, name)
        outcomes.append(
            ReviewerOutcome(
                reviewer=name,
                findings=findings,
                elapsed_ms=elapsed_ms,
                failed=problem is not None,
                error=problem,
                **observed,
            )
        )

    return GroupOutcome(outcomes=outcomes, group_elapsed_ms=round(group_elapsed * 1000))
