"""The run ledger (FR-11): one JSON line per reviewer run in `ledger.jsonl`.

Line shape, plan.md §4.4:
    {"ts": "2026-09-23T19:04:11Z", "request_id": "rev_...", "agent": "SecurityReviewer",
     "ms": 2140, "findings": 3}

This is optional infrastructure. `register_ledger()` is called once per process,
from each entry point — `main.py` for the terminal, `app.py`'s `on_app_startup` for
the browser — the same call in both, and plugs a writer into `review_runner`'s run
observers. Nothing else imports this module — no agent, no reviewer, not the
runner — so deleting an entry point's one registration call switches the ledger
off for that entry point and changes nothing else (FR-11's acceptance criterion).

What a line never contains (Article II.4): finding messages, file paths, model
output or diff text. A reviewer name, a duration and a count cannot leak a
credential found in a reviewed diff.
"""

import json
import sys
import threading
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


# Serializes ledger appends WITHIN THIS PROCESS (FR-12 made this necessary: one
# Chainlit process serves many browser sessions, so two reviews can finish at the
# same moment). One module-level lock for every writer, since two writers
# pointed at the same file must also exclude each other.
#
# Honest scope of what this protects:
# - Coroutines on one event loop cannot interleave a *synchronous* open/write/close
#   anyway — nothing awaits in the middle of it. The lock is what protects the case
#   that actually can interleave in one process: appends from different threads
#   (Chainlit worker threads, `cl.make_async`, a threaded server).
# - It does NOTHING across processes. Running `main.py` and Chainlit at the same
#   time against the same `ledger.jsonl` is still unprotected: each process has its
#   own lock, and Windows does not guarantee an append lands atomically. That is a
#   known, separate limitation — it would need an OS-level file lock
#   (msvcrt.locking / fcntl.flock) or one writer process — and it is not fixed here.
_WRITE_LOCK = threading.Lock()


class LedgerWriter:
    """Appends one line per reviewer run to `path`. Never raises: a ledger that
    cannot be written is reported on stderr and the review carries on."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def __call__(self, request_id: str, outcome: ReviewerOutcome) -> None:
        line = json.dumps(ledger_line(request_id, outcome), separators=(", ", ": "))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # One whole line per lock hold: open, write the line and its newline
            # together, close. Another thread's line can only land before or after.
            with _WRITE_LOCK, self.path.open("a", encoding="utf-8") as handle:
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
