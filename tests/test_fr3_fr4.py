"""Offline tests for FR-3 (typed list output) and FR-4 (per-run instructions).

These replace the print-only `try_style_reviewer.py` harness, which demonstrated
the same acceptance criteria by eye, asserted nothing, and needed the live API.

FR-3 — SCHEMA vs RUNTIME. Two different claims, tested separately:
- Schema: the JSON schema the runner sends to the model for a `list[Finding]`
  reviewer wraps the list in a single-key object — `{"response": [...]}`, the
  shape seen live on 2026-09-24 — because a strict JSON schema must have an
  object at its root. Tested on the schema the runner itself derives for the
  real Style Reviewer (`get_output_schema(agent)`), not on a hand-built one.
- Runtime: `final_output` is never that wrapper — it is a plain `list[Finding]`.
  Tested by driving the REAL runner with a scripted model that emits the wrapper,
  exactly as the live model did. (The FR-6 stubbed tests are NOT evidence here:
  they stub `Runner.run` and inject `final_output` directly, so no unwrapping
  ever happens in them. `test_fr10_hooks.py` is the other place the real runner
  performs this unwrap.)

FR-4 — two contexts that differ in language and strictness resolve to visibly
different prompts, strict is terser, and the resolved prompt can be inspected
before any model call — through the same callable the SDK invokes per run.

Run with `pytest tests/test_fr3_fr4.py`, or standalone: `python tests/test_fr3_fr4.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import asyncio
import json

from agents import RunContextWrapper, Runner
from agents.run_internal.turn_preparation import get_output_schema

from finding import Finding
from review_context import ReviewContext
from reviewers import build_style_instructions, build_style_reviewer
from test_fr10_hooks import DIFF, ScriptedModel, run_config

STRICT_MARKER = "REPORTING STYLE — STRICT: be terse"
NORMAL_MARKER = "REPORTING STYLE — NORMAL"


def context(**overrides) -> ReviewContext:
    fields = dict(
        repo="code-review-desk",
        language="Python",
        ruleset_id="python-default",
        strictness="normal",
    )
    fields.update(overrides)
    return ReviewContext(**fields)


# ---------------------------------------------------------------------------
# FR-3 — the schema wraps the list; the runtime value does not
# ---------------------------------------------------------------------------


def test_fr3_schema_wraps_the_list_in_a_single_key_object() -> None:
    schema = get_output_schema(build_style_reviewer())
    assert schema is not None
    assert schema.is_strict_json_schema() is True

    root = schema.json_schema()
    assert root["type"] == "object"  # a strict schema's root must be an object...
    assert list(root["properties"]) == ["response"]  # ...with exactly one key
    assert root["required"] == ["response"]
    assert root["additionalProperties"] is False

    wrapped = root["properties"]["response"]
    assert wrapped["type"] == "array"  # the list lives INSIDE the wrapper
    assert wrapped["items"] == {"$ref": "#/$defs/Finding"}

    finding = root["$defs"]["Finding"]
    assert set(finding["properties"]) == {
        "file", "line", "severity", "message", "source_reviewer",
    }
    assert finding["properties"]["severity"]["enum"] == ["critical", "major", "minor"]
    assert finding["properties"]["message"]["minLength"] == 1


def test_fr3_runtime_final_output_is_a_plain_list_never_the_wrapper() -> None:
    # The scripted model emits the wrapper JSON, as the live model did; the REAL
    # runner parses it. What comes out is the list, not the object around it.
    result = asyncio.run(
        Runner.run(
            build_style_reviewer(),
            DIFF,
            context=context(),
            run_config=run_config(ScriptedModel()),
        )
    )
    output = result.final_output
    assert type(output) is list, type(output)
    assert not isinstance(output, dict)
    assert all(isinstance(f, Finding) for f in output)
    # The acceptance criterion's own wording: countable with a plain expression.
    assert len([f for f in output if f.severity == "critical"]) == 0
    assert len([f for f in output if f.severity == "minor"]) == 1


def test_fr3_the_wrapper_is_required_at_parse_time() -> None:
    # Schema-level, observed from the parsing side: the parser expects the
    # wrapper, rejects a bare array, and hands back the plain list.
    from agents.exceptions import ModelBehaviorError

    schema = get_output_schema(build_style_reviewer())
    finding = {
        "file": "a.py", "line": 1, "severity": "minor",
        "message": "x", "source_reviewer": "",
    }
    parsed = schema.validate_json(json.dumps({"response": [finding]}))
    assert type(parsed) is list and isinstance(parsed[0], Finding)
    try:
        schema.validate_json(json.dumps([finding]))
    except ModelBehaviorError:
        pass
    else:
        raise AssertionError("a bare array was accepted without the wrapper")


def test_fr3_invalid_severity_is_rejected_not_coerced() -> None:
    # spec.md §4.3's edge case.
    from agents.exceptions import ModelBehaviorError

    schema = get_output_schema(build_style_reviewer())
    bad = {"file": "a.py", "line": 1, "severity": "blocker", "message": "x", "source_reviewer": ""}
    try:
        schema.validate_json(json.dumps({"response": [bad]}))
    except ModelBehaviorError:
        return
    raise AssertionError("severity 'blocker' was not rejected")


# ---------------------------------------------------------------------------
# FR-4 — instructions resolved per run, from context
# ---------------------------------------------------------------------------


def test_fr4_two_contexts_resolve_to_visibly_different_prompts() -> None:
    normal = build_style_instructions(context(language="Python", strictness="normal"))
    strict = build_style_instructions(context(language="Go", strictness="strict"))

    assert normal != strict
    assert "written in Python." in normal and "written in Go." in strict
    assert "written in Go." not in normal and "written in Python." not in strict


def test_fr4_strict_carries_the_terser_style_and_normal_does_not() -> None:
    normal = build_style_instructions(context(strictness="normal"))
    strict = build_style_instructions(context(strictness="strict"))

    assert STRICT_MARKER in strict
    assert "under 15 words" in strict
    assert STRICT_MARKER not in normal
    assert "under 15 words" not in normal

    assert NORMAL_MARKER in normal
    assert NORMAL_MARKER not in strict


def test_fr4_strictness_alone_changes_the_prompt() -> None:
    # Isolates strictness: same language, same ruleset.
    assert build_style_instructions(context(strictness="normal")) != build_style_instructions(
        context(strictness="strict")
    )


def test_fr4_the_agents_own_callable_resolves_the_same_text_before_any_call() -> None:
    # The SDK calls `agent.instructions(run_context, agent)` at request time. The
    # prompt is inspectable before any model call by invoking it the same way.
    style = build_style_reviewer()
    assert callable(style.instructions)
    for ctx in (context(strictness="normal"), context(language="Go", strictness="strict")):
        resolved = style.instructions(RunContextWrapper(ctx), style)
        assert resolved == build_style_instructions(ctx)


def test_fr4_prompts_never_contain_the_repository_name() -> None:
    # Article IV.2 / spec.md §4.2: context reaches the prompt, the repo name never.
    for strictness in ("normal", "strict"):
        prompt = build_style_instructions(context(repo="billing-service", strictness=strictness))
        assert "billing-service" not in prompt


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
