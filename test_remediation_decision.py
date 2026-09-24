"""Tests for `decide_needs_remediation` (FR-6) — no model call, no network.

Run with `pytest test_remediation_decision.py` or `python test_remediation_decision.py`.
"""

from finding import Finding
from remediation import decide_needs_remediation
from reviewers import (
    SECURITY_REVIEWER_NAME,
    STYLE_REVIEWER_NAME,
    TESTS_REVIEWER_NAME,
)


def f(severity: str, source: str, message: str = "x") -> Finding:
    return Finding(
        file="a.py",
        line=1,
        severity=severity,
        message=message,
        source_reviewer=source,
    )


def test_no_findings_no_handoff() -> None:
    assert decide_needs_remediation([]) is False


def test_critical_security_finding_triggers_handoff() -> None:
    assert decide_needs_remediation([f("critical", SECURITY_REVIEWER_NAME)]) is True


def test_critical_from_tests_does_not_trigger() -> None:
    # Tests' own ladder allows "critical" for a deleted test guarding something
    # dangerous. That is not a security finding (spec.md §4.6).
    assert decide_needs_remediation([f("critical", TESTS_REVIEWER_NAME)]) is False


def test_critical_from_style_does_not_trigger() -> None:
    assert decide_needs_remediation([f("critical", STYLE_REVIEWER_NAME)]) is False


def test_non_critical_security_finding_does_not_trigger() -> None:
    assert decide_needs_remediation([f("major", SECURITY_REVIEWER_NAME)]) is False
    assert decide_needs_remediation([f("minor", SECURITY_REVIEWER_NAME)]) is False


def test_both_conditions_must_hold_on_the_same_finding() -> None:
    # A critical from Tests plus a major from Security must NOT trigger: neither
    # finding is a critical security finding on its own.
    mixed = [f("critical", TESTS_REVIEWER_NAME), f("major", SECURITY_REVIEWER_NAME)]
    assert decide_needs_remediation(mixed) is False


def test_one_qualifying_finding_among_many_triggers() -> None:
    many = [
        f("minor", STYLE_REVIEWER_NAME),
        f("major", TESTS_REVIEWER_NAME),
        f("critical", SECURITY_REVIEWER_NAME, "hardcoded credential"),
        f("minor", SECURITY_REVIEWER_NAME),
    ]
    assert decide_needs_remediation(many) is True


def test_multiple_criticals_still_a_single_boolean() -> None:
    # spec.md §4.6: more than one critical still means exactly one handoff.
    two = [
        f("critical", SECURITY_REVIEWER_NAME, "sql injection"),
        f("critical", SECURITY_REVIEWER_NAME, "leaked key"),
    ]
    assert decide_needs_remediation(two) is True


def test_unstamped_source_does_not_trigger() -> None:
    # The default is empty, so a finding that never went through review_runner's
    # stamping cannot summon a security fix.
    assert decide_needs_remediation([f("critical", "")]) is False


def test_model_supplied_source_cannot_be_trusted_but_is_overwritten() -> None:
    # A model could put anything in source_reviewer; only the exact reviewer name
    # stamped by review_runner counts.
    for spoofed in ("securityreviewer", "Security", "SecurityReviewer ", "Desk"):
        assert decide_needs_remediation([f("critical", spoofed)]) is False


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
