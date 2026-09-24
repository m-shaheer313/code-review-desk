"""The Remediation Specialist (FR-6), reached by handoff.

Why a handoff and not a tool (spec.md §4.6's required justification): once a
critical security issue is confirmed, the specialist's own judgment and voice
should reach the user directly. Authorship of the reply transfers — the Desk is
not qualified to speak for a fix it did not design. So this is a plain `Agent`
used as a handoff target, never wrapped in `.as_tool(...)`.

`output_type` is left unset, i.e. plain text. plan.md §2 says "text/patch
proposal" for this agent and specifies no model, so a free-form string is the
right shape: a patch with prose around it does not fit `list[Finding]`, and
forcing a structured type here would fight the one requirement that matters —
that the proposal reaches the user in the specialist's own voice.

NG-2 is absolute: this system proposes and never applies. The instructions say so,
and the agent has no tools, so it has no way to touch a file even if it wanted to.
"""

from agents import Agent, ModelSettings

from finding import Finding
from review_context import ReviewContext
from reviewers import SECURITY_REVIEWER_NAME, shared_model

REMEDIATION_SPECIALIST_NAME = "RemediationSpecialist"

# Writing a fix needs a little more latitude than classifying one, but this is
# still security-critical output — plan.md §2 says "moderate".
REMEDIATION_TEMPERATURE = 0.3
REMEDIATION_MAX_TOKENS = 4096

REMEDIATION_HANDOFF_DESCRIPTION = (
    "Hand off to the remediation specialist when the merged findings contain at "
    "least one critical finding from the security reviewer. The specialist "
    "proposes a fix directly to the user."
)

REMEDIATION_INSTRUCTIONS = (
    "You are a remediation specialist. A code review has just found one or more "
    "CRITICAL security problems, and you are now speaking directly to the "
    "developer who wrote the code.\n\n"
    "For each critical finding you were given, propose a concrete fix: either a "
    "small patch in unified-diff form, or numbered step-by-step corrections when a "
    "patch would be misleading (for example when the fix is to rotate a leaked "
    "credential, or when the correct change spans files you cannot see). Be "
    "specific — name the function, the parameter, the call to replace. A fix the "
    "developer cannot act on is not a fix.\n\n"
    "Open your reply by stating plainly that this is a PROPOSAL ONLY: nothing has "
    "been applied, committed, or pushed, and the developer decides whether to use "
    "it. Never claim or imply that you changed anything, and never offer to apply "
    "it — you cannot.\n\n"
    "If a finding concerns a leaked credential, do NOT repeat the credential's "
    "value anywhere in your reply, not even partially. Refer to it by location and "
    "kind, and say that it must be treated as compromised and rotated — removing "
    "it from the code is not enough once it has been committed.\n\n"
    "Address only the critical findings. Do not review the code for new problems, "
    "do not comment on style or test coverage, and do not re-litigate the "
    "severity you were handed. If something is genuinely ambiguous, say what you "
    "would need to know rather than guessing at a fix."
)


def build_remediation_specialist() -> Agent[ReviewContext]:
    """The Remediation Specialist: a plain agent, used as a handoff target.

    No tools by design (NG-2) and no `output_type`, so its `final_output` is the
    proposal text itself.
    """
    return Agent[ReviewContext](
        name=REMEDIATION_SPECIALIST_NAME,
        instructions=REMEDIATION_INSTRUCTIONS,
        handoff_description=REMEDIATION_HANDOFF_DESCRIPTION,
        model=shared_model(),
        model_settings=ModelSettings(
            temperature=REMEDIATION_TEMPERATURE,
            max_tokens=REMEDIATION_MAX_TOKENS,
        ),
        tools=[],
    )


def decide_needs_remediation(findings: list[Finding]) -> bool:
    """Whether this review should hand off to Remediation (FR-6).

    True if and only if at least one finding is BOTH `"critical"` AND came from
    the Security Reviewer — spec.md §4.6's wording is "a critical security
    finding", not merely a critical one. Tests and Style have their own severity
    ladders that can legitimately reach `"critical"` (a deleted test guarding
    something dangerous, for instance), and neither should summon a security fix.

    `source_reviewer` is stamped by `review_runner` from the run that produced the
    finding, never taken from model output, so an unstamped or model-invented
    value cannot trigger the handoff.
    """
    return any(
        finding.severity == "critical"
        and finding.source_reviewer == SECURITY_REVIEWER_NAME
        for finding in findings
    )
