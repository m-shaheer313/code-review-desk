"""The Merge Specialist (FR-6), exposed to the Desk as a callable tool.

Why a tool and not a handoff (spec.md §4.6's required justification): Merge's
output is consumed by the Desk and re-presented in the Desk's own voice — the
Desk, not Merge, remains the entity speaking to the user. So Merge is a callable
capability, `.as_tool(...)`, and the Desk keeps the conversation.

This agent gets no tools of its own: it reorganizes what it is handed and needs
nothing from the filesystem or the run context.
"""

import json

from agents import Agent, ModelSettings, RunConfig, Tool

from finding import Finding
from review_context import ReviewContext
from reviewers import shared_model

MERGE_SPECIALIST_NAME = "MergeSpecialist"

# Merge has no tools, so one turn is all it needs; the ceiling exists because
# Article VI.2 requires every run to have one.
MERGE_MAX_TURNS = 2

# Merge is a mechanical task — dedupe and order. Variety is a defect here, so the
# temperature is the lowest of any agent in the system (plan.md §2: "low").
MERGE_TEMPERATURE = 0.0
MERGE_MAX_TOKENS = 4096

MERGE_TOOL_NAME = "merge_findings"
MERGE_TOOL_DESCRIPTION = (
    "Deduplicate and severity-order the findings from all three reviewers. Input "
    'is a JSON object: {"findings": [ ... ]}, where each element has the keys '
    "file, line, severity, message and source_reviewer. Pass every finding you "
    "received, from every reviewer, in that one array. Returns the same findings "
    "deduplicated and ordered critical first, then major, then minor. It never "
    "adds, invents, or rewrites findings."
)

# The key the Desk serializes findings under, and the key Merge is told to expect.
# Stated in both places so parsing is a contract, not an inference.
MERGE_INPUT_KEY = "findings"

MERGE_INSTRUCTIONS = (
    "You are a merge specialist. You are given the findings produced by three "
    "independent code reviewers (security, tests, style) over the same diff. You "
    "have exactly two jobs: deduplicate, then order.\n\n"
    "INPUT FORMAT. Your input is a single JSON object with exactly one key, "
    f'"{MERGE_INPUT_KEY}", whose value is an array of finding objects. Each '
    "finding object has these keys, and only these:\n"
    '  "file": string — the path from the diff\n'
    '  "line": integer — the line number\n'
    '  "severity": string — exactly one of "critical", "major", "minor"\n'
    '  "message": string — the finding text, never empty\n'
    '  "source_reviewer": string — which reviewer produced it, one of '
    '"SecurityReviewer", "TestsReviewer", "StyleReviewer"\n'
    "The array may be empty. It is not sorted and may contain duplicates across "
    "reviewers — that is exactly what you are here to fix. Parse the JSON; do not "
    "treat it as prose, and do not answer questions about it.\n\n"
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
    "finding you kept. If the input array is empty, return an empty list."
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


async def _merge_output_as_json(run_result) -> str:
    """Serialize Merge's `list[Finding]` back to JSON for the caller.

    Without this, the default extractor hands back whatever the final message
    happened to look like — for a structured-output agent that is the raw strict
    wrapper (`{"response": [...]}`), which makes the caller guess at the shape.
    Emitting the same `{"findings": [...]}` contract Merge was given keeps both
    ends of the tool boundary on one format.
    """
    output = getattr(run_result, "final_output", None)
    if isinstance(output, list):
        return json.dumps(
            {
                MERGE_INPUT_KEY: [
                    finding.model_dump()
                    for finding in output
                    if isinstance(finding, Finding)
                ]
            }
        )
    # Not the expected shape — hand the raw text back and let the caller fall back
    # to the unmerged union (plan.md §3) rather than raising.
    return str(output)


def as_merge_tool(run_config: RunConfig | None = None) -> Tool:
    """Merge, wrapped as the tool the Desk calls (plan.md §2, §3).

    `max_turns` is set because Article VI.2 bounds every run, and Merge's job is a
    single turn — it has no tools to call.
    """
    return build_merge_specialist().as_tool(
        tool_name=MERGE_TOOL_NAME,
        tool_description=MERGE_TOOL_DESCRIPTION,
        custom_output_extractor=_merge_output_as_json,
        max_turns=MERGE_MAX_TURNS,
        run_config=run_config,
    )
