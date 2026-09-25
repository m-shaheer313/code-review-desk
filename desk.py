"""The Desk — a real Agent, wrapped in deterministic pre-processing (FR-1 → FR-8).

HOW "THE DESK IS AN AGENT" IS RECONCILED WITH "SPLITTING AND CONCURRENCY MUST
STAY DETERMINISTIC":

The deterministic steps moved **upstream of the agent run**, not into it. They are
not tools the Desk may decline to call — they have already happened by the time
the Desk's first token exists:

    split_diff_by_file(diff)          <- no model involved at all (FR-1)
    run_all_reviewers(...)            <- one asyncio.gather (FR-5)
        |
        v
    Runner.run(desk_agent, <those findings as its input>)   <- the model's turn
        |-- calls merge_findings (a real tool call it chooses to make)   FR-6
        |-- hands off to RemediationSpecialist if warranted (real handoff) FR-6
        '-- output_guardrails=[credential check] on the way out           FR-8

So the Desk agent never decides *whether* the diff was split or *whether* the
reviewers ran concurrently; it cannot, because both are finished facts in its
input. Article V.1's "concurrency is graded, not incidental" stays enforced by
construction, and FR-1's "before any model sees the diff" is likewise structural.

What the agent genuinely owns is the part that is model work: calling Merge, and
judging whether a critical security finding warrants the handoff. That is the
discretion plan.md §2 describes, and no more.

TWO VALUES THE MODEL IS NOT TRUSTED WITH. `Report.footer` carries per-reviewer
latency and token counts; a model asked to fill those in would invent them, and
Article VII.3 forbids estimated or hardcoded numbers. So the footer is overwritten
after the run from FR-5's real measurements — the same "stamp it from the run, not
from the model" pattern already used for `Finding.source_reviewer`. The Desk's
instructions tell it to emit an empty footer.
"""

import json

from agents import Agent, ModelSettings, RunConfig, Runner, trace
from agents.exceptions import AgentsException, OutputGuardrailTripwireTriggered
from openai import APIStatusError

from diff_utils import split_diff_by_file
from finding import Finding
from guardrail import ReportRefused, credential_output_guardrail, scan_report
from merge import MERGE_INPUT_KEY, MERGE_TOOL_NAME, as_merge_tool
from remediation import (
    REMEDIATION_SPECIALIST_NAME,
    build_remediation_specialist,
    decide_needs_remediation,
)
from report import Report, ReviewerFooterRow
from review_context import ReviewContext
from review_runner import (
    REVIEW_WORKFLOW_NAME,
    GroupOutcome,
    ReviewerDoneCallback,
    new_request_id,
    review_run_config,
    run_all_reviewers,
    trace_id_for,
)
from reviewers import shared_model

DESK_NAME = "Desk"

# Orchestration, not authorship: the Desk reorganizes and decides, so precision
# beats variety. plan.md §2 says "moderate temperature (e.g. 0.3)"; 0.2 given the
# only judgement call left to it is the handoff.
DESK_TEMPERATURE = 0.2
DESK_MAX_TOKENS = 4096

# One merge tool call, one optional handoff, one final Report — plus headroom.
# Article VI.2 requires the bound to exist.
DESK_MAX_TURNS = 6

DESK_INSTRUCTIONS = (
    "You are the desk of a code review service. Three independent reviewers "
    "(security, tests, style) have ALREADY reviewed a diff concurrently, and their "
    "raw findings are given to you as JSON. You never see the diff itself and must "
    "not ask for it.\n\n"
    f'YOUR INPUT is a JSON object with the keys "{MERGE_INPUT_KEY}" (the raw '
    'findings from all three reviewers, unmerged and unordered) and '
    '"critical_security_finding_present" (a boolean the system has already '
    "computed for you).\n\n"
    "STEP 1 — MERGE. Call the "
    f"{MERGE_TOOL_NAME} tool exactly once, passing the findings array as a JSON "
    f'object of the form {{"{MERGE_INPUT_KEY}": [...]}}. It returns the same '
    "findings deduplicated and ordered by severity. Do not deduplicate or reorder "
    "them yourself, and do not skip this call even when there is only one finding "
    "or none.\n\n"
    "STEP 2 — DECIDE. If critical_security_finding_present is true, hand off to "
    f"the {REMEDIATION_SPECIALIST_NAME} so it can propose a fix in its own voice. "
    "Pass it the merged findings. Hand off exactly once, and only when that flag "
    "is true.\n\n"
    "STEP 3 — REPORT. If you did not hand off, return a Report containing the "
    "merged findings exactly as the merge tool returned them — same wording, same "
    "severities, same order, same source_reviewer values. Set "
    "remediation_proposed to false, leave remediation_proposal empty, and leave "
    "notes empty.\n\n"
    "Leave the footer as an EMPTY ARRAY. The system fills in latency and token "
    "counts from its own measurements; anything you put there is discarded.\n\n"
    "You must never invent a finding, reword one, change a severity, or add "
    "commentary of your own. You are not reviewing code — you are assembling other "
    "reviewers' work. Never repeat a credential value that appears in a finding; "
    "describe it by location and kind only."
)


def build_desk(run_config: RunConfig | None = None) -> Agent[ReviewContext]:
    """The Desk agent (plan.md §2).

    Merge is a real tool the model chooses to call; Remediation is a real handoff
    target; FR-8's guardrail runs on the way out. Static instructions — the Desk's
    job is orchestration, not per-turn personalization.

    `run_config` is the review's grouped config (FR-13). It is handed to the
    Merge tool, whose nested Runner.run would otherwise start from a bare
    default; the Remediation handoff needs nothing, since a handoff is part of
    the Desk's own run.
    """
    return Agent[ReviewContext](
        name=DESK_NAME,
        instructions=DESK_INSTRUCTIONS,
        model=shared_model(),
        model_settings=ModelSettings(
            temperature=DESK_TEMPERATURE,
            max_tokens=DESK_MAX_TOKENS,
        ),
        tools=[as_merge_tool(run_config=run_config)],
        handoffs=[build_remediation_specialist()],
        output_type=Report,
        output_guardrails=[credential_output_guardrail],
    )


def desk_input(findings: list[Finding], critical_security_present: bool) -> str:
    """The Desk's input: the reviewers' raw findings plus the deterministic
    handoff decision. The decision is computed here, not inferred by the model,
    so the handoff cannot fire on a reviewer's own idea of "critical"."""
    return json.dumps(
        {
            MERGE_INPUT_KEY: [f.model_dump() for f in findings],
            "critical_security_finding_present": critical_security_present,
        }
    )


def build_footer(group: GroupOutcome) -> list[ReviewerFooterRow]:
    """One row per reviewer, from the run's own measurements (never the model's).

    `tokens` comes from the run-level hooks' live reference to each run's Usage
    (FR-10, Article VII.3); None when the hooks never saw that run's context. A
    failed reviewer still gets its row, with whatever it genuinely spent.
    """
    return [
        ReviewerFooterRow(
            reviewer=outcome.reviewer,
            ms=outcome.elapsed_ms,
            tokens=outcome.tokens,
            partial=outcome.failed,
        )
        for outcome in group.outcomes
    ]


def _provider_message(exc: APIStatusError) -> str:
    """One-line provider explanation; Gemini returns a list body, OpenAI a dict."""
    body = exc.body
    if isinstance(body, list):
        body = body[0] if body else {}
    error = body.get("error") if isinstance(body, dict) else None
    message = (error or {}).get("message") if isinstance(error, dict) else None
    return " ".join((message or str(exc)).split())[:300]


def _report_from_run(result, raw_findings: list[Finding]) -> tuple[Report, list[str]]:
    """Turn whatever the run produced into a Report.

    Two shapes are possible and both are legitimate:
    - no handoff  -> `final_output` is a `Report` from the Desk itself
    - handoff     -> `final_output` is the Remediation Specialist's proposal text,
                     because after a handoff the last agent owns the reply

    In the handoff case the Report is assembled here around that text.
    """
    notes: list[str] = []
    output = getattr(result, "final_output", None)
    last_agent = getattr(getattr(result, "last_agent", None), "name", None)

    if isinstance(output, Report):
        # A deep copy, because run_review goes on to overwrite footer and notes.
        # Mutating the object the run handed back would alias it with whoever
        # else holds it — found by FR-12's two-session test, where one session's
        # review rewrote the other session's stored report.
        return output.model_copy(deep=True), notes

    if isinstance(output, str) and output.strip():
        if last_agent != REMEDIATION_SPECIALIST_NAME:
            notes.append(
                f"unexpected text output from {last_agent or 'the run'}; "
                "treating it as a remediation proposal"
            )
        return (
            Report(
                findings=raw_findings,
                footer=[],
                remediation_proposed=True,
                remediation_proposal=output,
                notes=notes,
            ),
            notes,
        )

    notes.append("the desk produced no usable report; showing unmerged findings")
    return (
        Report(
            findings=raw_findings,
            footer=[],
            remediation_proposed=False,
            notes=notes,
        ),
        notes,
    )


async def run_review(
    diff_text: str,
    context: ReviewContext,
    run_config: RunConfig | None = None,
    on_reviewer_done: ReviewerDoneCallback | None = None,
) -> tuple[Report | None, str | None]:
    """Run one whole review. Returns `(report, error_message)`.

    Raises `ReportRefused` or `OutputGuardrailTripwireTriggered` when the report
    cannot be shown (FR-8). Both are caught at the top of each entry point —
    `main.py` for the terminal, `app.py` for the browser — and deliberately NOT
    here, so each entry point has exactly one place deciding what its user sees.

    `on_reviewer_done`, if given, receives each reviewer's raw outcome as that
    reviewer finishes (FR-12) — before merging, the Desk, or the guardrail. It
    carries counts and timings for progress display; showing finding *text* from
    it would bypass FR-8's check on the finished report.

    `report` is None only when there is nothing to review at all; every other
    failure degrades into a partial report with a note (Article VIII.3).
    """
    # (a) FR-1 — deterministic, upstream of any model. Nothing to review means
    # no trace: there is no model activity to record.
    chunks, split_error = split_diff_by_file(diff_text)
    if split_error is not None:
        return None, split_error

    notes: list[str] = []
    reviewable = [c for c in chunks if c["has_text_changes"]]
    if not reviewable:
        return None, "no reviewable text changes found in this diff"
    if len(reviewable) < len(chunks):
        skipped = [c["file"] for c in chunks if not c["has_text_changes"]]
        notes.append(f"no reviewable text changes: {', '.join(skipped)}")

    # FR-13 — ONE id for the whole review, generated before the gather (plan.md
    # §12). The ledger (FR-11) uses the same id, so a ledger line names its trace.
    request_id = new_request_id()
    review_config = review_run_config(request_id, run_config)

    # The one trace for this review. Every Runner.run started inside it — the
    # three concurrent reviewers (asyncio tasks inherit the active trace through
    # their context), the Desk, the Merge tool's nested run, the Remediation
    # handoff — joins this trace instead of starting its own. That is what makes
    # it one trace; `group_id` on the individual configs alone would only group
    # several separate traces together.
    with trace(
        REVIEW_WORKFLOW_NAME,
        trace_id=trace_id_for(request_id),
        group_id=request_id,
        metadata={"request_id": request_id},
        disabled=review_config.tracing_disabled,
    ):
        return await _review_in_trace(
            reviewable, context, review_config, request_id, notes, on_reviewer_done
        )


async def _review_in_trace(
    reviewable: list[dict],
    context: ReviewContext,
    review_config: RunConfig,
    request_id: str,
    notes: list[str],
    on_reviewer_done: ReviewerDoneCallback | None,
) -> tuple[Report | None, str | None]:
    """Steps (b)-(g) of one review, run inside that review's trace."""
    run_config = review_config
    # (b) FR-5 — deterministic, one gather, upstream of the Desk's run.
    review_input = "\n".join(chunk["diff_text"] for chunk in reviewable)
    group = await run_all_reviewers(
        review_input,
        context,
        run_config=run_config,
        request_id=request_id,
        on_reviewer_done=on_reviewer_done,
    )
    notes.extend(
        f"{outcome.reviewer} did not complete: {outcome.error}"
        for outcome in group.outcomes
        if outcome.failed
    )

    raw_findings = group.all_findings
    # The handoff decision is ours, not the model's (FR-6, spec.md §4.6's wording).
    critical_present = decide_needs_remediation(raw_findings)

    # (c)-(f) The Desk's own run: merge as a tool call, remediation as a handoff.
    try:
        result = await Runner.run(
            build_desk(run_config),
            desk_input(raw_findings, critical_present),
            context=context,
            max_turns=DESK_MAX_TURNS,
            run_config=run_config,
        )
    except OutputGuardrailTripwireTriggered:
        # FR-8 fired inside the run. Let it reach the entry point untouched — the
        # report must not be partially shown (plan.md §6).
        raise
    except APIStatusError as exc:
        notes.append(
            f"the desk could not run ({exc.status_code}: {_provider_message(exc)}); "
            "showing unmerged findings"
        )
        report = Report(
            findings=raw_findings,
            footer=build_footer(group),
            remediation_proposed=False,
            notes=notes,
        )
        _final_sweep(report)
        return report, None
    except AgentsException as exc:
        notes.append(
            f"the desk could not run ({type(exc).__name__}: {exc}); "
            "showing unmerged findings"
        )
        report = Report(
            findings=raw_findings,
            footer=build_footer(group),
            remediation_proposed=False,
            notes=notes,
        )
        _final_sweep(report)
        return report, None

    # (g) Assemble: take the model's findings, overwrite what it must not own.
    report, run_notes = _report_from_run(result, raw_findings)
    report.footer = build_footer(group)  # real measurements, never the model's
    report.notes = notes + [n for n in run_notes if n not in notes]
    if critical_present and not report.remediation_proposed:
        report.notes.append(
            "a critical security finding was present but no remediation proposal "
            "was produced"
        )

    # FR-8's final sweep over the assembled Report. The agent-level guardrail
    # cannot see this object: after a handoff the Desk's guardrail never runs, and
    # the footer/notes are attached after the run ends.
    _final_sweep(report)
    return report, None


def _final_sweep(report: Report) -> None:
    """Last check before the Report leaves this module. Raises `ReportRefused`.

    Fails toward refusal: if the scan itself errors, the report is still withheld
    (spec.md §4.8's second edge case).
    """
    try:
        hits = scan_report(report)
    except Exception as exc:  # noqa: BLE001 — deliberate: unknown means unsafe
        raise ReportRefused([]) from exc
    if hits:
        raise ReportRefused(hits)
