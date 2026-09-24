# Specification — Code Review Desk

**Version:** 1.0.0
**Governs:** Behavior only. No implementation detail, library name, or code shape is normative
here — see `plan.md` for architecture and `tasks.md` for build order. Any behavior described here
that conflicts with `constitution.md` is void; the constitution wins.

RFC 2119 keywords (MUST / MUST NOT / SHOULD / MAY) apply throughout.

---

## 1. Purpose

The Code Review Desk takes a unified diff and reviews it. Three specialist reviewers — security,
tests, and style — examine the diff at the same time, each with its own tuning. Their findings are
merged into one structured report. A critical security finding hands the conversation to a
remediation specialist, who proposes a fix. Nothing the Desk produces ever repeats a secret it
found while reading the diff.

## 2. Scope

### 2.1 In scope

- Accepting a unified diff (from a file path in the terminal, or pasted text in the browser) and
  splitting it into per-file chunks before any model sees it.
- Running three reviewers — security, tests, style — concurrently over the diff.
- Merging their findings into one ordered, deduplicated report.
- Escalating to a remediation specialist when a critical security finding exists.
- Refusing to output anything that looks like a leaked credential, wherever it came from.
- Full auditability: a ledger of every run, and one trace per review showing real concurrency.

### 2.2 Out of scope (explicit non-goals)

1. **NG-1 — No repository integration.** The Desk does not fetch diffs from GitHub, GitLab, or any
   other hosted repository API. It only reads a diff supplied directly — a file path in the
   terminal, or pasted text in the browser.
2. **NG-2 — Proposals only, no automatic application.** The remediation specialist proposes a
   patch; the Desk never applies, commits, or pushes it. Applying a suggested fix is a human
   decision made entirely outside this system.
3. **NG-3 — No cross-session history.** The Desk does not answer "what changed since the last
   review of this repository?" or otherwise persist reports across sessions for later comparison.
   Each review is self-contained; the ledger (FR-11) is an audit record, not a queryable history
   feature.

## 3. Actors

| Actor | Description |
|---|---|
| **Developer (user)** | Supplies the diff and reads the report. |
| **Desk** | Entry point: splits the diff, launches the three reviewers, merges results, decides on remediation. |
| **Security Reviewer** | Looks for vulnerabilities, unsafe patterns, leaked-looking secrets in the diff. |
| **Tests Reviewer** | Looks for missing or weakened test coverage implied by the diff. |
| **Style Reviewer** | Looks for ruleset violations — naming, formatting, structural conventions. |
| **Merge Specialist** | Deduplicates and orders findings from all three reviewers. Never speaks to the user directly. |
| **Remediation Specialist** | Reached only on a critical security finding; proposes a fix directly to the user. |
| **Coding Agent (build time only)** | Writes the implementation from this spec; not present at runtime. |

## 4. Functional Requirements

Each requirement is stated as: behavior, preconditions, edge cases, error handling, and acceptance
criteria. A requirement is not "done" until every edge case listed under it is demonstrable.

### 4.1 FR-1 — A diff goes in, split by file

**Behavior.** The Desk reads a unified diff (from a command-line path in the terminal entry point,
or pasted text in the browser entry point) and splits it into per-file chunks **before any model
ever sees the diff**. The application is asynchronous from its entry point.

**Edge cases**
- An empty diff (zero bytes, or no recognizable diff hunks) → reported to the user as a plain
  message ("no changes found to review"), never as an exception or traceback.
- A malformed diff (not valid unified-diff syntax) → reported plainly, naming what looked wrong,
  rather than passed through to a reviewer to guess at.
- A diff touching a single file → produces exactly one chunk; the pipeline MUST NOT require more
  than one file to function.
- A diff touching binary files or renames with no textual hunk → that file's chunk MUST be handled
  without crashing; it MAY be reported as "no reviewable text changes" for that file.

**Acceptance criteria**
- A two-file diff produces exactly two chunks, demonstrable by inspection before any reviewer runs.
- No global default client override exists anywhere in the code.
- The entry point is declared asynchronous and launched through the async runner.

### 4.2 FR-2 — Repository rules live in context

**Behavior.** A `ReviewContext` (repository name, language, ruleset id, strictness — `"normal"` or
`"strict"`, defaulting to `"normal"`) is supplied once per review and is available to every tool
and instruction-building step as contextual data — never as literal text baked into a prompt.

**Edge cases**
- `strictness` is anything other than the two allowed values → rejected at the moment the context
  is constructed, with a clear validation error, before any review begins.
- `ruleset_id` refers to a ruleset that cannot be found → any tool reading it MUST report this
  plainly rather than silently reviewing against no rules or fabricating rules.

**Acceptance criteria**
- Any tool that reads `ruleset_id` does so through the run context, not as a model-supplied
  argument — its generated schema has no such parameter.
- Grepping the prompts (the actual text sent to the model) finds no repository name anywhere.

### 4.3 FR-3 — Findings come back as a list of typed objects

**Behavior.** A reviewer's output is a list of typed findings, never prose. Each finding has: file,
line, severity (`"critical"`, `"major"`, or `"minor"`), and a message.

**Edge cases**
- A reviewer finds nothing → it returns an empty list, not an omitted output or a prose sentence
  saying "no issues found."
- A finding's severity value is outside the three allowed literals → this MUST fail validation and
  surface as a structured-output parsing error, never be silently coerced.
- A finding's line number does not correspond to any line actually present in the diff → out of
  scope for the base requirement (see "If you finish early" territory in `tasks.md`); the base
  system MAY trust the model's line number as given.

**Acceptance criteria**
- `final_output` can be iterated directly as a Python list; criticals can be counted with a plain
  Python expression (e.g. a generator/list comprehension over `finding.severity == "critical"`).
- The generated schema for this output can be inspected and shown to wrap the list in a
  single-key object (a strict-schema requirement), while `final_output` itself is a plain list —
  this distinction can be pointed at and explained.

### 4.4 FR-4 — Reviewer instructions are built per run

**Behavior.** Each reviewer's system prompt is assembled at request time from the ruleset and the
language in context, and becomes noticeably terser when `strictness == "strict"`.

**Edge cases**
- The ruleset cannot be resolved (missing file, unknown id) → the prompt is still produced, with a
  neutral fallback phrase instead of a fabricated ruleset description.

**Acceptance criteria**
- Two different contexts (differing in ruleset, language, or strictness) produce two visibly
  different resolved prompt texts.
- The fully resolved prompt can be inspected/printed before any model call is made for that run.

### 4.5 FR-5 — Three reviewers, cloned, running concurrently

**Behavior.** Security, Tests, and Style reviewers are clones of one base reviewer, differing only
in instructions and model settings. They are launched together and awaited as a group over the
same diff — never one after another.

**Edge cases**
- One reviewer fails (raises, times out, or hits its turn ceiling) while the others succeed → the
  failure MUST NOT block or crash the other two; the merge step (FR-6) proceeds with whatever
  succeeded, and the failure is noted in the final report rather than silently dropped.
- All three reviewers succeed but one takes far longer than the others → the overall wall-clock
  time for the group MUST be close to the slowest individual reviewer's time, not the sum of all
  three — this is the defining, testable property of this requirement.

**Acceptance criteria**
- The three reviews are launched together and awaited as a group (e.g. a single concurrent-gather
  operation), demonstrable in code.
- Two wall-clock numbers can be shown side by side: the concurrent group's total time, and the sum
  of the three reviewers' individual times — the former is close to the slowest single reviewer,
  the latter is clearly larger. A sequential implementation that merely produces correct findings
  does **not** satisfy this requirement.

### 4.6 FR-6 — Merge as a tool, remediation by handoff

**Behavior.** Two specialists, deliberately wired two different ways:
- A **Merge Specialist**, exposed to the Desk as a callable tool (not a handoff target),
  deduplicates overlapping findings across the three reviewers and orders them by severity. The
  Desk keeps the conversation and presents the merged report in its own voice.
- A **Remediation Specialist**, reached by handoff, takes over only when the merged findings
  contain at least one `"critical"` severity finding from the Security Reviewer. It proposes a fix
  directly to the user (per NG-2, a proposal only — never applied automatically).

**Design rationale (required in spec.md by the brief itself):** Merging is a tool call because its
output is consumed by the Desk and re-presented in the Desk's own voice — the Desk, not Merge,
remains the entity "speaking" to the user. Remediation is a handoff because, once a critical
security issue is found, the remediation specialist's own judgment and voice should reach the user
directly — the Desk is not qualified to speak for the fix it did not design.

**Edge cases**
- No critical security finding exists → the Remediation Specialist is never invoked; the Desk
  presents the merged report as the final result.
- More than one critical security finding exists → the handoff to Remediation still occurs exactly
  once per review; Remediation MAY address multiple critical findings within its own single
  response.

**Acceptance criteria**
- A diff with at least one critical security finding triggers the Remediation handoff; a diff with
  none does not.
- The two-sentence justification above (tool vs. handoff) can be stated from `spec.md` directly.

### 4.7 FR-7 — A cheaper second opinion, configured at the run level *(cut-list priority 2 — see §7)*

**Behavior.** The Desk can re-run a review using a cheaper model, without touching any agent
definition — the override happens at the level of the individual run, not the agent.

**Edge cases**
- The override is requested for a reviewer whose original run already completed → this MUST
  produce an entirely independent second review, not a mutation of the first result.

**Acceptance criteria**
- The exact same reviewer agent object produces one review under its own configured model and one
  review under the run-level override — demonstrable by showing that `model=` was never reassigned
  on the agent between the two calls.

### 4.8 FR-8 — Nothing leaks: an output guardrail

**Behavior.** Before the final report reaches the user, an output guardrail inspects it for
anything shaped like a credential — an API key, an access token, or a password — that may have
been copied out of the diff into a finding's message. If found, the guardrail refuses the report
outright; the program catches this and reports the refusal instead of the report.

**Edge cases**
- A clean diff (no credential-shaped text anywhere) MUST pass through untouched — the guardrail
  MUST NOT produce false positives on ordinary code (e.g. a variable literally named `password`
  with no real value is not, by itself, grounds for refusal — the concern is a value that looks
  like a real secret, not the presence of the word).
- The guardrail itself fails to run (its own internal error) → this MUST be caught and treated as
  "could not confirm the report is safe"; the program MUST fail toward refusing to show the report
  rather than showing an unchecked one.

**Acceptance criteria**
- A diff containing a fake but credential-shaped string (e.g. a plausible-looking API key pattern)
  produces a refusal, not a report.
- A clean diff produces a normal report, unaffected by the guardrail's presence.
- The exact point in the code where the guardrail's tripwire is caught can be shown directly.

### 4.9 FR-9 — Required tools, failing tools, and a ceiling

Three independent controls, all present simultaneously:

**9a — Required tool call.** The Style Reviewer, whose findings are meaningless without knowing the
active ruleset, is configured so the model has **no choice** but to call the ruleset-lookup tool at
least once per review — this is a forced tool call, not an optional one made available.

**9b — Dedicated error handling for diff-reading tools.** Every tool that reads the diff or the
ruleset hands its own failures to a dedicated error-handling path rather than raising into the
runner — consistent with Article III, but called out here because this project's tools handle
externally-supplied, potentially malformed input (the diff itself) as their primary job.

**9c — Ceiling.** Every reviewer's run is protected by a maximum-turn ceiling. Exceeding it raises
an exception, caught at the run boundary, and reported as a **partial review** — the report is
still produced from whatever findings did complete, clearly marked incomplete, never a crash.

**Edge cases**
- The ruleset file is deleted or unreadable → the Style Reviewer still completes, producing a
  review that says plainly it could not consult the ruleset (a sentence the model can act on, per
  NFR-4), rather than the whole run failing.
- A reviewer's ceiling is reached while others are still running (concurrent context) → only that
  reviewer's contribution is marked partial; the other two are unaffected.

**Acceptance criteria**
- Deleting the ruleset file still produces a completed review with a sensible message about the
  missing ruleset, not a crash.
- The chosen ceiling value is a specific, named number with a stated rationale (see `plan.md`).

### 4.10 FR-10 — Latency and tokens per reviewer

**Behavior.** Run-level hooks record, for each of the three reviewers, how long it took and how
many tokens it used. The final report carries a footer with these numbers, read from the run
context — never estimated. Separately, one specific reviewer has finer-grained, agent-level
lifecycle observability attached to it alone.

**Design decision — which reviewer.** The Security Reviewer has the agent-level hooks, for the same
reason it is the one whose findings trigger a handoff: its behavior carries the highest cost if
something about its process goes unexamined.

**Edge cases**
- A reviewer fails entirely (FR-5's failure case) → its footer row MUST still appear, showing
  whatever partial timing/token data exists, marked as incomplete, rather than being silently
  omitted from the footer.

**Acceptance criteria**
- The footer shows three rows (one per reviewer) with token counts read from the run context, not
  computed by estimation or guesswork.
- The distinction between what the agent-level hooks see (fine-grained events for one reviewer
  only) and what the run-level hooks see (every reviewer, coarser events) can be explained clearly.

### 4.11 FR-11 — Every run lands in a ledger *(cut-list priority 1 — see §7)*

**Behavior.** A custom runner appends exactly one line to `ledger.jsonl` per run (per reviewer
invocation, not per whole review), registered once at startup. No agent definition mentions it.

**Edge cases**
- A three-file diff review, with three reviewers each making one run, produces exactly three ledger
  lines for that review — one per reviewer run, not one per file and not one per whole review.

**Acceptance criteria**
- One review of a three-file diff produces the expected number of ledger lines (one per reviewer
  run).
- Removing the runner's registration (a single line at startup) is the only change needed to switch
  the ledger off — no agent file requires modification.

### 4.12 FR-12 — Findings stream into the interface

**Behavior.** A browser page accepts a pasted diff and shows findings as they arrive, progressively,
rather than only after the entire review finishes. Session state holds the current `ReviewContext`
and the most recent report.

**Edge cases**
- A second diff pasted into the same session reuses the existing `ReviewContext` (repository,
  language, ruleset, strictness) rather than requiring it to be re-entered, unless the user
  explicitly changes it.
- Two separate browser sessions MUST NOT share context or report state — no module-level or global
  container may hold either.

**Acceptance criteria**
- Findings visibly appear during a review, not all at once at the end.
- A second diff in the same session reuses the existing context.
- The message handler awaits its run rather than calling a synchronous variant.

### 4.13 FR-13 — One review, one trace

**Behavior.** Tracing is enabled for every review, exported under the developer's own key. A whole
review — all three reviewers, the merge, and any remediation handoff — appears as a single trace.

**Edge cases**
- The three reviewers' spans MUST be visibly overlapping in time within the trace (proving
  concurrency), not stacked sequentially one after another — a trace showing them stacked would
  indicate FR-5's concurrency requirement was not actually met, regardless of what the code claims.

**Acceptance criteria**
- A trace can be opened, and the three reviewer spans can be shown overlapping in time.
- The slowest of the three reviewers can be named directly from the trace.

## 5. Non-Functional Requirements

### NFR-1 — Secrets
Governed by Article II of `constitution.md`. A missing required key fails startup with one clear
sentence. No secret — the developer's own, or one found inside a reviewed diff — is ever written to
`ledger.jsonl` or shown in a report.

### NFR-2 — Cost
Every agent declares its own model settings; nothing generates without an enforced turn ceiling.

### NFR-3 — Observability
Every review is traceable (FR-13), and every run appears in `ledger.jsonl` (FR-11) — durable, not
only printed to the terminal.

### NFR-4 — Failure
A tool that meets bad input (malformed diff, missing ruleset) returns a sentence the model can use.
A tool that raises into the runner is a defect, not a tolerated failure mode.

### NFR-5 — Provenance
`git log`, read in chronological order, MUST show the four Phase 0 artifacts committed before the
first commit touching any runtime file.

## 6. Data Contracts (behavioral shape only)

### 6.1 ReviewContext
A record holding: repository name, language, ruleset id, and strictness (`"normal"` or `"strict"`,
defaulting to `"normal"`). Invalid values are rejected at construction time.

### 6.2 Finding
A record with: file, line, severity (`"critical"`, `"major"`, or `"minor"`), and a message.

### 6.3 Report (implied by FR-6, not separately named in the brief)
The merged, deduplicated, severity-ordered list of findings the Desk presents, plus the footer data
from FR-10 (per-reviewer latency and token counts).

## 7. Cut List (in order, if time runs short during implementation)

1. FR-11 (ledger / custom runner)
2. FR-7 (cheaper run-level re-run)
3. The agent-level hooks half of FR-10 (the run-level footer data is retained)

**FR-5 (concurrent reviewers) and FR-8 (output guardrail) MUST NOT be cut under any
circumstance** — concurrency and the leak-prevention guardrail are what this project is for, and
what the viva is built around.

## 8. Glossary

- **Desk** — the entry point; splits the diff, launches reviewers, merges results, decides on
  remediation.
- **Reviewer** — one of the three concurrent specialists (Security, Tests, Style).
- **Merge Specialist** — deduplicates and orders findings; exposed as a tool, not a handoff target.
- **Remediation Specialist** — proposed a fix on a critical finding; reached by handoff.
- **Finding** — one typed issue reported by a reviewer.
- **Ledger** — the durable, append-only record of every individual run (`ledger.jsonl`).
- **Trace / span** — the recorded structure of one review, and one named unit of work within it.
- **Ceiling** — the maximum number of turns a single reviewer's run may take.
