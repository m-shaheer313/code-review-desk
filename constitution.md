# Constitution — Code Review Desk

**Version:** 1.1.0
**Status:** Ratified
**Applies to:** All source code, configuration, and documentation produced for this project.

## Preamble

This document defines the non-negotiable rules governing the design and implementation of the
Code Review Desk. Every requirement in `spec.md`, every architectural decision in `plan.md`, and
every task in `tasks.md` MUST comply with the articles below. Where a conflict exists between this
document and any other project artifact, this document takes precedence, and the conflicting
artifact MUST be corrected before work continues.

The keywords **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are to be interpreted
as in RFC 2119.

---

## Article I — Model Provider and Configuration

1. The Desk and its reviewers MUST use `gemini-3.6-flash` as the default underlying language
   model, accessed through an OpenAI-compatible client, configured at the **agent level** — passed
   into each `Agent` definition's own model/client configuration.

   **Note (carried forward from the Saylani Ops Desk project):** `gemini-2.5-flash` was blocked by
   Google for new API keys/projects during that prior project, ahead of its stated retirement (not
   before Oct 16, 2026), and `gemini-3.6-flash` was the confirmed working replacement at that time.
   **Note:** `gemini-2.5-flash` was tested against this project's API key at Phase 0 and returned
   an error, consistent with the block observed in the Saylani Ops Desk project. The Article was
   amended to `gemini-3.6-flash` accordingly (see commits `dd613ca`, `b486029`).
2. `set_default_openai_client`, or any equivalent global/process-wide client override, MUST NOT
   appear anywhere in the codebase.
3. FR-7 explicitly permits overriding the model **at the run level** for a single re-run (a
   "cheaper second opinion") — this is the one sanctioned exception to "configured at the agent
   level," and it MUST be done via the run's configuration, never by mutating an agent's own
   `model=` attribute.
4. Every agent definition MUST declare its own model settings (temperature, max tokens, etc.)
   explicitly. An agent MAY inherit settings only from the specific base agent it was cloned from.

## Article II — Secrets Management

1. All credentials (API keys, tracing export key) MUST live exclusively in a `.env` file at the
   project root, listed in `.gitignore` before it is ever populated with a real value.
2. An `.env.example` file with placeholder values only MUST be committed.
3. If a required environment variable is missing or empty at startup, the program MUST fail fast
   with one human-readable error naming the missing variable — never a raw stack trace.
4. **This project handles user-supplied diffs, which may themselves contain real or fake secrets.**
   No secret — whether the developer's own credential or a credential found *inside a reviewed
   diff* — MUST ever be logged, printed, embedded in a trace span, written to `ledger.jsonl`, or
   included in a report shown to the user. This is the specific concern FR-8's output guardrail
   exists to enforce, and it is a constitutional requirement independent of that guardrail's
   correctness — a defect in the guardrail does not excuse a secret reaching the user.

## Article III — Tool Safety

1. No tool function exposed to an agent MUST allow an unhandled exception to propagate out of the
   tool and into the agent runner.
2. Every tool MUST catch every exception it can foreseeably raise (missing file, malformed diff,
   missing ruleset, unreadable path) internally and return a short, model-actionable string or
   structured value instead of raising.
3. A tool that cannot complete its purpose (e.g. the ruleset file is missing) MUST say so plainly
   rather than returning an empty value or fabricating a result.

## Article IV — Review Reproducibility and Context Isolation

1. A review MUST be reproducible from the diff alone: nothing outside the diff and the declared
   `ReviewContext` (repository, language, ruleset id, strictness) may silently influence a
   reviewer's findings. No reviewer MAY read from or depend on state left over from a previous
   review in the same process.
2. `ReviewContext` MUST reach tools and dynamic instructions only through the SDK's local run
   context mechanism, never by being interpolated into a static prompt template. Grepping the
   source for a real repository name MUST find it only where the context is constructed.
3. A tool that reads `ReviewContext` MUST take the run context as its parameter, not as a
   model-supplied argument; its generated schema MUST NOT contain a wrapper field for repository,
   language, or ruleset.

## Article V — Concurrency Discipline

1. The three reviewers (Security, Tests, Style) MUST be launched together and awaited as a group
   (e.g. `asyncio.gather`), never started and awaited one after another. A sequential
   implementation that produces correct findings does not satisfy this project — concurrency is
   graded, not incidental.
2. Every concurrent run MUST still be individually protected by Article VI's turn ceiling; running
   reviewers concurrently MUST NOT be used as a reason to relax per-reviewer bounds.

## Article VI — Cost and Ceiling Discipline

1. Every agent MUST declare explicit model settings; none may generate under an unbounded
   configuration.
2. Every review run MUST be protected by a maximum-turn ceiling. When exceeded, the SDK's
   max-turns exception MUST be caught at the run boundary and reported as a **partial review**
   (per spec.md's failure semantics), never as an unhandled crash.
3. A reviewer configured to require a tool call (FR-9) MUST still be bounded by the same ceiling as
   any other reviewer — a required tool call is not an exemption from the turn limit.

## Article VII — Observability and Auditability

1. Tracing MUST be enabled for every review, exported under the developer's own tracing key.
2. One full review — all three reviewers, the merge, and any remediation handoff — MUST appear as
   a single trace, with the three reviewers' spans visibly overlapping in time (proving concurrency
   from the trace itself), not stacked end to end.
3. Every run MUST append exactly one line to `ledger.jsonl` (NFR-3), with real, run-context-derived
   token counts — never estimated or hardcoded values.

## Article VIII — Failure Philosophy

1. A tool raising an exception into the runner is a **defect**, not an acceptable failure mode.
2. Any exception that is an expected part of normal operation — a missing ruleset, an output
   guardrail tripwire, a turn-ceiling exceeded — MUST be caught at a well-defined boundary and
   turned into a specific, polite message or a partial-review report. The user MUST NOT ever see a
   Python traceback.
3. A review that cannot fully complete (ceiling exceeded, a reviewer failed) MUST still produce the
   best partial report available from what did complete, clearly marked as partial, rather than
   terminating with no output.

## Article IX — Amendments

Any change to this document MUST be its own commit with a message beginning `constitution:`, and
MUST be reflected the same day in any part of `spec.md`, `plan.md`, or `tasks.md` it affects.
