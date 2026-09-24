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


def build_style_instructions(context: ReviewContext | None) -> str:
    """Assemble the Style Reviewer's prompt for one run (FR-4).

    Takes the plain `ReviewContext` so the resolved text can be printed and
    diffed between contexts without an agent or a model call.

    Reads language, strictness and ruleset id from context; never the repo name
    (Article IV.2). If the ruleset id resolves to nothing, says so neutrally
    rather than inventing a description of it (spec.md §4.4 edge case).
    """
    if context is None:
        language, strictness, ruleset_id = "unspecified", "normal", ""
    else:
        language, strictness, ruleset_id = (
            context.language,
            context.strictness,
            context.ruleset_id,
        )

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
        "fine."
    )


def _style_instructions_for_run(
    ctx: RunContextWrapper[ReviewContext], agent: Agent[ReviewContext]
) -> str:
    """Adapter matching the SDK's dynamic-instructions signature."""
    return build_style_instructions(ctx.context)


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
    tool. The forced `get_ruleset` call (FR-9a) is deliberately not wired yet."""
    return build_base_reviewer().clone(
        name="StyleReviewer",
        instructions=_style_instructions_for_run,
        tools=[get_ruleset],
        output_type=list[Finding],
        model_settings=ModelSettings(
            temperature=STYLE_TEMPERATURE,
            max_tokens=STYLE_MAX_TOKENS,
        ),
    )
