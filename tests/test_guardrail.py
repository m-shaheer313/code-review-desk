"""Tests for FR-8's credential guardrail. No model call, no network.

Run with `pytest tests/test_guardrail.py`, or standalone: `python tests/test_guardrail.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import asyncio

from finding import Finding
from guardrail import (
    ReportRefused,
    credential_output_guardrail,
    scan_report,
    scan_text,
)
from report import Report, ReviewerFooterRow

# The exact text the live Remediation Specialist produced on 2026-09-24. It is a
# paraphrased credential, not a copy of the diff's value, which is precisely why
# an equality check against the original would have missed it.
LIVE_PROPOSAL = (
    "This is a PROPOSAL ONLY: nothing has been applied, committed, or pushed.\n\n"
    "The live API secret key hardcoded at line 6 of `billing/refunds.py` has been "
    "exposed in the source code.\n\n"
    "```diff\n"
    "--- billing/refunds.py\n"
    "+++ billing/refunds.py\n"
    "+API_SECRET_KEY = os.environ.get(\"BILLING_API_SECRET_KEY\")\n"
    "-API_SECRET_KEY = \"sk_live_12345EXAMPLESECRETKEY\"\n"
    "```\n"
)

# Real finding messages from the same live run — all must pass.
LIVE_CLEAN_MESSAGES = [
    "A live API secret key has been hardcoded into the source code; remove the "
    "credential and load it from environment variables or a secure secret manager "
    "instead.",
    "PY003: Do not use wildcard imports (from x import *).",
    "PY001: Functions use snake_case; use process_refund instead of ProcessRefund.",
    "PY004: Every public function and method annotates its parameters and its "
    "return type.",
    "PY007: Every public function has a docstring.",
    "PY009: f-strings are preferred over string concatenation.",
]


def clean_report(messages=None, proposal=None) -> Report:
    return Report(
        findings=[
            Finding(
                file="billing/refunds.py",
                line=index + 1,
                severity="minor",
                message=message,
                source_reviewer="StyleReviewer",
            )
            for index, message in enumerate(messages or LIVE_CLEAN_MESSAGES)
        ],
        footer=[ReviewerFooterRow(reviewer="StyleReviewer", ms=100, tokens=0)],
        remediation_proposed=proposal is not None,
        remediation_proposal=proposal,
    )


# --- the required live case ------------------------------------------------


def test_live_paraphrased_credential_is_caught() -> None:
    hits = scan_text(LIVE_PROPOSAL, "remediation proposal")
    assert hits, "the live proposal's sk_live_ value was not caught"
    assert any("secret key" in hit.kind for hit in hits), [h.kind for h in hits]


def test_live_proposal_in_a_report_is_refused() -> None:
    report = clean_report(proposal=LIVE_PROPOSAL)
    assert scan_report(report)


# --- no false positives ---------------------------------------------------


def test_discussing_the_word_password_passes() -> None:
    assert scan_text('avoid hardcoding the word "password" as a variable name', "x") == []
    assert scan_text("a variable literally named password is not itself a leak", "x") == []
    assert scan_text('the key "secret" should not appear in a config file name', "x") == []


def test_every_live_finding_message_passes() -> None:
    for message in LIVE_CLEAN_MESSAGES:
        assert scan_text(message, "finding") == [], message
    assert scan_report(clean_report()) == []


def test_ordinary_code_prose_passes() -> None:
    for text in (
        "use a parameterized query instead of string concatenation",
        "Authorization header should be set from the environment",
        "rename ProcessRefund to process_refund per PY001",
        "no test accompanies the new refund() path in billing/refunds.py",
        "the connection to generativelanguage.googleapis.com is not verified",
        "password = os.environ['BILLING_PASSWORD']",
        'api_key = "<your-key-here>"',
        'token = ""',
        'secret = "CHANGEME"',
    ):
        assert scan_text(text, "x") == [], text


# --- credential shapes that must be caught --------------------------------


def test_known_prefixes_are_caught() -> None:
    for text in (
        'STRIPE_KEY = "sk_live_51MZq8vKdR7xWpN3bQfHjLc9TgYeA2sVuX4mB6nD8kJhF0rPzQw"',
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
        "AIzaSyD-fake-key-material-here-0123456789",
        "xoxb-1234567890-abcdefghijkl",
        "glpat-abcdefghijklmnopqrst",
        "-----BEGIN RSA PRIVATE KEY-----",
    ):
        assert scan_text(text, "x"), text


def test_bearer_and_jwt_shapes_are_caught() -> None:
    assert scan_text("Authorization: Bearer abcdef0123456789abcdef", "x")
    assert scan_text(
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dQw4w9WgXcQ",
        "x",
    )


def test_password_assignment_with_a_real_looking_value_is_caught() -> None:
    assert scan_text('password = "hunter2xyzzy"', "x")
    assert scan_text("client_secret: 'Gx9PmQ2vLk8Rt4Wn'", "x")


def test_high_entropy_token_is_caught() -> None:
    assert scan_text("leftover value 9fK2mQ7xZ1vB4nR8tY3wL6pJ5hD0sA2g", "x")


# --- the SDK guardrail wrapper --------------------------------------------


class FakeAgent:
    name = "Desk"


def run_guardrail(output):
    result = asyncio.run(credential_output_guardrail.run(None, FakeAgent(), output))
    return result.output.tripwire_triggered


def test_guardrail_trips_on_a_dirty_report() -> None:
    assert run_guardrail(clean_report(proposal=LIVE_PROPOSAL)) is True


def test_guardrail_passes_a_clean_report() -> None:
    assert run_guardrail(clean_report()) is False


def test_guardrail_checks_plain_text_output_too() -> None:
    # After a handoff the final output is the specialist's text, not a Report.
    assert run_guardrail(LIVE_PROPOSAL) is True
    assert run_guardrail("PROPOSAL ONLY: rotate the key and load it from the env") is False


def test_guardrail_fails_toward_refusal_when_the_scan_errors() -> None:
    class Exploding:
        @property
        def findings(self):
            raise RuntimeError("boom")

    assert run_guardrail(Exploding()) is True


# --- the refusal object ----------------------------------------------------


def test_refusal_never_quotes_the_offending_value() -> None:
    hits = scan_text(LIVE_PROPOSAL, "remediation proposal")
    exc = ReportRefused(hits)
    rendered = str(exc) + " ".join(hit.describe() for hit in hits)
    assert "sk_live_12345EXAMPLESECRETKEY" not in rendered
    assert "12345EXAMPLESECRETKEY" not in rendered


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
