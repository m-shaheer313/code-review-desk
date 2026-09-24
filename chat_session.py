"""Browser-session logic for FR-12, kept free of Chainlit so it can be tested.

`app.py` is a thin Chainlit shell over `handle_message`. Everything that decides
behavior lives here and takes its session store and its "send a message" function
as parameters — Chainlit passes `cl.user_session` and `cl.Message(...).send()`,
tests pass plain stand-ins.

SESSION ISOLATION (spec.md §4.12). This module holds no state. The ReviewContext
and the last Report live only in the store the caller hands in, which in the app
is `cl.user_session` — one per browser session. Nothing here is module-level and
mutable, so two sessions cannot see each other's context or report.

SETTING THE CONTEXT. Decision: a one-line header, not a form. A message may begin
with a line like

    context: repo=billing-service language=Python ruleset=python-default strictness=strict

and the rest of the message is the diff. Any subset of keys may be given; the rest
are kept from the session's current context (or defaults on the first message).
A message with no header reuses the session's context unchanged — which is how a
second diff "reuses the existing ReviewContext unless the user changes it".
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from agents.exceptions import OutputGuardrailTripwireTriggered

import desk
from config import ConfigError
from guardrail import REFUSAL_MESSAGE, ReportRefused
from report import Report
from review_context import ReviewContext
from review_runner import ReviewerOutcome

CONTEXT_KEY = "review_context"
REPORT_KEY = "last_report"

HEADER_PREFIX = "context:"
# Header key -> ReviewContext field. `ruleset` is the friendlier spelling.
HEADER_KEYS = {
    "repo": "repo",
    "language": "language",
    "ruleset": "ruleset_id",
    "ruleset_id": "ruleset_id",
    "strictness": "strictness",
}

WELCOME = (
    "Paste a unified diff to review it.\n\n"
    "To set the review context, start your message with a line like:\n\n"
    "`context: repo=my-repo language=Python ruleset=python-default strictness=normal`\n\n"
    "Any subset of keys works; the rest stay as they were. Without that line, the "
    "session's current context is reused (defaults on the first review)."
)


class SessionStore(Protocol):
    """What `cl.user_session` provides, and all this module needs from it."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


Send = Callable[[str], Awaitable[None]]


def default_context() -> ReviewContext:
    """A fresh default each call — never a shared module-level instance."""
    return ReviewContext(
        repo="unnamed-repository",
        language="Python",
        ruleset_id="python-default",
        strictness="normal",
    )


def parse_message(
    text: str, current: ReviewContext | None
) -> tuple[ReviewContext | None, str, str | None]:
    """Split a message into `(context, diff_text, error)`.

    Never raises. On error, `context` is None and the session's context must be
    left as it was.
    """
    base = current or default_context()
    lines = (text or "").splitlines()
    first = lines[0].strip() if lines else ""
    if not first.lower().startswith(HEADER_PREFIX):
        return base, text or "", None

    fields = {
        "repo": base.repo,
        "language": base.language,
        "ruleset_id": base.ruleset_id,
        "strictness": base.strictness,
    }
    for pair in first[len(HEADER_PREFIX):].split():
        key, sep, value = pair.partition("=")
        if not sep or not value:
            return None, "", f"could not read '{pair}' in the context line — use key=value"
        field_name = HEADER_KEYS.get(key.strip().lower())
        if field_name is None:
            allowed = ", ".join(sorted({"repo", "language", "ruleset", "strictness"}))
            return None, "", f"unknown context key '{key}' — allowed: {allowed}"
        fields[field_name] = value.strip()

    try:
        context = ReviewContext(**fields)
    except ValueError as exc:  # invalid strictness, rejected at construction
        return None, "", str(exc)
    return context, "\n".join(lines[1:]), None


def describe_context(context: ReviewContext) -> str:
    return (
        f"repo=`{context.repo}` language=`{context.language}` "
        f"ruleset=`{context.ruleset_id}` strictness=`{context.strictness}`"
    )


def render_progress(outcome: ReviewerOutcome) -> str:
    """One reviewer's completion, as it happens.

    COUNTS ONLY, by design: this message is sent before merging and before FR-8's
    guardrail has inspected anything, so a finding's text — which could quote a
    credential from the diff — must not appear here (Article II.4). The full
    findings arrive in the final report, after the guardrail.
    """
    tokens = "—" if outcome.tokens is None else outcome.tokens
    if outcome.failed:
        return (
            f"⚠️ **{outcome.reviewer}** did not complete after {outcome.elapsed_ms} ms "
            f"— 0 findings (see notes in the final report)"
        )
    count = len(outcome.findings)
    plural = "finding" if count == 1 else "findings"
    return (
        f"✅ **{outcome.reviewer}** finished in {outcome.elapsed_ms} ms — "
        f"{count} raw {plural} ({tokens} tokens)"
    )


def render_report(report: Report) -> str:
    lines = ["### Review", ""]
    if not report.findings:
        lines.append("No findings.")
    for finding in report.findings:
        lines.append(
            f"- **{finding.severity}** `{finding.file}:{finding.line}` "
            f"({finding.source_reviewer}) — {finding.message}"
        )
    if report.remediation_proposal:
        lines += ["", "### Proposed remediation (proposal only — nothing applied)", ""]
        lines.append(report.remediation_proposal)
    lines += ["", "| reviewer | ms | tokens | partial |", "|---|---|---|---|"]
    for row in report.footer:
        tokens = "—" if row.tokens is None else row.tokens
        lines.append(f"| {row.reviewer} | {row.ms} | {tokens} | {row.partial} |")
    if report.notes:
        lines += ["", "**Notes**"] + [f"- {note}" for note in report.notes]
    if report.is_partial:
        lines += ["", "_This is a PARTIAL review: not every reviewer completed._"]
    return "\n".join(lines)


def start_session(store: SessionStore) -> None:
    """Empty per-session state (plan.md §13). Context stays None until the first
    message sets or defaults it."""
    store.set(CONTEXT_KEY, None)
    store.set(REPORT_KEY, None)


async def handle_message(
    store: SessionStore,
    text: str,
    send: Send,
    run: Callable[..., Awaitable[tuple[Report | None, str | None]]] | None = None,
) -> None:
    """Handle one pasted message: update the context if asked, review the diff,
    stream progress per reviewer, then show the final report.

    The review is AWAITED (spec.md §4.12's acceptance criterion) — there is no
    synchronous path. `run` defaults to `desk.run_review`, looked up at call time
    so tests can substitute it.

    This is the browser's top-level boundary for FR-8: a refused report is
    replaced by the refusal message and is never stored in the session.
    """
    run = run or desk.run_review

    context, diff_text, error = parse_message(text, store.get(CONTEXT_KEY))
    if error is not None:
        await send(f"Context not changed: {error}")
        return
    store.set(CONTEXT_KEY, context)

    if not diff_text.strip():
        await send(f"Review context set: {describe_context(context)}")
        return

    await send(f"Reviewing with {describe_context(context)} …")

    async def on_reviewer_done(outcome: ReviewerOutcome) -> None:
        await send(render_progress(outcome))

    try:
        report, error = await run(diff_text, context, on_reviewer_done=on_reviewer_done)
    except (ReportRefused, OutputGuardrailTripwireTriggered):
        store.set(REPORT_KEY, None)
        await send(REFUSAL_MESSAGE)
        return
    except ConfigError as exc:
        await send(str(exc))
        return

    if error is not None:
        await send(error)
        return

    store.set(REPORT_KEY, report)
    await send(render_report(report))
