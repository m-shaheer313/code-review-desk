"""The Desk's own output structure (plan.md §4.3, FR-6/FR-10).

`footer` rows carry per-reviewer latency and token counts. Latency is real from
FR-5's timing; `tokens` stays 0 until FR-10 wires the run-level hooks that read
genuine usage data — Article VII.3 forbids estimated or hardcoded counts, so 0
here means "not measured yet", not "measured as zero".
"""

from pydantic import BaseModel

from finding import Finding


class ReviewerFooterRow(BaseModel):
    reviewer: str
    ms: int
    tokens: int
    partial: bool = False  # true if this reviewer hit its ceiling or failed


class Report(BaseModel):
    findings: list[Finding]
    footer: list[ReviewerFooterRow]
    remediation_proposed: bool
    # Not in plan.md §4.3: that model assumed Remediation would speak to the user
    # directly through the SDK handoff, so its text never passed through the
    # Report. Until the Desk is an agent with a real handoff, the orchestrator
    # captures the proposal and carries it here.
    remediation_proposal: str | None = None
    # Notes about the review process itself (a failed reviewer, a merge that fell
    # back to the unmerged union). Shown to the user alongside the findings; never
    # silently dropped (Article VIII.3).
    notes: list[str] = []

    @property
    def is_partial(self) -> bool:
        return any(row.partial for row in self.footer)
