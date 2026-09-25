"""pytest configuration: import-order guarantee and per-test isolation.

WHY THIS FILE EXISTS. Run as `python test_x.py`, every test file got its own
process. Under `pytest` they all share ONE process, and pytest imports every test
module during collection, before running any test. Two kinds of process-wide
state then leak between files; both were observed, not hypothesized (9 failures
on the first pytest run):

1. `Runner.run` stubs. Several files stub the SDK's `Runner.run` by assigning to
   the `Runner` class, which every module shares. A stub left installed by one
   file made test_fr10_hooks' real-runner tests run against a fake (8 failures),
   and made one test_fr3_fr4 test pass for the wrong reason.
2. Tracing on/off. test_fr13_tracing needs tracing ON. Its override, applied at
   import during collection, switched tracing on for the whole session, so
   test_trace_guard saw a trace created during a test run (1 failure).

WHAT IT DOES.
- Imports `testing_env` before anything else. pytest loads conftest.py before any
  test module, so OPENAI_AGENTS_DISABLE_TRACING is set before any code can create
  a trace — regardless of the order pytest collects files in. The per-file
  `import testing_env` lines stay: they give the same guarantee when a file is
  run on its own with `python test_x.py`.
- Resets, before and after every test: the real `Runner.run`, tracing switched
  off, and the ledger's run-observer list emptied. A test that needs something
  else (test_fr13_tracing) sets it up in its own fixture, which runs after this.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import pytest
from agents import Runner, set_tracing_disabled

import review_runner

# The genuine classmethod descriptor, captured before any test module can stub
# it. Restoring the descriptor (not a bound method) puts Runner back exactly as
# the SDK defined it.
_REAL_RUNNER_RUN = Runner.__dict__["run"]


def _reset_process_wide_state() -> None:
    Runner.run = _REAL_RUNNER_RUN
    # A manual override, which the SDK ranks above the env flag — so it also
    # cancels any earlier set_tracing_disabled(False).
    set_tracing_disabled(True)
    review_runner._run_observers.clear()


@pytest.fixture(autouse=True)
def isolate_process_wide_state():
    _reset_process_wide_state()
    yield
    _reset_process_wide_state()
