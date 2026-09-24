"""Output guardrail: nothing credential-shaped reaches the user (FR-8).

Article II.4 is the requirement this serves, and it is broader than "don't log our
own key": a secret found *inside a reviewed diff* must not appear in a finding
message or a remediation proposal either. The diffs this system reviews are
exactly the place where credentials get committed, so a reviewer quoting the line
it is complaining about is the single most likely leak path.

Detection targets **value shapes**, not vocabulary (spec.md §4.8's edge case). A
finding that says 'avoid naming a variable "password"' is ordinary review prose
and passes; `password = "hunter2xyz"` does not.

The scanner is a plain function so it can be unit-tested and reused. It is wired
into the SDK two ways in `desk.py`: as an `@output_guardrail` on the Desk and on
the Remediation Specialist (so a handoff's text is checked too), and as a final
sweep over the assembled Report.
"""

import math
import re
from dataclasses import dataclass

from agents import GuardrailFunctionOutput, RunContextWrapper, output_guardrail

REFUSAL_MESSAGE = (
    "This review was withheld. The report contained text shaped like a real "
    "credential, which must never be echoed back — the value may have been copied "
    "out of the diff under review. Nothing is shown rather than risk repeating a "
    "secret. Rotate any credential you believe is in this diff, remove it from the "
    "code, and review again."
)

# Well-known credential prefixes. Matching a prefix is enough on its own: these
# strings do not occur by accident.
_PREFIX_PATTERNS = [
    (r"\bsk[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9]{8,}", "stripe/openai-style secret key"),
    (r"\bpk_(?:live|test)_[A-Za-z0-9]{8,}", "publishable-key shape"),
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}", "aws access key id"),
    (r"\bgh[pousr]_[A-Za-z0-9]{16,}", "github token"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}", "github fine-grained pat"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "slack token"),
    (r"\bAIza[0-9A-Za-z_-]{20,}", "google api key"),
    (r"\bya29\.[0-9A-Za-z_-]{10,}", "google oauth token"),
    (r"\bglpat-[0-9A-Za-z_-]{16,}", "gitlab pat"),
    (r"\bnpm_[A-Za-z0-9]{20,}", "npm token"),
    (r"\bdop_v1_[a-f0-9]{32,}", "digitalocean token"),
    (r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", "sendgrid key"),
    (r"\bhvs\.[A-Za-z0-9_-]{20,}", "vault token"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}", "jwt"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "private key block"),
]

# `Authorization: Bearer <token>` with something substantial after it.
_BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=]{16,}", re.IGNORECASE)

# A secret-ish name assigned a quoted literal: password = "…", api_key: '…'.
_SECRET_NAMES = (
    r"pass(?:word|wd|phrase)?|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|auth|credential|bearer"
)
_ASSIGNMENT_PATTERN = re.compile(
    rf"(?:{_SECRET_NAMES})\s*[:=]\s*[\"']([^\"'\n]{{8,}})[\"']",
    re.IGNORECASE,
)

# Values that look like an assignment but carry no secret: placeholders, env
# lookups, obvious redactions.
_PLACEHOLDER_PATTERN = re.compile(
    r"^(?:\s*|<[^>]*>|\{+[^}]*\}+|\$\{?[A-Z_]+\}?|x{3,}|\*{3,}|\.{3,}|REDACTED"
    r"|CHANGEME|TODO|FIXME|your[-_ ]?\w+[-_ ]?here|example|placeholder|dummy"
    r"|os\.environ.*|getenv.*|None|null|true|false)$",
    re.IGNORECASE,
)

# High-entropy candidates: long, unbroken, mixed-class strings.
_TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_ENTROPY_THRESHOLD = 3.6  # bits per character


@dataclass(frozen=True)
class CredentialHit:
    kind: str
    where: str  # which part of the report, never the offending value itself

    def describe(self) -> str:
        # The value is deliberately NOT included: a guardrail that reports what it
        # caught by quoting it defeats its own purpose (Article II.4).
        return f"{self.kind} in {self.where}"


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _looks_high_entropy(value: str) -> bool:
    """Long, mixed-class and high-entropy — the shape of a random token.

    Requiring all three keeps ordinary long identifiers out: a dotted hostname is
    long but single-class, and a snake_case function name is long but low entropy.
    """
    classes = sum(
        (
            bool(re.search(r"[a-z]", value)),
            bool(re.search(r"[A-Z]", value)),
            bool(re.search(r"[0-9]", value)),
        )
    )
    if classes < 3:
        return False
    return _shannon_entropy(value) >= _ENTROPY_THRESHOLD


def scan_text(text: str, where: str) -> list[CredentialHit]:
    """Every credential-shaped thing in one piece of text. Never raises."""
    if not isinstance(text, str) or not text.strip():
        return []

    hits: list[CredentialHit] = []

    for pattern, kind in _PREFIX_PATTERNS:
        if re.search(pattern, text):
            hits.append(CredentialHit(kind=kind, where=where))

    if _BEARER_PATTERN.search(text):
        hits.append(CredentialHit(kind="bearer token", where=where))

    for value in _ASSIGNMENT_PATTERN.findall(text):
        if _PLACEHOLDER_PATTERN.match(value.strip()):
            continue
        hits.append(CredentialHit(kind="secret assigned to a named variable", where=where))

    for candidate in _TOKEN_CANDIDATE.findall(text):
        if _looks_high_entropy(candidate):
            hits.append(CredentialHit(kind="high-entropy token", where=where))

    return hits


def scan_report(report) -> list[CredentialHit]:
    """Scan every user-facing part of a Report: finding messages, file paths, the
    remediation proposal, and the notes. Never raises — a scanner that throws
    would be indistinguishable from a clean report to its caller."""
    hits: list[CredentialHit] = []
    for index, finding in enumerate(getattr(report, "findings", []) or []):
        hits.extend(scan_text(getattr(finding, "message", ""), f"finding {index + 1}"))
        hits.extend(scan_text(getattr(finding, "file", ""), f"finding {index + 1} path"))
    hits.extend(
        scan_text(getattr(report, "remediation_proposal", "") or "", "remediation proposal")
    )
    for index, note in enumerate(getattr(report, "notes", []) or []):
        hits.extend(scan_text(note, f"note {index + 1}"))
    return hits


class ReportRefused(Exception):
    """The assembled report could not be shown. Carries the user-facing refusal.

    Raised by the final sweep in `desk.run_review`. The SDK's own
    `OutputGuardrailTripwireTriggered` covers the agent-level checks; both are
    caught at the same single place in `main.py`.
    """

    def __init__(self, hits: list[CredentialHit]) -> None:
        self.hits = hits
        super().__init__(REFUSAL_MESSAGE)


@output_guardrail(name="credential_shape_check")
async def credential_output_guardrail(
    ctx: RunContextWrapper, agent, agent_output
) -> GuardrailFunctionOutput:
    """SDK output guardrail, attached to the Desk and to Remediation.

    Fails toward refusal: if the scan itself errors, the tripwire trips, because
    "could not confirm the report is safe" must not be shown as a safe report
    (spec.md §4.8's second edge case).
    """
    try:
        if isinstance(agent_output, str):
            hits = scan_text(agent_output, f"{agent.name} output")
        else:
            hits = scan_report(agent_output)
    except Exception as exc:  # noqa: BLE001 — deliberate: unknown means unsafe
        return GuardrailFunctionOutput(
            output_info={"error": f"guardrail could not run: {type(exc).__name__}"},
            tripwire_triggered=True,
        )

    return GuardrailFunctionOutput(
        output_info={"hits": [hit.describe() for hit in hits]},
        tripwire_triggered=bool(hits),
    )
