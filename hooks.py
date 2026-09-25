"""Lifecycle hooks (FR-10).

Two kinds, deliberately recording different things (plan.md §7):

RUN-LEVEL — `ReviewerRunHooks`, one instance per reviewer run, passed to
`Runner.run(hooks=...)` by `review_runner`. It fires for every agent that runs in
that run, which for a reviewer run is just that reviewer. It records coarse,
per-run aggregates: how many model calls and tool calls happened, and — the point
of FR-10 — the run's real token usage.

AGENT-LEVEL — `SecurityAgentHooks`, attached as `hooks=` on the Security Reviewer's
agent definition only. It fires only while that agent is executing, in whatever run
it executes in, and records a fine-grained, ordered event log: start, each model
call, each tool call with the tool's name, end with the finding count.

Token counts are never estimated (Article VII.3). The SDK accumulates each model
response's reported usage into `RunContextWrapper.usage` *before* firing
`on_llm_end` (agents/run_internal/run_loop.py), and mutates that one object in
place for the whole run. The run hooks keep a reference to it from the first event,
so the numbers stay readable even when the run later raises — which is exactly the
case spec.md §4.10 says must still produce a footer row.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from agents import AgentHooks, RunContextWrapper, RunHooks, Usage


@dataclass
class ReviewerRunStats:
    """What the run-level hooks saw for one reviewer — across every run that
    reviewer made (the Style Reviewer makes two: its forced ruleset lookup, then
    its findings; FR-9a)."""

    agent_name: str | None = None
    llm_calls: int = 0
    tool_calls: int = 0
    completed: bool = False  # on_agent_end fired — a run produced its output
    started_at: float | None = None
    last_event_at: float | None = None
    # Live references to each run's own Usage object, not copies: each keeps
    # accumulating as its run proceeds and survives an exception mid-run. One per
    # run, because every Runner.run has its own context and its own Usage.
    usages: list[Usage] = field(default_factory=list)

    @property
    def tokens(self) -> int | None:
        """Real total tokens summed over this reviewer's runs, or None if no run
        got far enough for the hooks to see its context (never guessed as 0)."""
        if not self.usages:
            return None
        return sum(usage.total_tokens for usage in self.usages)

    @property
    def requests(self) -> int | None:
        if not self.usages:
            return None
        return sum(usage.requests for usage in self.usages)


class ReviewerRunHooks(RunHooks[Any]):
    """Run-level hooks for one reviewer run. Create a fresh instance per run —
    stats are per instance, so nothing leaks between reviews (Article IV.1)."""

    def __init__(self) -> None:
        self.stats = ReviewerRunStats()

    def _touch(self, context: RunContextWrapper[Any]) -> None:
        now = time.perf_counter()
        if self.stats.started_at is None:
            self.stats.started_at = now
        self.stats.last_event_at = now
        # By identity: one entry per run context, however many events it fires.
        if not any(usage is context.usage for usage in self.stats.usages):
            self.stats.usages.append(context.usage)

    async def on_agent_start(self, context, agent) -> None:
        self._touch(context)
        self.stats.agent_name = agent.name

    async def on_llm_end(self, context, agent, response) -> None:
        self._touch(context)
        self.stats.llm_calls += 1

    async def on_tool_end(self, context, agent, tool, result) -> None:
        self._touch(context)
        self.stats.tool_calls += 1

    async def on_agent_end(self, context, agent, output) -> None:
        self._touch(context)
        self.stats.completed = True


@dataclass
class AgentEvent:
    kind: str
    detail: str = ""


class SecurityAgentHooks(AgentHooks[Any]):
    """Agent-level hooks for the Security Reviewer only (spec.md §4.10's design
    decision). Records an ordered event log of what Security itself did.

    Never records finding messages, tool results or model text — only event kinds,
    tool names and counts — because a credential found in a diff must not reach a
    log (Article II.4).
    """

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def on_start(self, context, agent) -> None:
        self.events.append(AgentEvent("start", agent.name))

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        self.events.append(AgentEvent("llm_start"))

    async def on_llm_end(self, context, agent, response) -> None:
        tokens = getattr(getattr(response, "usage", None), "total_tokens", None)
        self.events.append(AgentEvent("llm_end", f"tokens={tokens}"))

    async def on_tool_start(self, context, agent, tool) -> None:
        self.events.append(AgentEvent("tool_start", tool.name))

    async def on_tool_end(self, context, agent, tool, result) -> None:
        self.events.append(AgentEvent("tool_end", tool.name))

    async def on_end(self, context, agent, output) -> None:
        count = len(output) if isinstance(output, list) else "n/a"
        self.events.append(AgentEvent("end", f"findings={count}"))
