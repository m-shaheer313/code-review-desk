"""Tests for testing_env — the guard that stops test runs exporting real traces.

The dangerous condition is simulated for real: OPENAI_API_KEY is set in this
process, and a full stubbed review is run. A capturing processor replaces the
exporter purely as a measuring device (so even a broken guard could not send
anything); it receives a trace only if tracing is enabled. It must receive none.

Run with `pytest tests/test_trace_guard.py`, or standalone: `python tests/test_trace_guard.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import ast
import asyncio
import os
from pathlib import Path

from agents import set_trace_processors
from agents.tracing.processor_interface import TracingProcessor

import desk
from report import Report
from review_context import ReviewContext
from test_desk_agent import DIFF, FakeResult
from test_fr9 import install_stub

# This file lives in tests/. Test files are its siblings; product code and
# scripts/ live one level up.
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent


class CountingProcessor(TracingProcessor):
    def __init__(self) -> None:
        self.traces = 0

    def on_trace_start(self, trace) -> None:
        self.traces += 1

    def on_trace_end(self, trace) -> None:
        pass

    def on_span_start(self, span) -> None:
        pass

    def on_span_end(self, span) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


def _first_import(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))
    return ast.unparse(node)


def test_every_test_file_imports_the_guard_first() -> None:
    # A new test file that forgets the guard fails here, instead of exporting.
    # conftest.py is included: under pytest it is what loads the guard before any
    # test module, whatever order pytest collects them in.
    test_files = sorted(TESTS_DIR.glob("test_*.py")) + [TESTS_DIR / "conftest.py"]
    assert len(test_files) >= 11
    offenders = {
        p.name: _first_import(p)
        for p in test_files
        if _first_import(p) != "import testing_env"
    }
    assert offenders == {}, offenders


def test_the_disable_flag_is_set_for_the_process() -> None:
    assert os.environ[testing_env.TRACING_DISABLE_VAR] == "1"


def test_no_trace_is_created_even_with_an_openai_key_present() -> None:
    counter = CountingProcessor()
    set_trace_processors([counter])
    saved = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "sk-test-guard-only-not-a-real-key"
    try:
        install_stub(
            {},
            desk_output=FakeResult(
                Report(findings=[], footer=[], remediation_proposed=False),
                last_agent_name=desk.DESK_NAME,
            ),
        )
        report, error = asyncio.run(
            desk.run_review(
                DIFF,
                ReviewContext(repo="r", language="Python", ruleset_id="python-default"),
            )
        )
    finally:
        if saved is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = saved

    assert error is None and isinstance(report, Report)  # the review really ran
    assert counter.traces == 0, "a trace was created during a test run"


def test_product_code_and_live_scripts_never_import_the_guard() -> None:
    # Live reviews must trace (Article VII.1); only tests may switch it off.
    # Test code lives in tests/, so this scan of the project root and scripts/
    # covers exactly the product code and the live scripts.
    scanned = list(PROJECT_ROOT.glob("*.py")) + list((PROJECT_ROOT / "scripts").glob("*.py"))
    # Guard against a vacuous pass from scanning the wrong directory.
    assert {"main.py", "app.py", "desk.py"} <= {p.name for p in scanned}, scanned
    assert not any(p.name.startswith("test_") or p.name == "testing_env.py" for p in scanned)
    for path in scanned:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name != "testing_env" for a in node.names), path.name
            if isinstance(node, ast.ImportFrom):
                assert node.module != "testing_env", path.name


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
