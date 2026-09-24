"""The run ledger (FR-11): one JSON line per reviewer run in `ledger.jsonl`.

Line shape, plan.md §4.4:
    {"ts": "2026-09-23T19:04:11Z", "request_id": "rev_...", "agent": "SecurityReviewer",
     "ms": 2140, "findings": 3}

This is optional infrastructure. `register_ledger()` is called once, from the entry
point, and plugs a writer into `review_runner`'s run observers. Nothing else in
the codebase imports this module — no agent, no reviewer, not the runner — so
deleting that one registration call switches the ledger off and changes nothing
else (FR-11's acceptance criterion).

What a line never contains (Article II.4): finding messages, file paths, model
output or diff text. A reviewer name, a duration and a count cannot leak a
credential found in a reviewed diff.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from review_runner import ReviewerOutcome, add_run_observer

LEDGER_PATH = Path(__file__).resolve().parent / "ledger.jsonl"


def _iso_utc(moment: datetime | None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_line(request_id: str, outcome: ReviewerOutcome) -> dict:
    """The §4.4 record for one reviewer run. A failed run still gets its line with
    the data that genuinely exists: its real elapsed time, and 0 findings because
    it produced none — the same keep-the-row rule as the FR-10 footer."""
    return {
        "ts": _iso_utc(outcome.finished_at),
        "request_id": request_id,
        "agent": outcome.reviewer,
        "ms": outcome.elapsed_ms,
        "findings": len(outcome.findings),
    }


class LedgerWriter:
    """Appends one line per reviewer run to `path`. Never raises: a ledger that
    cannot be written is reported on stderr and the review carries on."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def __call__(self, request_id: str, outcome: ReviewerOutcome) -> None:
        line = json.dumps(ledger_line(request_id, outcome), separators=(", ", ": "))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            print(
                f"ledger not written ({exc.strerror or type(exc).__name__}): {self.path}",
                file=sys.stderr,
            )

    # Two writers for the same file are the same writer, so registering twice
    # cannot double every line.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, LedgerWriter) and other.path == self.path

    def __hash__(self) -> int:
        return hash(self.path)


def register_ledger(path: Path | None = None):
    """Start writing the ledger. Call once, at startup. Returns an unregister
    function. `path` defaults to the project root's `ledger.jsonl`."""
    return add_run_observer(LedgerWriter(path or LEDGER_PATH))
