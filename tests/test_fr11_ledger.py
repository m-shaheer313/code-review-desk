"""Structural tests for FR-11 — the run ledger. No live model call.

ISOLATION. Every test writes to a fresh `tempfile.TemporaryDirectory()`, passed
explicitly as `register_ledger(path=...)` — no path constant is monkeypatched, so
there is nothing to forget to restore. Every registration is undone in a
`finally`, so no writer leaks into another test. One test asserts the real
project-root `ledger.jsonl` is untouched by the whole exercise.

Model calls are stubbed at `Runner.run`, reusing test_fr9's stub (which supports
per-reviewer failures) and test_desk_agent's fixtures.

Run with `pytest tests/test_fr11_ledger.py`, or standalone: `python tests/test_fr11_ledger.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import ast
import asyncio
import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import desk
import ledger
import review_runner
from diff_utils import split_diff_by_file
from report import Report
from review_context import ReviewContext
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
)
from test_desk_agent import FakeResult, SECURITY_RAW, STYLE_RAW, TESTS_RAW
from test_fr9 import ceiling, install_stub

REVIEWERS = {SECURITY_REVIEWER_NAME, TESTS_REVIEWER_NAME, STYLE_REVIEWER_NAME}

THREE_FILE_DIFF = """diff --git a/billing/refunds.py b/billing/refunds.py
--- a/billing/refunds.py
+++ b/billing/refunds.py
@@ -1,2 +1,3 @@
 import logging
+from decimal import *
diff --git a/billing/fees.py b/billing/fees.py
--- a/billing/fees.py
+++ b/billing/fees.py
@@ -1 +1,2 @@
 import logging
+def CalculateFee(Amount): return Amount
diff --git a/billing/config.py b/billing/config.py
--- a/billing/config.py
+++ b/billing/config.py
@@ -1 +1,2 @@
 import os
+API_KEY = os.environ["BILLING_KEY"]
"""

TS_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REQUEST_ID_SHAPE = re.compile(r"^rev_[0-9a-f]{32}$")


def context() -> ReviewContext:
    return ReviewContext(
        repo="code-review-desk", language="Python", ruleset_id="python-default"
    )


def desk_report() -> FakeResult:
    """What the stubbed Desk returns — a plain Report, no handoff."""
    return FakeResult(
        Report(findings=[], footer=[], remediation_proposed=False),
        last_agent_name=desk.DESK_NAME,
    )


@contextmanager
def isolated_ledger(register: bool = True):
    """A ledger path in a fresh temp dir; registered only if asked, always
    unregistered afterwards."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        unregister = ledger.register_ledger(path=path) if register else None
        try:
            yield path
        finally:
            if unregister:
                unregister()


def read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def review(diff: str = THREE_FILE_DIFF, failures: dict | None = None) -> Report:
    install_stub(failures or {}, desk_output=desk_report())
    report, error = asyncio.run(desk.run_review(diff, context()))
    assert error is None, error
    assert isinstance(report, Report)
    return report


# ---------------------------------------------------------------------------
# One line per reviewer run, one request_id per review
# ---------------------------------------------------------------------------


def test_three_file_review_writes_exactly_three_lines_sharing_one_request_id() -> None:
    chunks, _ = split_diff_by_file(THREE_FILE_DIFF)
    assert len(chunks) == 3, "the fixture must really be a three-file diff"

    with isolated_ledger() as path:
        review()
        lines = read_lines(path)

    # Three reviewer runs -> three lines. Not 9 (per file), not 1 (per review).
    assert len(lines) == 3, lines
    assert {line["agent"] for line in lines} == REVIEWERS  # each a different reviewer
    request_ids = {line["request_id"] for line in lines}
    assert len(request_ids) == 1, request_ids  # one review -> one id
    assert REQUEST_ID_SHAPE.match(request_ids.pop())


def test_line_shape_matches_plan_md_4_4() -> None:
    with isolated_ledger() as path:
        review()
        lines = read_lines(path)

    for line in lines:
        assert set(line) == {"ts", "request_id", "agent", "ms", "findings"}, line
        assert TS_SHAPE.match(line["ts"]), line["ts"]
        assert isinstance(line["ms"], int) and line["ms"] > 0
        assert isinstance(line["findings"], int)
    by_agent = {line["agent"]: line for line in lines}
    assert by_agent[SECURITY_REVIEWER_NAME]["findings"] == len(SECURITY_RAW)
    assert by_agent[TESTS_REVIEWER_NAME]["findings"] == len(TESTS_RAW)
    assert by_agent[STYLE_REVIEWER_NAME]["findings"] == len(STYLE_RAW)


def test_two_reviews_get_two_request_ids_and_six_lines() -> None:
    with isolated_ledger() as path:
        review()
        review()
        lines = read_lines(path)

    assert len(lines) == 6
    ids = [line["request_id"] for line in lines]
    assert len(set(ids)) == 2
    assert all(ids.count(i) == 3 for i in set(ids))


def test_ledger_never_contains_finding_text_or_paths() -> None:
    # Article II.4 — counts only.
    with isolated_ledger() as path:
        review()
        raw = path.read_text(encoding="utf-8")
    for finding in SECURITY_RAW + TESTS_RAW + STYLE_RAW:
        assert finding.message not in raw
    assert "billing/" not in raw


# ---------------------------------------------------------------------------
# FR-11's second acceptance criterion: the ledger is optional infrastructure
# ---------------------------------------------------------------------------


def test_without_registration_a_normal_review_writes_nothing() -> None:
    with isolated_ledger(register=False) as path:
        assert review_runner._run_observers == [], "nothing may be registered by default"
        report = review()
        # The review genuinely completed — nobody depended on the ledger.
        assert report.is_partial is False
        assert len(report.footer) == 3
        assert not path.exists()
        assert read_lines(path) == []


def test_unregistering_switches_the_ledger_off() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        unregister = ledger.register_ledger(path=path)
        try:
            review()
        finally:
            unregister()
        review()  # after unregistering
        assert len(read_lines(path)) == 3


def test_no_agent_or_reviewer_module_imports_the_ledger() -> None:
    # "No agent definition mentions it" (spec.md §4.11), checked on the imports.
    root = Path(__file__).resolve().parent.parent  # project root; this file is in tests/
    for module in (
        "reviewers.py", "review_runner.py", "desk.py", "merge.py",
        "remediation.py", "tools.py", "hooks.py", "guardrail.py",
    ):
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name != "ledger" for a in node.names), module
            if isinstance(node, ast.ImportFrom):
                assert node.module != "ledger", module


def test_registering_twice_still_writes_one_line_per_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        first = ledger.register_ledger(path=path)
        second = ledger.register_ledger(path=path)
        try:
            review()
        finally:
            first()
            second()
        assert len(read_lines(path)) == 3


# ---------------------------------------------------------------------------
# Failed and partial reviewer runs keep their line
# ---------------------------------------------------------------------------


def test_failed_and_ceiling_runs_still_write_their_own_line() -> None:
    with isolated_ledger() as path:
        report = review(
            failures={
                SECURITY_REVIEWER_NAME: ceiling(),
                TESTS_REVIEWER_NAME: RuntimeError("connection reset"),
            }
        )
        lines = read_lines(path)

    assert report.is_partial is True
    assert len(lines) == 3
    assert {line["agent"] for line in lines} == REVIEWERS
    by_agent = {line["agent"]: line for line in lines}
    for failed in (SECURITY_REVIEWER_NAME, TESTS_REVIEWER_NAME):
        assert by_agent[failed]["findings"] == 0
        # Real elapsed time for a run that failed — the stub sleeps 10ms before
        # failing, so a placeholder 0 would not pass.
        assert by_agent[failed]["ms"] >= 10, by_agent[failed]
    assert by_agent[STYLE_REVIEWER_NAME]["findings"] == len(STYLE_RAW)
    assert len({line["request_id"] for line in lines}) == 1


def test_unwritable_ledger_never_fails_the_review() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # A directory where the file should be: every append raises OSError.
        blocked = Path(tmp) / "ledger.jsonl"
        blocked.mkdir()
        unregister = ledger.register_ledger(path=blocked)
        try:
            report = review()
        finally:
            unregister()
        assert report.is_partial is False
        assert len(report.footer) == 3


def test_the_real_project_ledger_is_untouched() -> None:
    real = ledger.LEDGER_PATH
    before = (real.exists(), real.stat().st_mtime_ns if real.exists() else None)
    with isolated_ledger() as path:
        review()
        assert path != real
    after = (real.exists(), real.stat().st_mtime_ns if real.exists() else None)
    assert before == after


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
