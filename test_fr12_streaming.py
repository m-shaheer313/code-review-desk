"""Structural tests for FR-12 — streaming, session isolation, and the ledger lock.
No live model call, no Chainlit server.

STREAMING is tested on the real `run_all_reviewers` with `Runner.run` stubbed to
finish each reviewer after a DIFFERENT delay (Security slowest, Tests fastest), so
completion order differs from the agent-list order Security, Tests, Style. A
callback that fired in list order, or all at once after the gather, fails.

SESSIONS are tested through `chat_session.handle_message` — the exact function
app.py calls — with plain stand-ins for `cl.user_session` and `cl.Message.send`.
app.py itself is checked by parsing its source, not importing it, so no Chainlit
runtime is needed.

Run with `python test_fr12_streaming.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import ast
import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

import chat_session
import desk
import ledger
import review_runner
from guardrail import REFUSAL_MESSAGE, ReportRefused
from report import Report
from review_context import ReviewContext
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
)
from test_desk_agent import (
    DIFF,
    FakeResult,
    SECURITY_RAW,
    STYLE_RAW,
    TESTS_RAW,
)
from test_fr9 import ceiling

ROOT = Path(__file__).resolve().parent
LIST_ORDER = [SECURITY_REVIEWER_NAME, TESTS_REVIEWER_NAME, STYLE_REVIEWER_NAME]
# Deliberately NOT list order: Tests first, Style second, Security last.
DELAYS = {SECURITY_REVIEWER_NAME: 0.30, TESTS_REVIEWER_NAME: 0.05, STYLE_REVIEWER_NAME: 0.15}
COMPLETION_ORDER = [TESTS_REVIEWER_NAME, STYLE_REVIEWER_NAME, SECURITY_REVIEWER_NAME]
RAW = {
    SECURITY_REVIEWER_NAME: SECURITY_RAW,
    TESTS_REVIEWER_NAME: TESTS_RAW,
    STYLE_REVIEWER_NAME: STYLE_RAW,
}


def context(**overrides) -> ReviewContext:
    fields = dict(repo="code-review-desk", language="Python", ruleset_id="python-default")
    fields.update(overrides)
    return ReviewContext(**fields)


def install_timed_stub(failures=None, desk_output=None) -> None:
    """Each reviewer finishes after its own DELAYS entry; `failures` raise instead."""
    failures = failures or {}

    async def fake_run(agent=None, input=None, *, starting_agent=None, **kwargs):
        resolved = starting_agent if agent is None else agent
        if resolved.name == desk.DESK_NAME:
            return desk_output
        await asyncio.sleep(DELAYS[resolved.name])
        if resolved.name in failures:
            raise failures[resolved.name]
        return FakeResult(RAW[resolved.name])

    review_runner.Runner.run = fake_run
    desk.Runner.run = fake_run


def desk_report() -> FakeResult:
    return FakeResult(
        Report(findings=[], footer=[], remediation_proposed=False),
        last_agent_name=desk.DESK_NAME,
    )


# ---------------------------------------------------------------------------
# Streaming: per reviewer, in completion order, during the review
# ---------------------------------------------------------------------------


def test_callback_fires_once_per_reviewer_in_completion_order() -> None:
    install_timed_stub()
    seen: list[str] = []
    group = asyncio.run(
        review_runner.run_all_reviewers(
            DIFF, context(), on_reviewer_done=lambda o: seen.append(o.reviewer)
        )
    )
    assert seen == COMPLETION_ORDER, seen
    assert seen != LIST_ORDER
    # The returned structure keeps its fixed order; completion order lives only
    # in the callback.
    assert [o.reviewer for o in group.outcomes] == LIST_ORDER


def test_findings_arrive_during_the_review_not_all_at_once() -> None:
    # FR-12's acceptance criterion, measured: the first reviewer is announced
    # well before the slowest one finishes.
    install_timed_stub()
    stamps: dict[str, float] = {}
    start = time.monotonic()
    asyncio.run(
        review_runner.run_all_reviewers(
            DIFF,
            context(),
            on_reviewer_done=lambda o: stamps.__setitem__(o.reviewer, time.monotonic() - start),
        )
    )
    end = time.monotonic() - start
    assert stamps[TESTS_REVIEWER_NAME] < 0.15, stamps  # ~0.05s in
    assert end - stamps[TESTS_REVIEWER_NAME] > 0.15, (stamps, end)  # long before the end
    assert stamps[TESTS_REVIEWER_NAME] < stamps[STYLE_REVIEWER_NAME] < stamps[SECURITY_REVIEWER_NAME]


def test_each_streamed_outcome_is_already_final() -> None:
    install_timed_stub()
    streamed = {}
    group = asyncio.run(
        review_runner.run_all_reviewers(
            DIFF, context(), on_reviewer_done=lambda o: streamed.__setitem__(o.reviewer, o)
        )
    )
    for outcome in group.outcomes:
        # The very object the callback got is the one in the final result.
        assert streamed[outcome.reviewer] is outcome
        assert all(f.source_reviewer == outcome.reviewer for f in outcome.findings)
        assert outcome.elapsed_ms > 0


def test_failed_reviewer_is_streamed_too_and_others_continue() -> None:
    install_timed_stub(failures={SECURITY_REVIEWER_NAME: ceiling()})
    seen = []
    group = asyncio.run(
        review_runner.run_all_reviewers(DIFF, context(), on_reviewer_done=seen.append)
    )
    assert [o.reviewer for o in seen] == COMPLETION_ORDER
    security = next(o for o in seen if o.reviewer == SECURITY_REVIEWER_NAME)
    assert security.failed is True and "ceiling" in security.error
    assert group.failed_reviewers == [SECURITY_REVIEWER_NAME]


def test_async_callback_is_awaited() -> None:
    install_timed_stub()
    seen = []

    async def callback(outcome):
        await asyncio.sleep(0)
        seen.append(outcome.reviewer)

    asyncio.run(review_runner.run_all_reviewers(DIFF, context(), on_reviewer_done=callback))
    assert seen == COMPLETION_ORDER


def test_a_failing_callback_never_fails_a_reviewer() -> None:
    install_timed_stub()

    def broken(outcome):
        raise RuntimeError("browser went away")

    group = asyncio.run(review_runner.run_all_reviewers(DIFF, context(), on_reviewer_done=broken))
    assert group.failed_reviewers == []
    assert all(o.findings for o in group.outcomes)


def test_fr5_group_time_tracks_the_slowest_not_the_sum() -> None:
    # FR-5's defining property, now saved as a test for the first time. Must
    # hold with the streaming callback in place.
    install_timed_stub()
    group = asyncio.run(
        review_runner.run_all_reviewers(DIFF, context(), on_reviewer_done=lambda o: None)
    )
    assert group.slowest_ms >= 290
    assert group.group_elapsed_ms < group.sum_individual_ms
    assert group.group_elapsed_ms - group.slowest_ms < 100, (
        group.group_elapsed_ms, group.slowest_ms,
    )


def test_still_exactly_one_gather_and_no_as_completed() -> None:
    # Article V.1: launched together, awaited as a group. Streaming must not have
    # replaced the gather.
    tree = ast.parse((ROOT / "review_runner.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    gathers = [c for c in calls if getattr(c.func, "attr", None) == "gather"]
    assert len(gathers) == 1
    assert {k.arg: ast.unparse(k.value) for k in gathers[0].keywords} == {
        "return_exceptions": "True"
    }
    assert not [c for c in calls if getattr(c.func, "attr", None) == "as_completed"]


# ---------------------------------------------------------------------------
# Sessions: isolation, reuse, change, refusal — via the handler app.py calls
# ---------------------------------------------------------------------------


class FakeSession:
    """Stand-in for cl.user_session: a get/set store owned by one session."""

    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value) -> None:
        self._data[key] = value


class Outbox:
    """Stand-in for cl.Message(...).send(): records what the user would see."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


def fresh_session() -> tuple[FakeSession, Outbox]:
    store = FakeSession()
    chat_session.start_session(store)
    return store, Outbox()


def handle(store, outbox, text, run=None) -> None:
    asyncio.run(chat_session.handle_message(store, text, outbox, run=run))


def test_progress_messages_arrive_before_the_final_report() -> None:
    install_timed_stub(desk_output=desk_report())
    store, outbox = fresh_session()
    handle(store, outbox, DIFF)

    progress = [m for m in outbox.messages if "raw finding" in m]
    assert len(progress) == 3
    order = [next(n for n in LIST_ORDER if n in m) for m in progress]
    assert order == COMPLETION_ORDER
    report_index = next(i for i, m in enumerate(outbox.messages) if m.startswith("### Review"))
    assert all(outbox.messages.index(m) < report_index for m in progress)
    assert isinstance(store.get(chat_session.REPORT_KEY), Report)


def test_progress_messages_carry_counts_never_finding_text() -> None:
    # They are sent before FR-8's guardrail has looked at anything.
    install_timed_stub(desk_output=desk_report())
    store, outbox = fresh_session()
    handle(store, outbox, DIFF)
    progress = " ".join(m for m in outbox.messages if "raw finding" in m)
    for finding in SECURITY_RAW + TESTS_RAW + STYLE_RAW:
        assert finding.message not in progress


def test_second_diff_reuses_the_sessions_context() -> None:
    install_timed_stub(desk_output=desk_report())
    store, outbox = fresh_session()
    handle(store, outbox, "context: repo=billing strictness=strict\n" + DIFF)
    first = store.get(chat_session.CONTEXT_KEY)
    handle(store, outbox, DIFF)  # no header
    assert store.get(chat_session.CONTEXT_KEY) is first
    assert first.repo == "billing" and first.strictness == "strict"


def test_header_changes_only_the_keys_it_names() -> None:
    install_timed_stub(desk_output=desk_report())
    store, outbox = fresh_session()
    handle(store, outbox, "context: repo=billing strictness=strict\n" + DIFF)
    handle(store, outbox, "context: language=Go\n" + DIFF)
    ctx = store.get(chat_session.CONTEXT_KEY)
    assert (ctx.repo, ctx.language, ctx.ruleset_id, ctx.strictness) == (
        "billing", "Go", "python-default", "strict",
    )


def test_context_only_message_sets_context_without_reviewing() -> None:
    calls = []

    async def run(*args, **kwargs):
        calls.append(args)
        return None, None

    store, outbox = fresh_session()
    handle(store, outbox, "context: repo=billing", run=run)
    assert calls == []
    assert store.get(chat_session.CONTEXT_KEY).repo == "billing"
    assert outbox.messages[-1].startswith("Review context set")


def test_invalid_context_is_rejected_and_leaves_state_alone() -> None:
    calls = []

    async def run(*args, **kwargs):
        calls.append(args)
        return None, None

    store, outbox = fresh_session()
    handle(store, outbox, "context: repo=billing", run=run)
    before = store.get(chat_session.CONTEXT_KEY)
    for bad in ("context: strictness=loose\n" + DIFF, "context: colour=blue\n" + DIFF,
                "context: repo\n" + DIFF):
        handle(store, outbox, bad, run=run)
        assert outbox.messages[-1].startswith("Context not changed"), outbox.messages[-1]
        assert store.get(chat_session.CONTEXT_KEY) is before
    assert calls == []


def test_two_sessions_never_share_context_or_report() -> None:
    install_timed_stub(desk_output=desk_report())
    store_a, outbox_a = fresh_session()
    store_b, outbox_b = fresh_session()

    async def both():
        # Concurrently, as two browser tabs on one Chainlit process would be.
        await asyncio.gather(
            chat_session.handle_message(store_a, "context: repo=alpha\n" + DIFF, outbox_a),
            chat_session.handle_message(
                store_b, "context: repo=beta strictness=strict\n" + DIFF, outbox_b
            ),
        )

    asyncio.run(both())

    ctx_a, ctx_b = (s.get(chat_session.CONTEXT_KEY) for s in (store_a, store_b))
    assert (ctx_a.repo, ctx_a.strictness) == ("alpha", "normal")
    assert (ctx_b.repo, ctx_b.strictness) == ("beta", "strict")
    assert store_a.get(chat_session.REPORT_KEY) is not store_b.get(chat_session.REPORT_KEY)
    assert not any("beta" in m for m in outbox_a.messages)
    assert not any("alpha" in m for m in outbox_b.messages)


def test_refused_report_is_replaced_and_never_stored() -> None:
    async def refusing_run(*args, **kwargs):
        raise ReportRefused(hits=[])

    store, outbox = fresh_session()
    store.set(chat_session.REPORT_KEY, "an earlier report")
    handle(store, outbox, DIFF, run=refusing_run)
    assert outbox.messages[-1] == REFUSAL_MESSAGE
    assert store.get(chat_session.REPORT_KEY) is None


def _module_level_mutables(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.ListComp, ast.DictComp)):
                found.append(ast.unparse(node)[:60])
            if isinstance(value, ast.Call):
                found.append(ast.unparse(node)[:60])
    return found


def test_no_module_level_session_container_in_app_or_session_logic() -> None:
    assert _module_level_mutables(ROOT / "app.py") == []
    # chat_session.py's only module-level dict is HEADER_KEYS, a read-only
    # key-spelling table that never holds session data.
    leftovers = [m for m in _module_level_mutables(ROOT / "chat_session.py")
                 if not m.startswith("HEADER_KEYS")]
    assert leftovers == [], leftovers


def test_handler_awaits_the_run_and_never_calls_a_sync_variant() -> None:
    for name in ("app.py", "chat_session.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        for forbidden in ("asyncio.run(", "run_sync(", "run_until_complete(", "make_async("):
            assert forbidden not in source, (name, forbidden)
    tree = ast.parse((ROOT / "chat_session.py").read_text(encoding="utf-8"))
    handler = next(n for n in tree.body if getattr(n, "name", None) == "handle_message")
    assert isinstance(handler, ast.AsyncFunctionDef)
    awaited = [ast.unparse(n.value) for n in ast.walk(handler) if isinstance(n, ast.Await)]
    assert any(a.startswith("run(") for a in awaited), awaited


# ---------------------------------------------------------------------------
# Ledger: both entry points register identically; appends are locked
# ---------------------------------------------------------------------------


def _register_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "register_ledger"
    ]


def test_both_entry_points_make_the_identical_registration_call() -> None:
    assert _register_calls(ROOT / "main.py") == ["register_ledger()"]
    assert _register_calls(ROOT / "app.py") == ["register_ledger()"]
    # In app.py it runs from on_app_startup: once per process, not per session.
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    startup = next(n for n in tree.body if getattr(n, "name", None) == "on_app_startup")
    assert "cl.on_app_startup" in [ast.unparse(d) for d in startup.decorator_list]
    assert any(
        isinstance(n, ast.Call) and getattr(n.func, "id", None) == "register_ledger"
        for n in ast.walk(startup)
    )


def test_every_ledger_append_takes_the_lock() -> None:
    class CountingLock:
        def __init__(self) -> None:
            self.inner, self.acquired = threading.Lock(), 0

        def __enter__(self):
            self.inner.acquire()
            self.acquired += 1

        def __exit__(self, *exc):
            self.inner.release()

    original = ledger._WRITE_LOCK
    counting = CountingLock()
    ledger._WRITE_LOCK = counting
    try:
        install_timed_stub(desk_output=desk_report())
        with tempfile.TemporaryDirectory() as tmp:
            unregister = ledger.register_ledger(path=Path(tmp) / "ledger.jsonl")
            try:
                asyncio.run(review_runner.run_all_reviewers(DIFF, context()))
            finally:
                unregister()
    finally:
        ledger._WRITE_LOCK = original
    assert counting.acquired == 3


def test_threaded_appends_never_produce_a_partial_line() -> None:
    """Same-process concurrency: many threads, one writer, one file.

    Honest limit of this test: a short single write() is often atomic on its own,
    so this shows the property holds under load but cannot, by itself, prove the
    lock is the reason. `test_every_ledger_append_takes_the_lock` covers that.
    Cross-process safety is not tested because it is not provided — see ledger.py.
    """
    from review_runner import ReviewerOutcome

    threads_n, per_thread = 8, 50
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        writer = ledger.LedgerWriter(path)
        outcome = ReviewerOutcome(reviewer=SECURITY_REVIEWER_NAME, elapsed_ms=5)

        def burst(index: int) -> None:
            for n in range(per_thread):
                writer(f"rev_{index:02d}_{n:03d}" + "x" * 200, outcome)

        workers = [threading.Thread(target=burst, args=(i,)) for i in range(threads_n)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == threads_n * per_thread
    parsed = [json.loads(line) for line in lines]  # any torn line would raise here
    assert len({p["request_id"] for p in parsed}) == threads_n * per_thread


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
