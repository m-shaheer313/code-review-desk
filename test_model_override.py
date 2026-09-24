"""Structural test for FR-7's run-level model override. No live model call.

`Runner.run` is stubbed to capture the RunConfig it receives, so the test fails
both if the agent were mutated AND if the override were silently dropped.

Run with `python test_model_override.py`.
"""

import testing_env  # noqa: F401 — must stay the first import (no real trace export)

import ast
import asyncio
import inspect

import review_runner
from config import CHEAP_MODEL_NAME, MODEL_NAME
from review_context import ReviewContext
from review_runner import run_reviewer_with_override
from reviewers import build_style_reviewer

DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2"

captured: list[dict] = []


class FakeResult:
    def __init__(self, final_output):
        self.final_output = final_output


def install_stub():
    captured.clear()

    async def fake_run(agent=None, input=None, *, starting_agent=None, **kwargs):
        resolved = starting_agent if agent is None else agent
        captured.append(
            {
                "agent": resolved,
                "agent_model": resolved.model,
                "run_config": kwargs.get("run_config"),
                "max_turns": kwargs.get("max_turns"),
            }
        )
        return FakeResult([])

    review_runner.Runner.run = fake_run


def context() -> ReviewContext:
    return ReviewContext(
        repo="code-review-desk", language="Python", ruleset_id="python-default"
    )


def test_agent_model_object_is_not_touched_by_the_override() -> None:
    install_stub()
    agent = build_style_reviewer()
    model_before = agent.model

    asyncio.run(run_reviewer_with_override(agent, DIFF, context(), CHEAP_MODEL_NAME))

    # Identity, not equality: the same object, not merely an equal one.
    assert agent.model is model_before
    # And the agent the run actually received still carried its own model.
    assert captured[0]["agent"] is agent
    assert captured[0]["agent_model"] is model_before
    assert agent.model.model == MODEL_NAME


def test_run_config_carries_the_override_model() -> None:
    install_stub()
    agent = build_style_reviewer()

    asyncio.run(run_reviewer_with_override(agent, DIFF, context(), CHEAP_MODEL_NAME))

    run_config = captured[0]["run_config"]
    assert run_config is not None, "no RunConfig reached Runner.run"
    assert run_config.model is not None, "override was silently dropped"
    # A Model object, not a bare string — a string would be resolved through
    # RunConfig.model_provider, which defaults to OpenAI's provider.
    assert not isinstance(run_config.model, str), (
        "override passed as a string would be routed to the wrong provider"
    )
    assert run_config.model.model == CHEAP_MODEL_NAME
    # The override is a different object from the agent's own model.
    assert run_config.model is not agent.model
    assert agent.model.model != run_config.model.model


def test_same_agent_runs_under_its_own_model_then_the_override() -> None:
    # spec.md §4.7's acceptance criterion: one agent object, two reviews, no
    # reassignment of `model=` in between.
    install_stub()
    agent = build_style_reviewer()
    model_before = agent.model

    async def both():
        await review_runner.Runner.run(
            agent,
            DIFF,
            context=context(),
            max_turns=6,
            run_config=review_runner.RunConfig(tracing_disabled=True),
        )
        await run_reviewer_with_override(agent, DIFF, context(), CHEAP_MODEL_NAME)

    asyncio.run(both())

    assert len(captured) == 2
    assert captured[0]["run_config"].model is None  # ran under the agent's own model
    assert captured[1]["run_config"].model.model == CHEAP_MODEL_NAME
    assert agent.model is model_before


def test_override_preserves_other_run_config_settings() -> None:
    install_stub()
    agent = build_style_reviewer()
    original = review_runner.RunConfig(
        tracing_disabled=False, workflow_name="review rev_8f21", group_id="rev_8f21"
    )

    asyncio.run(
        run_reviewer_with_override(
            agent, DIFF, context(), CHEAP_MODEL_NAME, run_config=original
        )
    )

    passed = captured[0]["run_config"]
    # FR-13's trace grouping must survive the override.
    assert passed.group_id == "rev_8f21"
    assert passed.workflow_name == "review rev_8f21"
    assert passed.tracing_disabled is False
    assert passed.model.model == CHEAP_MODEL_NAME
    # The caller's own RunConfig was not mutated.
    assert original.model is None


def test_turn_ceiling_still_applies_under_the_override() -> None:
    # Article VI.3 / plan.md §10: a cheaper model is not an excuse for a looser
    # ceiling.
    install_stub()
    asyncio.run(
        run_reviewer_with_override(
            build_style_reviewer(), DIFF, context(), CHEAP_MODEL_NAME
        )
    )
    assert captured[0]["max_turns"] == review_runner.REVIEWER_MAX_TURNS


def test_source_never_reads_or_writes_agent_model() -> None:
    """Static check: no `.model` access on the agent anywhere in the function."""
    source = inspect.getsource(run_reviewer_with_override)
    tree = ast.parse(source.strip())

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "model":
            owner = ast.unparse(node.value)
            assert owner != "agent", f"function touches agent.model: {ast.unparse(node)}"
        # No assignment to any `.model` attribute at all.
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    assert target.attr != "model", (
                        f"function assigns a model attribute: {ast.unparse(node)}"
                    )


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
