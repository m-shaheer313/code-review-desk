"""Agent-facing tools (FR-2 slice: `get_ruleset` only).

Article III is the governing rule here: no tool may let an exception reach the
agent runner. Every foreseeable failure — unknown ruleset id, missing file,
unreadable file, absent run context — is caught and returned as a short,
model-actionable string that says plainly that the ruleset is unavailable, rather
than an empty value the model might mistake for "no rules apply".

Article IV.3: `get_ruleset` takes the run context as its parameter and nothing
else. `ruleset_id` is never a model-supplied argument, so the generated schema has
zero properties.
"""

from pathlib import Path

from agents import RunContextWrapper, function_tool

from review_context import ReviewContext

RULESETS_DIR = Path(__file__).parent / "rulesets"
RULESET_SUFFIXES = (".txt", ".json", ".md")

UNAVAILABLE_PREFIX = "ruleset unavailable"


def available_ruleset_ids() -> list[str]:
    """Ruleset ids backed by a real file. A file's stem *is* its id, so dropping a
    new file into `rulesets/` registers it — no table to keep in sync."""
    try:
        return sorted(
            path.stem
            for path in RULESETS_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in RULESET_SUFFIXES
        )
    except OSError:
        return []


def _resolve_ruleset_path(ruleset_id: str) -> Path | None:
    """Map an id to its file, or None if the id is unknown.

    The id is matched against the known stems rather than pasted into a path, so a
    traversal-shaped id (`../.env`) can never reach the filesystem — it is simply
    not in the known set (Article II.4).
    """
    if not ruleset_id or ruleset_id not in available_ruleset_ids():
        return None
    for suffix in RULESET_SUFFIXES:
        candidate = RULESETS_DIR / f"{ruleset_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def load_ruleset_text(ruleset_id: str) -> str:
    """Return the ruleset's text, or an `unavailable` sentence. Never raises.

    Kept separate from the tool wrapper so the loading behavior is testable
    without an agent run.
    """
    path = _resolve_ruleset_path(ruleset_id)
    if path is None:
        known = ", ".join(available_ruleset_ids()) or "none found"
        return (
            f"{UNAVAILABLE_PREFIX}: no ruleset is registered under the id "
            f"{ruleset_id!r}. Known ids: {known}. Review against general "
            f"best practice and say in your findings that the ruleset was missing."
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "strerror", None) or exc.__class__.__name__
        return (
            f"{UNAVAILABLE_PREFIX}: the ruleset file for {ruleset_id!r} could not "
            f"be read ({reason}). Review against general best practice and say in "
            f"your findings that the ruleset was unreadable."
        )
    if not text.strip():
        return (
            f"{UNAVAILABLE_PREFIX}: the ruleset file for {ruleset_id!r} is empty. "
            f"Review against general best practice and say in your findings that "
            f"the ruleset was empty."
        )
    return text


@function_tool
def get_ruleset(ctx: RunContextWrapper[ReviewContext]) -> str:
    """Return the full text of the ruleset this review is being judged against.

    Call this before reporting style findings — the ruleset is the only authority
    on what counts as a violation in this repository.
    """
    review_context = ctx.context
    if review_context is None:
        return (
            f"{UNAVAILABLE_PREFIX}: no review context was supplied for this run, "
            f"so there is no ruleset id to look up."
        )
    return load_ruleset_text(review_context.ruleset_id)
