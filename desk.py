"""The Desk — orchestration of one whole review (FR-1 → FR-6).

DESIGN DECISION — the Desk has no `Agent` object yet, and this is deliberate.

plan.md §2 describes the Desk as an agent with static instructions, `output_type
Report`, and Remediation as a handoff target. That agent is still coming, but
nothing built so far needs it, and two requirements actively argue against
letting a model drive this step today:

- FR-1 requires the diff to be split **before any model sees it**, and FR-5
  requires all three reviewers to be launched in one `asyncio.gather`. Both are
  deterministic obligations. A model-driven Desk would decide for itself whether
  to call the splitter and whether to launch reviewers together — it could satisfy
  them on a good run and quietly violate them on a bad one, and Article V.1 says
  concurrency is graded, not incidental.
- The two things that genuinely require an `Agent` are FR-8's output guardrail
  (guardrails attach to an agent) and FR-6's handoff *as a handoff* (the SDK's
  handoff mechanism needs a source agent). Both are later work.

So: deterministic orchestration now; the Desk `Agent` arrives with FR-8, wrapping
this flow rather than replacing it. Two consequences are recorded honestly below,
at the call sites — Merge is invoked as a tool object directly rather than by a
model choosing to call it, and Remediation is invoked directly rather than reached
by a true SDK handoff.

Error-reporting pattern matches the rest of the codebase: `(result, error_message)`
and no raising for expected failures (Article VIII).
"""

import json

from agents import RunConfig, Runner
from agents.exceptions import AgentsException
from agents.tool_context import ToolContext
from openai import APIStatusError
from pydantic import ValidationError

from diff_utils import split_diff_by_file
from finding import Finding
from merge import MERGE_INPUT_KEY, MERGE_TOOL_NAME, as_merge_tool
from remediation import (
    REMEDIATION_SPECIALIST_NAME,
    build_remediation_specialist,
    decide_needs_remediation,
)
from report import Report, ReviewerFooterRow
from review_context import ReviewContext
from review_runner import GroupOutcome, run_all_reviewers
from reviewers import SECURITY_REVIEWER_NAME

REMEDIATION_MAX_TURNS = 3  # no tools to call; Article VI.2 still requires a bound


def findings_to_json(findings: list[Finding]) -> str:
    """Serialize findings into the exact contract Merge's instructions state.

    `source_reviewer` is included: Merge is told to copy it through, and FR-6's
    handoff decision reads it off the merged result.
    """
    return json.dumps({MERGE_INPUT_KEY: [f.model_dump() for f in findings]})


def parse_merged_findings(raw: str) -> tuple[list[Finding] | None, str | None]:
    """Parse Merge's tool output back into findings.

    Returns `(findings, error_message)`; `findings` is None when the output could
    not be trusted, so the caller can fall back to the unmerged union rather than
    presenting nothing (plan.md §3's failure-behavior column).

    Accepts the `{"findings": [...]}` contract Merge is given and returns, and
    also tolerates a bare array or the SDK's strict `{"response": [...]}` wrapper,
    since a model that ignores its instructions should degrade rather than break
    the review.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "merge returned nothing"
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, "merge returned text that is not valid JSON"

    if isinstance(payload, dict):
        for key in (MERGE_INPUT_KEY, "response", "findings"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            return None, "merge returned JSON with no findings array"
    if not isinstance(payload, list):
        return None, "merge returned JSON that is not a findings array"

    try:
        return [Finding.model_validate(item) for item in payload], None
    except ValidationError:
        return None, "merge returned findings that failed validation"


def build_footer(group: GroupOutcome) -> list[ReviewerFooterRow]:
    """One row per reviewer. Latency is real (FR-5's timing); token counts wait
    for FR-10's hooks — Article VII.3 forbids inventing them, so 0 means
    "not measured yet"."""
    return [
        ReviewerFooterRow(
            reviewer=outcome.reviewer,
            ms=outcome.elapsed_ms,
            tokens=0,  # TODO(FR-10): real usage from the run-level hooks
            partial=outcome.failed,
        )
        for outcome in group.outcomes
    ]


def critical_security_findings(findings: list[Finding]) -> list[Finding]:
    """The findings that justify remediation — used to brief the specialist."""
    return [
        f
        for f in findings
        if f.severity == "critical" and f.source_reviewer == SECURITY_REVIEWER_NAME
    ]


def _provider_message(exc: APIStatusError) -> str:
    """One-line provider explanation; Gemini returns a list body, OpenAI a dict."""
    body = exc.body
    if isinstance(body, list):
        body = body[0] if body else {}
    error = body.get("error") if isinstance(body, dict) else None
    message = (error or {}).get("message") if isinstance(error, dict) else None
    return " ".join((message or str(exc)).split())[:300]


async def _run_merge(
    findings: list[Finding],
    context: ReviewContext,
    run_config: RunConfig | None,
) -> tuple[list[Finding], str | None]:
    """Call the merge tool. Returns `(findings, note)`; on any failure the
    unmerged union comes back with a note, never an exception (plan.md §3)."""
    if not findings:
        return [], None

    # Invoked as a tool object directly: with no Desk agent, there is no model to
    # choose to call it. The tool boundary itself is real — same FunctionTool the
    # Desk agent will expose — so only the "who decided to call it" part is
    # standing in. That flips when the Desk becomes an agent (FR-8).
    merge_tool = as_merge_tool(run_config=run_config)
    arguments = json.dumps({"input": findings_to_json(findings)})
    tool_context = ToolContext(
        context,
        tool_name=MERGE_TOOL_NAME,
        tool_call_id="desk_merge_1",
        tool_arguments=arguments,
        run_config=run_config,
    )
    try:
        raw = await merge_tool.on_invoke_tool(tool_context, arguments)
    except APIStatusError as exc:
        return findings, f"merge unavailable ({exc.status_code}); findings not deduplicated"
    except AgentsException as exc:
        return findings, f"merge failed ({type(exc).__name__}); findings not deduplicated"

    merged, problem = parse_merged_findings(raw)
    if merged is None:
        return findings, f"{problem}; findings not deduplicated"
    return merged, None


async def _run_remediation(
    criticals: list[Finding],
    context: ReviewContext,
    run_config: RunConfig | None,
) -> tuple[str | None, str | None]:
    """Invoke the Remediation Specialist. Returns `(proposal, note)`.

    NOTE — this is a direct invocation, not the SDK handoff spec.md §4.6 calls
    for. A real handoff needs a source agent to hand off *from*; the agent object
    used here is the same one that will be registered as the Desk's handoff target,
    so the target is right and only the transfer mechanism is provisional.
    """
    specialist = build_remediation_specialist()
    briefing = json.dumps(
        {
            "critical_security_findings": [f.model_dump() for f in criticals],
        }
    )
    try:
        result = await Runner.run(
            specialist,
            briefing,
            context=context,
            max_turns=REMEDIATION_MAX_TURNS,
            run_config=run_config,
        )
    except APIStatusError as exc:
        return None, f"remediation unavailable ({exc.status_code}): {_provider_message(exc)}"
    except AgentsException as exc:
        return None, f"remediation failed ({type(exc).__name__}: {exc})"

    proposal = getattr(result, "final_output", None)
    if not isinstance(proposal, str) or not proposal.strip():
        return None, f"{REMEDIATION_SPECIALIST_NAME} returned no proposal text"
    return proposal, None


async def run_review(
    diff_text: str,
    context: ReviewContext,
    run_config: RunConfig | None = None,
) -> tuple[Report | None, str | None]:
    """Run one whole review. Returns `(report, error_message)`.

    `report` is None only when there is nothing to review at all (empty or
    malformed diff); every other failure degrades into a partial report with a
    note (Article VIII.3).
    """
    if run_config is None:
        # FR-13 will supply a configured RunConfig carrying this review's trace
        # grouping; until then tracing is off, set per run, never globally.
        run_config = RunConfig(tracing_disabled=True)

    # (a) FR-1 — split before any model sees the diff.
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

    # (b) FR-5 — one gather, three reviewers, over the reviewable diff text.
    review_input = "\n".join(chunk["diff_text"] for chunk in reviewable)
    group = await run_all_reviewers(review_input, context, run_config=run_config)
    notes.extend(
        f"{outcome.reviewer} did not complete: {outcome.error}"
        for outcome in group.outcomes
        if outcome.failed
    )

    # (c) + (d) FR-6 — serialize the union, merge it, parse the result back.
    merged, merge_note = await _run_merge(group.all_findings, context, run_config)
    if merge_note:
        notes.append(merge_note)

    # (e) FR-6 — decide on the handoff from the merged findings.
    needs_remediation = decide_needs_remediation(merged)

    # (f) FR-6 — brief the specialist with the qualifying findings only.
    proposal: str | None = None
    if needs_remediation:
        proposal, remediation_note = await _run_remediation(
            critical_security_findings(merged), context, run_config
        )
        if remediation_note:
            notes.append(remediation_note)

    # (g) plan.md §4.3 — assemble the Report.
    report = Report(
        findings=merged,
        footer=build_footer(group),
        # True when the handoff was warranted and produced a proposal. A failed
        # remediation call must not claim a proposal exists.
        remediation_proposed=bool(proposal),
        remediation_proposal=proposal,
        notes=notes,
    )
    return report, None
