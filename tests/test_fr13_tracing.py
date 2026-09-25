"""Structural tests for FR-13 — one review, one trace. No live model call.

WHAT THESE PROVE, AND HOW
- A capturing TracingProcessor REPLACES the SDK's default exporter for this test
  process (`set_trace_processors`), so no trace can leave the machine while the
  tests run, and every trace the code opens is observable.
- `Runner.run` is stubbed. Inside the stub, each run records the RunConfig it was
  given AND the trace that is active at that moment (`get_current_trace()`). So
  the tests check not only that the configs carry the same id, but that every
  run — the three concurrent reviewers, the Desk, and the Merge tool's nested
  run — genuinely executed inside the one trace.
- The Desk stub invokes the REAL Merge tool taken from the REAL Desk agent it was
  handed, so Merge's nested run config comes from the actual build_desk wiring.
- Remediation is a handoff inside the Desk's own run: the tests assert it never
  becomes a separate Runner.run with a config of its own.

What stays unproven until a live run: that the exported spans *render* as
overlapping in the dashboard. That needs the real API.

Run with `pytest tests/test_fr13_tracing.py`, or standalone: `python tests/test_fr13_tracing.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import ast
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

import pytest
from agents import get_current_trace, set_trace_processors, set_tracing_disabled
from agents.tool_context import ToolContext
from agents.tracing.processor_interface import TracingProcessor

import config
import desk
import ledger
import review_runner
from merge import MERGE_SPECIALIST_NAME, MERGE_TOOL_NAME
from remediation import REMEDIATION_SPECIALIST_NAME
from review_context import ReviewContext
from review_runner import REVIEW_WORKFLOW_NAME, trace_id_for
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
)
from reviewers import STYLE_RULESET_LOOKUP_NAME
from test_desk_agent import DIFF, FakeResult, MERGED, SECURITY_RAW, STYLE_RAW, TESTS_RAW, lookup_result

# This file lives in tests/; the product files it inspects live one level up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEWERS = [SECURITY_REVIEWER_NAME, TESTS_REVIEWER_NAME, STYLE_REVIEWER_NAME]
RAW = {
    SECURITY_REVIEWER_NAME: SECURITY_RAW,
    TESTS_REVIEWER_NAME: TESTS_RAW,
    STYLE_REVIEWER_NAME: STYLE_RAW,
}
PROPOSAL = "PROPOSAL ONLY — nothing applied. Load the credential from the environment."
TRACE_ID_SHAPE = re.compile(r"^trace_[0-9a-f]{32}$")


class CapturingProcessor(TracingProcessor):
    """Records traces instead of exporting them."""

    def __init__(self) -> None:
        self.traces = []

    def on_trace_start(self, trace) -> None:
        self.traces.append(trace)

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


PROCESSOR = CapturingProcessor()


def enable_capturing_tracing() -> None:
    """Turn tracing ON for this file's tests, with nothing exported.

    ORDER MATTERS. testing_env switched tracing off; this file needs it on. First
    remove the real exporter by replacing all processors with the capturing one,
    THEN re-enable tracing (the SDK lets set_tracing_disabled override the env
    flag). There is never a moment with tracing on and the real exporter still
    installed.

    Deliberately NOT done at import time: under pytest every test module is
    imported before any test runs, so an import-time override would switch
    tracing on for the whole session (observed: it broke test_trace_guard).
    """
    set_trace_processors([PROCESSOR])
    set_tracing_disabled(False)


@pytest.fixture(autouse=True)
def tracing_on_for_this_file():
    # Runs after conftest.py's reset (which turns tracing off), and re-installs
    # the capturing processor in case another file replaced it.
    enable_capturing_tracing()
    yield

runs: list[dict] = []


def install_stub(delays=None) -> None:
    """Stub the model boundary; record config + active trace for every run."""
    runs.clear()
    PROCESSOR.traces.clear()
    delays = delays or {}

    async def fake_run(agent=None, input=None, *, starting_agent=None, **kwargs):
        resolved = starting_agent if agent is None else agent
        current = get_current_trace()
        runs.append(
            {
                "agent": resolved.name,
                "run_config": kwargs.get("run_config"),
                "trace_id": current.trace_id if current is not None else None,
            }
        )
        await asyncio.sleep(delays.get(resolved.name, 0.01))
        if resolved.name == STYLE_RULESET_LOOKUP_NAME:
            return lookup_result(resolved)  # Style's forced get_ruleset step (FR-9a)

        if resolved.name == MERGE_SPECIALIST_NAME:
            return FakeResult(MERGED)
        if resolved.name == desk.DESK_NAME:
            # Behave like the model: call the real merge tool on the real Desk
            # agent, then hand off to Remediation (same run, no new Runner.run).
            merge_tool = next(t for t in resolved.tools if t.name == MERGE_TOOL_NAME)
            arguments = json.dumps({"input": json.dumps({"findings": []})})
            await merge_tool.on_invoke_tool(
                ToolContext(
                    kwargs.get("context"),
                    tool_name=MERGE_TOOL_NAME,
                    tool_call_id="call_merge",
                    tool_arguments=arguments,
                    run_config=kwargs.get("run_config"),
                ),
                arguments,
            )
            return FakeResult(PROPOSAL, last_agent_name=REMEDIATION_SPECIALIST_NAME)
        return FakeResult(RAW[resolved.name])

    review_runner.Runner.run = fake_run
    desk.Runner.run = fake_run


def context(repo: str = "code-review-desk") -> ReviewContext:
    return ReviewContext(repo=repo, language="Python", ruleset_id="python-default")


def review(repo: str = "code-review-desk"):
    report, error = asyncio.run(desk.run_review(DIFF, context(repo)))
    assert error is None, error
    return report


def assert_one_review_one_trace(review_runs: list[dict]) -> str:
    """Every run in one review carries the same group id in its own RunConfig and
    ran inside the same trace. Returns that review's request_id.

    This is the check that fails if any single run — for instance one of the
    three concurrently-built reviewer configs — got a fresh or different id.
    """
    ids = {r["run_config"].group_id for r in review_runs}
    assert len(ids) == 1, f"runs of one review carry different ids: {ids}"
    request_id = ids.pop()
    assert request_id and request_id.startswith("rev_"), request_id

    traces = {r["trace_id"] for r in review_runs}
    assert traces == {trace_id_for(request_id)}, f"runs escaped the review's trace: {traces}"
    for r in review_runs:
        assert r["run_config"].workflow_name == REVIEW_WORKFLOW_NAME, r
        assert r["run_config"].trace_metadata == {"request_id": request_id}, r
        assert r["run_config"].tracing_disabled is False, r
    return request_id


# ---------------------------------------------------------------------------
# One review -> one trace, every run inside it
# ---------------------------------------------------------------------------


def test_every_run_of_a_review_carries_one_id_and_runs_in_one_trace() -> None:
    install_stub()
    review()

    names = [r["agent"] for r in runs]
    # 3 reviewers + Style's forced ruleset lookup (FR-9a) + the Desk + the Merge
    # tool's nested run. Remediation is a
    # handoff inside the Desk's run, so it never appears as a run of its own.
    assert sorted(names) == sorted(REVIEWERS + [STYLE_RULESET_LOOKUP_NAME, desk.DESK_NAME, MERGE_SPECIALIST_NAME]), names
    assert REMEDIATION_SPECIALIST_NAME not in names

    request_id = assert_one_review_one_trace(runs)

    # Exactly one trace was opened for the whole review, keyed off request_id.
    assert len(PROCESSOR.traces) == 1, [t.trace_id for t in PROCESSOR.traces]
    only = PROCESSOR.traces[0]
    assert only.trace_id == trace_id_for(request_id)
    assert TRACE_ID_SHAPE.match(only.trace_id)
    assert only.name == REVIEW_WORKFLOW_NAME
    assert only.export()["group_id"] == request_id


def test_each_concurrent_reviewer_got_its_own_config_instance() -> None:
    install_stub()
    review()
    by_agent = {r["agent"]: r["run_config"] for r in runs}
    configs = [by_agent[name] for name in REVIEWERS]
    # Three separate objects (no shared mutable RunConfig across gather tasks)...
    assert len({id(c) for c in configs}) == 3
    assert all(c is not by_agent[desk.DESK_NAME] for c in configs)
    # ...that nonetheless agree on the review's id.
    assert len({c.group_id for c in configs}) == 1


def test_merge_runs_under_the_reviews_config_via_build_desk() -> None:
    install_stub()
    review()
    by_agent = {r["agent"]: r for r in runs}
    merge, desk_run = by_agent[MERGE_SPECIALIST_NAME], by_agent[desk.DESK_NAME]
    assert merge["run_config"].group_id == desk_run["run_config"].group_id
    assert merge["trace_id"] == desk_run["trace_id"]


def test_ledger_request_id_is_the_trace_key() -> None:
    install_stub()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        unregister = ledger.register_ledger(path=path)
        try:
            review()
        finally:
            unregister()
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    request_id = assert_one_review_one_trace(runs)
    assert {line["request_id"] for line in lines} == {request_id}
    assert PROCESSOR.traces[0].trace_id == trace_id_for(lines[0]["request_id"])


def test_two_reviews_get_two_traces_even_when_concurrent() -> None:
    # Two Chainlit sessions reviewing at the same moment: each review's runs must
    # stay in that review's trace — the active trace travels with each task's
    # own context, not with the process.
    install_stub(delays={SECURITY_REVIEWER_NAME: 0.05, TESTS_REVIEWER_NAME: 0.02})

    async def both():
        return await asyncio.gather(
            desk.run_review(DIFF, context("alpha")),
            desk.run_review(DIFF, context("beta")),
        )

    asyncio.run(both())

    assert len(PROCESSOR.traces) == 2
    trace_ids = {t.trace_id for t in PROCESSOR.traces}
    assert len(trace_ids) == 2
    # Partition runs by trace; each partition is a complete, self-consistent review.
    for trace_id in trace_ids:
        mine = [r for r in runs if r["trace_id"] == trace_id]
        assert sorted(r["agent"] for r in mine) == sorted(
            REVIEWERS + [STYLE_RULESET_LOOKUP_NAME, desk.DESK_NAME, MERGE_SPECIALIST_NAME]
        )
        request_id = assert_one_review_one_trace(mine)
        assert trace_id == trace_id_for(request_id)
    assert all(r["trace_id"] in trace_ids for r in runs)


def test_a_fresh_id_for_one_reviewer_would_be_caught() -> None:
    """Self-check that the grouping assertion has teeth: sabotage the config
    builder so ONE concurrently-built reviewer config gets a fresh id, and confirm
    `assert_one_review_one_trace` rejects the result."""
    install_stub()
    original = review_runner.review_run_config
    calls = {"n": 0}

    def sabotaged(request_id, base=None):
        calls["n"] += 1
        if calls["n"] == 2:  # the second reviewer config built for the gather
            request_id = review_runner.new_request_id()
        return original(request_id, base)

    review_runner.review_run_config = sabotaged
    try:
        review()
    finally:
        review_runner.review_run_config = original
    try:
        assert_one_review_one_trace(runs)
    except AssertionError as exc:
        assert "different ids" in str(exc)
    else:
        raise AssertionError("a reviewer with a fresh id went undetected")


def test_tracing_is_on_by_default() -> None:
    install_stub()
    review()  # no run_config passed, exactly as main.py and app.py call it
    assert all(r["run_config"].tracing_disabled is False for r in runs)
    # A disabled trace is a no-op that never reaches processors; this one did.
    assert len(PROCESSOR.traces) == 1


def test_no_live_code_path_hardcodes_tracing_off() -> None:
    # Article VII.1: no product module or live script may switch tracing off.
    offenders = []
    scanned = list(PROJECT_ROOT.glob("*.py")) + list((PROJECT_ROOT / "scripts").glob("*.py"))
    # Guard against a vacuous pass: if the scan points at the wrong directory it
    # finds nothing and "no offenders" would be meaningless.
    assert {"main.py", "desk.py", "review_runner.py"} <= {p.name for p in scanned}, scanned
    for path in scanned:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "tracing_disabled":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    offenders.append(path.name)
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# The export key
# ---------------------------------------------------------------------------


class _Env:
    """Temporarily set TRACING_EXPORT_KEY; process env wins over .env because
    load_dotenv does not override variables that are already set."""

    def __init__(self, value: str) -> None:
        self.value, self.saved = value, os.environ.get(config.TRACING_KEY_VAR)

    def __enter__(self):
        os.environ[config.TRACING_KEY_VAR] = self.value

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop(config.TRACING_KEY_VAR, None)
        else:
            os.environ[config.TRACING_KEY_VAR] = self.saved


def test_configure_tracing_sets_the_exporter_key_via_the_sdk_setter() -> None:
    captured = []
    original = config.set_tracing_export_api_key
    config.set_tracing_export_api_key = captured.append
    try:
        with _Env("sk-test-not-a-real-key-000000"):
            config.configure_tracing()
    finally:
        config.set_tracing_export_api_key = original
    assert captured == ["sk-test-not-a-real-key-000000"]


def test_configure_tracing_rejects_missing_gemini_and_non_openai_keys() -> None:
    cases = {
        "": "missing or empty",
        "   ": "missing or empty",
        "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0": "looks like a Gemini API key",
        "not-a-key-at-all": "does not look like an OpenAI API key",
    }
    captured = []
    original = config.set_tracing_export_api_key
    config.set_tracing_export_api_key = captured.append
    try:
        for value, expected in cases.items():
            with _Env(value):
                try:
                    config.configure_tracing()
                except config.ConfigError as exc:
                    assert expected in str(exc), (value, str(exc))
                    # Never echo the key itself (Article II.4).
                    if value.strip():
                        assert value.strip() not in str(exc)
                else:
                    raise AssertionError(f"accepted a bad key: {value!r}")
    finally:
        config.set_tracing_export_api_key = original
    assert captured == [], "a rejected key must never reach the exporter"


def test_both_entry_points_configure_tracing_before_reviewing() -> None:
    main_tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))
    main_fn = next(n for n in main_tree.body if getattr(n, "name", None) == "main")
    main_calls = [ast.unparse(n.func) for n in ast.walk(main_fn) if isinstance(n, ast.Call)]
    assert "configure_tracing" in main_calls

    chat_tree = ast.parse((PROJECT_ROOT / "chat_session.py").read_text(encoding="utf-8"))
    handler = next(n for n in chat_tree.body if getattr(n, "name", None) == "handle_message")
    ordered = [ast.unparse(n.func) for n in ast.walk(handler) if isinstance(n, ast.Call)]
    assert "configure_tracing" in ordered
    assert ordered.index("configure_tracing") < ordered.index("run")


def test_trace_id_mapping_matches_the_sdk_shape() -> None:
    request_id = review_runner.new_request_id()
    assert TRACE_ID_SHAPE.match(trace_id_for(request_id))
    assert trace_id_for(request_id)[len("trace_"):] == request_id[len("rev_"):]


if __name__ == "__main__":
    # Standalone run: no pytest fixtures, so set tracing up once, here.
    enable_capturing_tracing()
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
