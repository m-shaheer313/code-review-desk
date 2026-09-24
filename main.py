"""Terminal entry point.

Reads a unified diff from a path on the command line and runs one review.

THIS FILE IS THE SINGLE PLACE FR-8's TRIPWIRE IS CAUGHT. `run_review` deliberately
lets `ReportRefused` and `OutputGuardrailTripwireTriggered` escape, so exactly one
place in the program decides what the user sees when a report is withheld — see
`_show_or_refuse` below. The report itself is discarded, never partially shown
(Article II.4, plan.md §6).

Per Article VIII.2, every other expected failure — missing credential, unreadable
path, empty or malformed diff — is also caught here and printed as a plain line.
The user never sees a traceback.
"""

import asyncio
import sys
from pathlib import Path

from agents.exceptions import OutputGuardrailTripwireTriggered

from config import ConfigError, configure_tracing, load_config
from guardrail import REFUSAL_MESSAGE, ReportRefused
from ledger import register_ledger
from report import Report
from review_context import ReviewContext

USAGE = "usage: python main.py <path-to-diff-file>"

# FR-2: the review context for a terminal run. The Chainlit path (FR-12) will hold
# its own per-session context instead.
DEFAULT_CONTEXT = ReviewContext(
    repo="code-review-desk",
    language="Python",
    ruleset_id="python-default",
    strictness="normal",
)


def _read_diff_file(path: Path) -> tuple[str | None, str | None]:
    """Return `(diff_text, error_message)` — never raises for a bad path."""
    # Checked up front: on Windows, reading a directory surfaces as
    # PermissionError, which would otherwise produce a misleading message.
    if path.is_dir():
        return None, f"that path is a directory, not a diff file: {path}"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except FileNotFoundError:
        return None, f"no such diff file: {path}"
    except PermissionError:
        return None, f"permission denied reading: {path}"
    except OSError as exc:
        return None, f"could not read {path}: {exc.strerror or exc}"


def print_report(report: Report) -> None:
    """Render a report that has already passed the guardrail."""
    if not report.findings:
        print("no findings.")
    else:
        print(f"{len(report.findings)} finding(s):")
        for finding in report.findings:
            print(
                f"  [{finding.severity:8}] {finding.file}:{finding.line} "
                f"({finding.source_reviewer}) — {finding.message}"
            )

    if report.remediation_proposed and report.remediation_proposal:
        print()
        print("proposed fix (a proposal only — nothing has been applied):")
        for line in report.remediation_proposal.splitlines():
            print(f"  {line}")

    print()
    print("reviewer            ms  tokens  partial")
    for row in report.footer:
        tokens = "—" if row.tokens is None else row.tokens
        print(f"  {row.reviewer:17} {row.ms:6} {tokens:>7}  {row.partial}")

    if report.notes:
        print()
        print("notes:")
        for note in report.notes:
            print(f"  - {note}")
    if report.is_partial:
        print()
        print("this is a PARTIAL review: not every reviewer completed.")


async def _show_or_refuse(diff_text: str) -> int:
    """Run the review and either print the report or print the refusal.

    >>> THIS is the single catch point for FR-8's tripwire. <<<
    """
    from desk import run_review  # imported here so a ConfigError stays catchable

    try:
        report, error = await run_review(diff_text, DEFAULT_CONTEXT)
    except (ReportRefused, OutputGuardrailTripwireTriggered) as exc:
        # The guardrail refused. Show the refusal and nothing else — no findings,
        # no partial report, no detail about what matched.
        print(REFUSAL_MESSAGE)
        if isinstance(exc, ReportRefused) and exc.hits:
            print()
            print("withheld because of:")
            for hit in exc.hits:
                print(f"  - {hit.describe()}")
        return 2

    if error is not None:
        print(error)
        return 1

    print_report(report)
    return 0


async def main() -> int:
    # The reports contain em dashes; the Windows console defaults to cp1252.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) != 2:
        print(USAGE)
        return 2

    try:
        # Validates GEMINI_API_KEY now, so a misconfigured run fails before any
        # work rather than halfway through a review (Article II.3).
        load_config()
        # FR-13 / Article VII.1: traces export under the developer's own key.
        # The same call the browser path makes (chat_session.handle_message) —
        # one setup function, two entry points.
        configure_tracing()
    except ConfigError as exc:
        print(str(exc))
        return 1

    diff_path = Path(sys.argv[1])
    diff_text, read_error = _read_diff_file(diff_path)
    if read_error is not None:
        print(read_error)
        return 1

    # FR-11: the ledger's single registration line. Delete it and the ledger is
    # off — no agent or reviewer file needs to change.
    register_ledger()

    return await _show_or_refuse(diff_text)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
