"""The Merge Specialist (FR-6), exposed to the Desk as a callable tool.

Why a tool and not a handoff (spec.md §4.6's required justification): Merge's
output is consumed by the Desk and re-presented in the Desk's own voice — the
Desk, not Merge, remains the entity speaking to the user. So Merge is a callable
capability, `.as_tool(...)`, and the Desk keeps the conversation.

This agent gets no tools of its own: it reorganizes what it is handed and needs
nothing from the filesystem or the run context.
"""

from agents import Agent, ModelSettings, Tool

from finding import Finding
from review_context import ReviewContext
from reviewers import shared_model

MERGE_SPECIALIST_NAME = "MergeSpecialist"

# Merge is a mechanical task — dedupe and order. Variety is a defect here, so the
# temperature is the lowest of any agent in the system (plan.md §2: "low").
MERGE_TEMPERATURE = 0.0
MERGE_MAX_TOKENS = 4096

MERGE_TOOL_NAME = "merge_findings"
MERGE_TOOL_DESCRIPTION = (
    "Deduplicate and severity-order the findings from all three reviewers. Pass "
    "every finding you received, from every reviewer, as one collection. Returns "
    "the same findings deduplicated and ordered critical first, then major, then "
    "minor. It never adds, invents, or rewrites findings."
)

MERGE_INSTRUCTIONS = (
    "You are a merge specialist. You are given the findings produced by three "
    "independent code reviewers (security, tests, style) over the same diff. You "
    "have exactly two jobs: deduplicate, then order.\n\n"
    "DEDUPLICATE. Two findings are the same issue when they describe the same "
    "problem at the same place: identical file path, the same line or a line "
    "within one or two of it, and messages that mean the same thing even if the "
    "wording differs. This happens because reviewers overlap — a hardcoded "
    "credential can be reported by security and by style in the same breath. When "
    "you merge duplicates, keep ONE finding: the highest severity of the group, "
    "the clearest message of the group, and the source_reviewer of the finding "
    "whose severity you kept. Findings at the same line that describe genuinely "
    "different problems are NOT duplicates — keep both.\n\n"
    "ORDER. Return the surviving findings sorted by severity: all 'critical' "
    "first, then all 'major', then all 'minor'. Within one severity, keep the "
    "order you received them in.\n\n"
    "You MUST NOT invent a finding, add a finding of your own, change a severity "
    "except by keeping the higher one of a duplicate group, rewrite a message into "
    "something it did not say, or drop a finding that is not a duplicate. You are "
    "reorganizing someone else's work, not reviewing the code. You never see the "
    "diff and must not ask for it.\n\n"
    "Copy file, line, severity and source_reviewer through unchanged from the "
    "finding you kept. If you were given no findings, return an empty list."
)


def build_merge_specialist() -> Agent[ReviewContext]:
    """The Merge Specialist agent. Not a handoff target — see `as_merge_tool`."""
    return Agent[ReviewContext](
        name=MERGE_SPECIALIST_NAME,
        instructions=MERGE_INSTRUCTIONS,  # static: merging does not vary per run
        model=shared_model(),
        model_settings=ModelSettings(
            temperature=MERGE_TEMPERATURE,
            max_tokens=MERGE_MAX_TOKENS,
        ),
        output_type=list[Finding],
        tools=[],
    )


def as_merge_tool() -> Tool:
    """Merge, wrapped as the tool the Desk calls (plan.md §2, §3)."""
    return build_merge_specialist().as_tool(
        tool_name=MERGE_TOOL_NAME,
        tool_description=MERGE_TOOL_DESCRIPTION,
    )
