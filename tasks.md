# Tasks — Code Review Desk

**Version:** 1.0.0
**Governs:** Build order. Every task names the requirement(s) it satisfies and a concrete
verification step.

Legend: 🔪 = on the cut list (see `spec.md` §7) — safe to skip under time pressure, in the stated
order, only after everything else in its phase is done.

---

## Phase 0 — Specification Gate (target: 0:00–0:35)

| ID | Task | Satisfies | Verification | Depends on |
|---|---|---|---|---|
| T001 | Initialize git repo; create `.gitignore` (`.env`, `__pycache__/`, `.venv/`, `ledger.jsonl`) | Art. II.1 | `.gitignore` tracked before any secret/runtime file exists | — |
| T002 | Write and commit `constitution.md` | Art. V | first commits, no code alongside | T001 |
| T003 | Write and commit `spec.md` | Art. V | separate commit, no code | T002 |
| T004 | Write and commit `plan.md` | Art. V | separate commit, no code | T003 |
| T005 | Write and commit `tasks.md` (this file) | Art. V | separate commit, no code | T004 |
| T006 | Create `.env.example` with placeholder keys only; commit it | NFR-1, Art. II.2 | no real secret in the file | T005 |
| T007 | **Verify the model works on this project's API key before writing any reviewer code** — try a single test call against `gemini-2.5-flash`; if blocked (per constitution.md's Article I.1 note), amend via Article IX now, before Phase 1 | Art. I.1, IX | one successful or one documented-and-amended model call | T006 |
| T008 | Verify Phase 0 gate: `git log --oneline` shows T002–T007 preceding any runtime-file commit | NFR-5 | log inspection | T002–T007 |

**Gate:** do not proceed to Phase 1 until T008 is confirmed.

## Phase 1 — Intake and One Reviewer (target: 0:35–1:05)

| ID | Task | Satisfies | Verification | Depends on |
|---|---|---|---|---|
| T101 | Add `.env` (untracked) with the real API key; startup check fails fast with a named error if missing | FR-1, NFR-1 | run with key removed → one clear error, no traceback | T006 |
| T102 | Create the model client and wire it into the Desk agent's own definition (agent-level, not global) | FR-1, Art. I | grep confirms no global default-client override | T101 |
| T103 | Implement `split_diff_by_file`: parses a unified diff into per-file chunks, before any model call | FR-1 | a two-file diff produces two chunks, demonstrated in isolation | — |
| T104 | Handle empty/malformed diff input with a plain reported message, not an exception | FR-1 | empty-string and garbage-text inputs both produce a clear message, no traceback | T103 |
| T105 | Wire an async entry point (terminal: diff path from argv) that calls `split_diff_by_file` and prints the chunk count | FR-1 | `uv run main.py path/to.diff` prints the correct chunk count | T102, T104 |
| T106 | Define `ReviewContext` dataclass with `__post_init__` validation (invalid `strictness` raises immediately) | FR-2 | constructing with `strictness="loose"` raises before any run | — |
| T107 | Implement `get_ruleset` tool: zero model-supplied parameters, reads `context.ruleset_id` only; returns "unavailable" string if missing | FR-2 | inspect generated schema — no parameters; deleting the ruleset file still returns a string, not a crash | T106 |
| T108 | Implement `list_changed_files` and `get_file_diff` tools, backed by the chunks from T103 | FR-1 (support), plan.md §3 | correct file list; unknown path returns a clear not-found string | T103 |
| T109 | Create the Base Reviewer agent; clone it once into a single test reviewer (e.g. Style) with per-run dynamic instructions built from `ReviewContext` (ruleset + language; terser when `strictness == "strict"`) | FR-4 | two different contexts produce two visibly different resolved prompts, printed before the model call | T106, T107 |
| T110 | Set this reviewer's `output_type` to `list[Finding]`; run it once end-to-end over a real diff chunk | FR-3 | `final_output` is a plain Python list; criticals countable with a list comprehension; generated schema shows the single-key wrapper | T109 |

**Phase 1 exit check:** FR-1 through FR-4 demonstrable with one working reviewer end-to-end.

## Phase 2 — Fan Out (target: 1:05–1:40)

| ID | Task | Satisfies | Verification | Depends on |
|---|---|---|---|---|
| T201 | Clone Base Reviewer into Security and Tests reviewers (the third, Style, already exists from T109); differentiate instructions/temperature per `plan.md` §2 | FR-5 | all three share Base's model config, differing only in stated overrides | T110 |
| T202 | Launch all three reviewers via a single `asyncio.gather(..., return_exceptions=True)` call over the same diff | FR-5, Art. V.1 | code inspection: one gather call, not three sequential awaits | T201 |
| T203 | Record wall-clock time for the gather group and for each individual reviewer run separately | FR-5 | two numbers shown side by side: group time ≈ slowest reviewer; sum of three is clearly larger | T202 |
| T204 | Confirm one reviewer's induced failure (e.g. a forced exception) does not cancel or block the other two | FR-5 | the other two still return valid `list[Finding]` results when one is made to fail | T202 |
| T205 | Configure the Style Reviewer's `get_ruleset` tool call as **required** via forced tool choice | FR-9a | a run without the ruleset tool being called is impossible — verify by inspecting the model-facing tool config, and by observing every Style run includes the call | T107, T201 |
| T206 | Confirm `get_ruleset`, `list_changed_files`, `get_file_diff` all hand failures to their own internal error handling, never raising into the runner | FR-9b, Art. III | malformed diff input and missing ruleset both produce clean strings, not exceptions, at the tool level | T107, T108 |
| T207 | Set `max_turns=6` (plan.md §10) on every reviewer's run; catch the ceiling exception at the run boundary, reporting that reviewer's contribution as partial | FR-9c | forcing more than 6 turns on one reviewer produces a partial result for that reviewer only, others unaffected | T202 |
| T208 | Implement the Merge Specialist agent; expose it to the Desk via `.as_tool(...)`; dedupes and orders findings by severity | FR-6 | overlapping findings from two reviewers collapse to one; result ordered critical → major → minor | T201 |
| T209 | Implement the Remediation Specialist agent; wire it as a handoff target on the Desk, triggered only when a critical security finding exists in the merged findings | FR-6 | a diff with a critical security finding triggers the handoff; a clean diff does not | T208 |
| T210 | Write the two-sentence tool-vs-handoff justification into `spec.md` §4.6 (already present) and confirm it matches the actual implementation | FR-6 | justification reads true against the code as built | T208, T209 |
| T211 🔪 | Implement the run-level model override for a "cheaper second opinion" — same reviewer agent object, a different model passed at the run/`RunConfig` level | FR-7 | same agent's `model=` attribute is provably unchanged between the two calls; two distinct reviews are produced | T201 |
| T212 | Implement the output guardrail: inspects the Desk's finished report (post-merge, post-remediation) for credential-shaped text; raises a tripwire on match | FR-8 | a diff with a fake API-key-shaped string produces a refusal; a clean diff passes through unaffected | T208, T209 |
| T213 | Catch the guardrail's tripwire at the single top-level entry point; show a refusal instead of the report; confirm the discarded report is never partially shown | FR-8, Art. VIII.2 | refusal message shown; grep confirms the exact catch site | T212 |

**Phase 2 exit check:** FR-5 through FR-9 demonstrable, including both wall-clock numbers and one refused (leaky) report.

## Phase 3 — Observe and Ship (target: 1:40–1:55)

| ID | Task | Satisfies | Verification | Depends on |
|---|---|---|---|---|
| T301 | Implement run-level hooks recording elapsed ms and token usage per reviewer run | FR-10 | three footer rows produced, values read from run-context usage data, not estimated | T202 |
| T302 | Add the footer to the final `Report` (per `plan.md` §4.3) | FR-10 | footer visibly present with three rows in a real run's output | T301 |
| T303 🔪 | Attach agent-level hooks to the Security Reviewer only | FR-10 | events appear only during Security's run, none during Tests/Style | T201, T301 |
| T304 🔪 | Implement the custom runner appending one `ledger.jsonl` line per reviewer run (plan.md §4.4 shape); register once at startup | FR-11 | a three-file diff review produces the expected number of ledger lines; removing the registration line disables it entirely | T202 |
| T305 | Set up the Chainlit page: paste a diff, session-scoped `ReviewContext` and last `Report`, stream findings as each reviewer completes | FR-12 | findings appear progressively, not all at once; a second diff in the same session reuses context; two sessions never share state | T208, T209 |
| T306 | Enable tracing under the developer's own key; group all three reviewer runs, Merge, and any Remediation handoff under one shared identifier generated before the `gather` call | FR-13 | one review opens as a single trace | T202, T208, T209 |
| T307 | Open a real trace; confirm the three reviewer spans visibly overlap in time; identify the slowest one | FR-13 | screenshot/observation showing overlapping spans, one named as slowest | T306 |

**Phase 3 exit check:** FR-10 through FR-13 demonstrable; cut list applied here first if behind schedule, in order T304 → T211 → T303.

## Demo Prep (target: 1:55–2:00)

| ID | Task | Satisfies | Verification |
|---|---|---|---|
| T401 | Run one real diff through the full pipeline live (ideally one with a planted critical security issue, to show the Remediation handoff) | Overall demo | recorded/screenshotted |
| T402 | Open the trace for that exact review; show the overlapping reviewer spans and name the slowest | FR-13 | trace matches the demoed review |
| T403 | Show the footer (latency + token counts) and the ledger lines for that same review | FR-10, FR-11 | values match across footer and ledger |
| T404 | Re-read every diff produced by the coding agent at least once before the viva | Constitution Art. II ("you own it") | self-check — be ready to explain any line |

## Pre-Viva Self-Check (map to the eight defense questions)

- [ ] Can show both wall-clock numbers (concurrent vs. sum) and explain exactly what made the difference (T203).
- [ ] Can explain the list-wrapping schema quirk and why `final_output` is still a plain list (T110).
- [ ] Can state which reviewer attributes are shared with Base vs. their own (T201).
- [ ] Can explain why Merge is a tool and Remediation is a handoff, and what would break if swapped (T208–T210).
- [ ] Can point to exactly where the output guardrail fires and what had already been paid for by that point (T212–T213).
- [ ] Can trace the footer's token numbers back to real run-context usage data, not an estimate (T301–T302).
- [ ] Can explain any ledger lines beyond the expected count — e.g. from a cut-list re-enable, a retried run, or the FR-7 override (T304, T211).
- [ ] Can name which control (ceiling, required tool, error handling) would stop a reviewer stuck calling a tool in a loop (T207).
