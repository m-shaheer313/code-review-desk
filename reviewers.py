"""Reviewer agents (FR-3 + FR-4 slice: Base and Style only).

Security and Tests reviewers, concurrency, Merge and the Desk come later.

Constitutional notes:
- Article I.1/I.4: the model and its settings are declared per agent definition
  and passed into each `Agent`. The shared client is handed to the model object;
  nothing installs it as a process-wide default (Article I.2).
- Article IV.2: `instructions` for Style is a *callable* resolved at request time
  from the run context. The repository name is deliberately absent from the
  produced text — grepping what is actually sent to the model finds no repo name.
"""

from functools import lru_cache

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, RunContextWrapper

from config import load_config
from finding import Finding
from review_context import ReviewContext
from tools import get_ruleset, ruleset_exists

# Baseline settings the Base Reviewer declares and clones inherit unless they
# override them (Article I.4 permits inheriting only from the agent cloned from).
BASE_TEMPERATURE = 0.2
# gemini-3.6-flash is a thinking model and its reasoning tokens are drawn from
# the same budget as its visible output. A 1024 ceiling was verified to truncate
# structured output mid-JSON (finish_reason="length" → ModelBehaviorError:
# "Invalid JSON when parsing model output"), so the ceiling has to leave room for
# thinking *plus* the findings list. Do not lower this without re-testing a run
# that actually produces several findings.
BASE_MAX_TOKENS = 4096

# Style wants determinism over variety: same diff, same ruleset, same findings.
STYLE_TEMPERATURE = 0.1
STYLE_MAX_TOKENS = 4096

# Security: precision over variety — a missed vulnerability and an invented one
# are both expensive, and this reviewer's criticals drive FR-6's handoff.
SECURITY_TEMPERATURE = 0.1
SECURITY_MAX_TOKENS = 4096

# Tests: slightly looser, since spotting an *absent* test takes more inference
# than matching a pattern in the diff.
TESTS_TEMPERATURE = 0.2
TESTS_MAX_TOKENS = 4096

SECURITY_REVIEWER_NAME = "SecurityReviewer"
TESTS_REVIEWER_NAME = "TestsReviewer"
STYLE_REVIEWER_NAME = "StyleReviewer"

# `Finding.source_reviewer` is part of the strict output schema, so the model is
# obliged to emit the field. Its value is overwritten by the orchestrator with the
# reviewer that actually produced the finding, so asking for an empty string keeps
# the model from spending tokens inventing one.
SOURCE_FIELD_NOTE = (
    "Set source_reviewer to an empty string on every finding. The system fills "
    "that field in; anything you put there is discarded."
)

BASE_INSTRUCTIONS = (
    "Placeholder. This base reviewer is never run directly — it exists only to be "
    "cloned, and every clone replaces these instructions."
)


@lru_cache(maxsize=1)
def shared_model() -> OpenAIChatCompletionsModel:
    """The one model object every reviewer shares.

    Built lazily rather than at import time so that a missing credential raises
    `ConfigError` inside the entry point's handler (one clear line, Article II.3)
    instead of as an import-time traceback.
    """
    config = load_config()
    return OpenAIChatCompletionsModel(
        model=config.model_name,
        openai_client=config.build_client(),
    )


def _context_fields(context: ReviewContext | None) -> tuple[str, str, str]:
    """`(language, strictness, ruleset_id)`, with defaults if no context was
    supplied. Shared by every instruction builder; deliberately does not expose
    `repo`, which must never reach the prompt (Article IV.2)."""
    if context is None:
        return "unspecified", "normal", ""
    return context.language, context.strictness, context.ruleset_id


def _optional_ruleset_line(ruleset_id: str) -> str:
    """Ruleset sentence for the two reviewers whose `get_ruleset` call is
    optional (plan.md §3) — Style's version is stronger and lives inline."""
    if ruleset_exists(ruleset_id):
        return (
            f"A ruleset named '{ruleset_id}' is available through the get_ruleset "
            f"tool. Calling it is optional — do so only if a repository-specific "
            f"rule would change your judgement."
        )
    return (
        "No ruleset could be resolved for this review. That is not an error for "
        "your purpose; judge against widely accepted practice, and do not invent "
        "rules or attribute them to a ruleset."
    )


def build_style_instructions(context: ReviewContext | None) -> str:
    """Assemble the Style Reviewer's prompt for one run (FR-4).

    Takes the plain `ReviewContext` so the resolved text can be printed and
    diffed between contexts without an agent or a model call.

    Reads language, strictness and ruleset id from context; never the repo name
    (Article IV.2). If the ruleset id resolves to nothing, says so neutrally
    rather than inventing a description of it (spec.md §4.4 edge case).
    """
    language, strictness, ruleset_id = _context_fields(context)

    if ruleset_exists(ruleset_id):
        ruleset_line = (
            f"The ruleset for this review is '{ruleset_id}'. Call the get_ruleset "
            f"tool to read it before reporting anything — it is the only authority "
            f"on what counts as a violation here."
        )
    else:
        ruleset_line = (
            "No ruleset could be resolved for this review. Call the get_ruleset "
            "tool anyway to confirm, then fall back to widely accepted "
            f"{language} style conventions and say so in your findings. Do not "
            "invent rules or attribute them to a ruleset."
        )

    if strictness == "strict":
        # Terser than the default branch below: shorter messages, higher bar for
        # reporting at all (FR-4's "noticeably terser" requirement).
        reporting_style = (
            "REPORTING STYLE — STRICT: be terse. One short clause per finding, "
            "under 15 words, no preamble, no restating the code, no advice beyond "
            "naming the violated rule. Report only clear, defensible violations; "
            "when in doubt, omit the finding. Do not explain your reasoning."
        )
    else:
        reporting_style = (
            "REPORTING STYLE — NORMAL: write one clear sentence per finding "
            "naming the rule that was violated and what to do instead. Borderline "
            "issues may be reported as 'minor'."
        )

    return (
        f"You are a code style reviewer. The code under review is written in "
        f"{language}.\n\n"
        f"{ruleset_line}\n\n"
        f"{reporting_style}\n\n"
        "Review only the unified diff you are given, and only for style: naming, "
        "formatting, structure, imports, docstrings, dead code. Security and test "
        "coverage belong to other reviewers — ignore them.\n\n"
        "Return a list of findings. Each finding needs the file path from the "
        "diff, the line number, a severity of 'critical', 'major' or 'minor', and "
        "a message. Style issues are rarely 'critical'. If the diff violates "
        "nothing, return an empty list — never a finding that says the code is "
        "fine.\n\n"
        f"{SOURCE_FIELD_NOTE}"
    )


def build_security_instructions(context: ReviewContext | None) -> str:
    """Assemble the Security Reviewer's prompt for one run (FR-4).

    This reviewer's `"critical"` findings are what FR-6's remediation handoff
    keys on, so the severity ladder is stated explicitly rather than left to the
    model's taste.

    Article II.4 is load-bearing here: a credential found *inside a reviewed
    diff* must not be reproduced in a finding message, because findings are shown
    to the user. The prompt says so; FR-8's output guardrail enforces it.
    """
    language, strictness, ruleset_id = _context_fields(context)

    if strictness == "strict":
        reporting_style = (
            "REPORTING STYLE — STRICT: be terse. One short clause per finding, "
            "under 15 words, naming the vulnerability class and the entry point. "
            "No remediation advice, no reasoning. Report only what you can "
            "defend from the diff; when in doubt, omit the finding."
        )
    else:
        reporting_style = (
            "REPORTING STYLE — NORMAL: one clear sentence per finding naming the "
            "vulnerability class, how it could be reached, and what to do instead."
        )

    return (
        f"You are a security reviewer. The code under review is written in "
        f"{language}.\n\n"
        f"{_optional_ruleset_line(ruleset_id)}\n\n"
        f"{reporting_style}\n\n"
        "Look for: injection of any kind (SQL, command, template, path), unsafe "
        "deserialization or dynamic evaluation, missing or broken authentication "
        "and authorization checks, unsafe defaults, disabled TLS or certificate "
        "verification, exception handling that swallows a security failure, and "
        "anything shaped like a leaked credential — API keys, tokens, passwords, "
        "private keys, connection strings with embedded secrets.\n\n"
        "NEVER reproduce a credential's value in a finding message, not even "
        "partially, and not even if it looks fake. Say where it is and what kind "
        "of secret it appears to be; the value itself must not appear in your "
        "output.\n\n"
        "Severity ladder — use it literally. 'critical': an exploitable "
        "vulnerability or a credential that appears to be real and live. 'major': "
        "a genuine weakness that needs another condition to be exploitable. "
        "'minor': hardening worth doing with no clear path to exploitation.\n\n"
        "Style and test coverage belong to other reviewers — ignore them. Review "
        "only the unified diff you are given. Return a list of findings, each with "
        "the file path from the diff, the line number, a severity and a message. "
        "If the diff introduces no security problem, return an empty list — never "
        "a finding that says the code is safe.\n\n"
        f"{SOURCE_FIELD_NOTE}"
    )


def build_tests_instructions(context: ReviewContext | None) -> str:
    """Assemble the Tests Reviewer's prompt for one run (FR-4).

    The diff is the only evidence this reviewer has, so the prompt is explicit
    that an unseen test file is not proof of a missing test — otherwise every
    production-only diff produces a false "untested" finding.
    """
    language, strictness, ruleset_id = _context_fields(context)

    if strictness == "strict":
        reporting_style = (
            "REPORTING STYLE — STRICT: be terse. One short clause per finding, "
            "under 15 words, naming the untested behavior. No reasoning, no "
            "suggested test bodies. Report only clear gaps; when in doubt, omit "
            "the finding."
        )
    else:
        reporting_style = (
            "REPORTING STYLE — NORMAL: one clear sentence per finding naming the "
            "behavior that is not covered and the kind of test that would cover it."
        )

    return (
        f"You are a test coverage reviewer. The code under review is written in "
        f"{language}.\n\n"
        f"{_optional_ruleset_line(ruleset_id)}\n\n"
        f"{reporting_style}\n\n"
        "Look for coverage the diff itself implies is missing: new functions, "
        "branches, error paths or edge cases added with no corresponding test "
        "changes visible in this diff; existing tests that were deleted, skipped, "
        "or had assertions weakened or removed; and tests changed to match new "
        "behavior without any test for the new behavior itself.\n\n"
        "You can only see this diff. Tests elsewhere in the repository are "
        "invisible to you, so never claim a test 'does not exist' — say that no "
        "test change accompanies the change, which is what you can actually "
        "observe.\n\n"
        "Severity ladder: 'critical' only for a deleted or disabled test that was "
        "guarding something dangerous. 'major' for a new code path or error branch "
        "with no accompanying test. 'minor' for a thin or narrowed assertion.\n\n"
        "Style and security belong to other reviewers — ignore them. Review only "
        "the unified diff you are given. Return a list of findings, each with the "
        "file path from the diff, the line number, a severity and a message. If "
        "the diff's test coverage is adequate, return an empty list — never a "
        "finding that says coverage is fine.\n\n"
        f"{SOURCE_FIELD_NOTE}"
    )


def _style_instructions_for_run(
    ctx: RunContextWrapper[ReviewContext], agent: Agent[ReviewContext]
) -> str:
    """Adapter matching the SDK's dynamic-instructions signature."""
    return build_style_instructions(ctx.context)


def _security_instructions_for_run(
    ctx: RunContextWrapper[ReviewContext], agent: Agent[ReviewContext]
) -> str:
    """Adapter matching the SDK's dynamic-instructions signature."""
    return build_security_instructions(ctx.context)


def _tests_instructions_for_run(
    ctx: RunContextWrapper[ReviewContext], agent: Agent[ReviewContext]
) -> str:
    """Adapter matching the SDK's dynamic-instructions signature."""
    return build_tests_instructions(ctx.context)


def build_base_reviewer() -> Agent[ReviewContext]:
    """The Base Reviewer (plan.md §2): not exposed, exists only to be cloned."""
    return Agent[ReviewContext](
        name="BaseReviewer",
        instructions=BASE_INSTRUCTIONS,
        model=shared_model(),
        model_settings=ModelSettings(
            temperature=BASE_TEMPERATURE,
            max_tokens=BASE_MAX_TOKENS,
        ),
        output_type=list[Finding],
    )


def build_style_reviewer() -> Agent[ReviewContext]:
    """Style Reviewer: a clone of Base with per-run instructions and the ruleset
    tool, whose call is FORCED (FR-9a, plan.md §9).

    `tool_choice` names `get_ruleset` specifically. "required" would only force
    *some* tool call — with more tools later, the model could satisfy it without
    ever reading the ruleset. The name is taken from the tool object rather than
    typed as a literal, so renaming the tool cannot silently unpin it.

    `reset_tool_choice=True` is load-bearing, not decoration: it drops the forced
    choice back to "auto" after the first tool call. Without it, the model would
    be forced to call `get_ruleset` on every turn, could never emit its findings,
    and every Style run would end in MaxTurnsExceeded. It is the SDK default; it is
    set explicitly so nobody "simplifies" it away.
    """
    return build_base_reviewer().clone(
        name=STYLE_REVIEWER_NAME,
        instructions=_style_instructions_for_run,
        tools=[get_ruleset],
        output_type=list[Finding],
        model_settings=ModelSettings(
            temperature=STYLE_TEMPERATURE,
            max_tokens=STYLE_MAX_TOKENS,
            tool_choice=get_ruleset.name,
        ),
        reset_tool_choice=True,
    )


def build_security_reviewer() -> Agent[ReviewContext]:
    """Security Reviewer: a clone of Base with per-run instructions.

    `get_ruleset` is available but NOT forced (plan.md §3 marks it optional here)
    — only Style's call is required, and that wiring comes with FR-9a. Agent-level
    hooks attach to this reviewer only, in FR-10.
    """
    return build_base_reviewer().clone(
        name=SECURITY_REVIEWER_NAME,
        instructions=_security_instructions_for_run,
        tools=[get_ruleset],
        output_type=list[Finding],
        model_settings=ModelSettings(
            temperature=SECURITY_TEMPERATURE,
            max_tokens=SECURITY_MAX_TOKENS,
        ),
    )


def build_tests_reviewer() -> Agent[ReviewContext]:
    """Tests Reviewer: a clone of Base with per-run instructions. `get_ruleset`
    is available but not forced (plan.md §3)."""
    return build_base_reviewer().clone(
        name=TESTS_REVIEWER_NAME,
        instructions=_tests_instructions_for_run,
        tools=[get_ruleset],
        output_type=list[Finding],
        model_settings=ModelSettings(
            temperature=TESTS_TEMPERATURE,
            max_tokens=TESTS_MAX_TOKENS,
        ),
    )
