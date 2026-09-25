"""Test-only environment guard: tests must never export real traces.

Every `test_*.py` imports this module FIRST, before anything that could create
a trace:

    import testing_env  # noqa: F401 — must stay the first import

WHY. Tracing is on by default for live reviews (FR-13, Article VII.1), and the
stubbed test suites open real `trace()` contexts. Today nothing is exported only
because tests never call `configure_tracing()` and OPENAI_API_KEY is unset. If
OPENAI_API_KEY were ever set system-wide, the SDK's default exporter would pick it
up and ship test traces to OpenAI's platform just by running the tests.

HOW. The SDK's own switch, OPENAI_AGENTS_DISABLE_TRACING. It is read ONCE, the
first time a trace is created, then cached (agents/tracing/provider.py,
`_refresh_disabled_flag`). Setting it here, at the top of every test file,
guarantees it is in place before that first read, whichever file is launched.
With it set, no trace is created at all, so there is nothing to export.

It is set unconditionally rather than with setdefault: the point is protection,
and an inherited "0" or "false" must not quietly switch it back off.

The one deliberate exception is test_fr13_tracing.py, which needs tracing ON to
test it. It first replaces the exporter with a capturing processor
(`set_trace_processors`) and only then re-enables tracing with
`set_tracing_disabled(False)`, which the SDK lets take precedence over this env
flag. In that order there is no moment where tracing is on and the real exporter
is still installed.

Never import this from product code or from scripts/ — the live scripts must
trace.
"""

import os
import sys
from pathlib import Path

TRACING_DISABLE_VAR = "OPENAI_AGENTS_DISABLE_TRACING"

os.environ[TRACING_DISABLE_VAR] = "1"

# Tests live in tests/, the product modules at the project root. Under pytest,
# pyproject.toml's `pythonpath = ["."]` puts the root on sys.path. A standalone
# `python tests/test_x.py` run gets only tests/ on sys.path, so the root is added
# here — this module is the first import of every test file, so it happens before
# any product import.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
