# Code Review Desk

Code Review Desk reviews a unified diff with three AI reviewers that run concurrently — Security,
Tests and Style — built on the OpenAI Agents SDK with Gemini as the model backend. The diff is
split per file before any model sees it; the three reviewers' findings are then deduplicated and
ordered by severity (critical first). If the Security reviewer reports a critical issue, a
remediation specialist proposes a fix — a proposal only, never applied. An output guardrail
refuses to show any report that contains credential-shaped text. Every review is traced as a
single trace and logged, one line per reviewer run, to `ledger.jsonl`.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it if needed).

```bash
uv sync
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

Then edit `.env` and set both keys:

| Variable | What it is |
|---|---|
| `GEMINI_API_KEY` | Your Google Gemini API key. The reviewers run on `gemini-3.6-flash`. |
| `TRACING_EXPORT_KEY` | An **OpenAI** API key (`sk-...`), not the Gemini key. Traces are exported to OpenAI's platform regardless of which model ran the review. |

Both are required. If either is missing or has the wrong form, you get a one-line message naming
the problem: the terminal version stops, and the browser version shows it in the chat.

## Running it

**Terminal** — pass the path to a diff file:

```bash
git diff > change.diff
uv run python main.py change.diff
```

The terminal uses a default review context: Python, ruleset `python-default`, normal strictness.

**Browser** — start the Chainlit interface:

```bash
uv run chainlit run app.py
```

Then open the URL it prints (usually http://localhost:8000) and paste a diff. Each reviewer's
progress appears as it finishes, followed by the full report. To set the review context, start
your message with a line like:

```
context: repo=my-repo language=Python ruleset=python-default strictness=strict
```

Any subset of keys works, and the context carries over to later diffs in the same session.

## How it was built

Built spec-first: `constitution.md`, `spec.md`, `plan.md` and `tasks.md` were committed as Phase 0
before any code, as NFR-5 requires.
