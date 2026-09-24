"""Configuration for the Code Review Desk.

Constitutional notes:
- Article I.1: the model is *named* here, but it MUST be passed into each `Agent`
  definition's own configuration. Nothing in this module configures a model for
  anyone; it only exposes the identifier and a client factory.
- Article I.2: there is deliberately no `set_default_openai_client` call — and no
  other process-wide client override — anywhere in this file.
- Article II.3: a missing credential raises `ConfigError`, which the entry point
  catches and prints as a single human-readable line. The user never sees a
  traceback.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from agents import OpenAIChatCompletionsModel, set_tracing_export_api_key
from dotenv import load_dotenv
from openai import AsyncOpenAI

# Article I.1, as amended (constitution.md v1.1.0, Article IX): gemini-2.5-flash
# errors on this project's API key, so gemini-3.6-flash is the mandated default.
MODEL_NAME = "gemini-3.6-flash"

# FR-7's "cheaper second opinion", overridden at the RUN level only — never
# assigned to an agent (Article I.3).
#
# Chosen from what this key can actually reach (`client.models.list()`): the lite
# tiers available are gemini-3.5-flash-lite, gemini-3.1-flash-lite,
# gemini-2.5-flash-lite and the alias gemini-flash-lite-latest. There is no
# gemini-3.6-flash-lite. gemini-3.5-flash-lite is the pick: "lite" is a genuinely
# cheaper tier rather than an older full-size model, and 3.5 is the closest
# generation below the mandated 3.6, so the second opinion differs in cost rather
# than in era. The `-latest` alias is deliberately avoided — it moves under us,
# and Article IV.1 requires a review to be reproducible.
CHEAP_MODEL_NAME = "gemini-3.5-flash-lite"

# Gemini's OpenAI-compatible endpoint.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# plan.md §10 — per-reviewer turn ceiling. Independent per reviewer: three
# concurrent reviewers each get their own budget, not a shared one.
REVIEWER_MAX_TURNS = 6

API_KEY_VAR = "GEMINI_API_KEY"

# FR-13 — see `configure_tracing`.
TRACING_KEY_VAR = "TRACING_EXPORT_KEY"
OPENAI_KEY_PREFIX = "sk-"
GEMINI_KEY_PREFIX = "AIza"

# See `Config.build_client`. Kept deliberately low: the SDK's backoff caps at a
# few seconds, so aggressive retrying against a per-minute quota makes things
# worse — 3 reviewers x 7 attempts is 21 requests hammering a window that never
# gets a chance to clear (measured: 429s with 45-57s retry hints). A proper fix
# would honor the RetryInfo delay Gemini returns in the error body, which the
# OpenAI client does not read; until then, fewer attempts is strictly better.
MAX_CLIENT_RETRIES = 3


class ConfigError(RuntimeError):
    """Startup configuration is unusable. Carries a user-ready message."""


@dataclass(frozen=True)
class Config:
    api_key: str
    model_name: str = MODEL_NAME
    base_url: str = GEMINI_BASE_URL
    reviewer_max_turns: int = REVIEWER_MAX_TURNS

    def build_client(self) -> AsyncOpenAI:
        """Return a fresh client. Per Article I.2, this is never installed as a
        global default — callers hand it to the agent that needs it.

        `max_retries` is raised above the SDK default of 2 because this project's
        key is on the Gemini free tier: 5 requests/minute, while one review needs
        about six (three reviewers, Style twice for its tool call, Merge, and
        Remediation). Transient 503 "high demand" responses from this endpoint are
        common on top of that. Rate limiting here is an expected operating
        condition, not a defect, and the retries give the per-minute window time
        to roll over instead of reporting a reviewer as failed.
        """
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=MAX_CLIENT_RETRIES,
        )


@lru_cache(maxsize=4)
def build_model(model_name: str) -> OpenAIChatCompletionsModel:
    """A Model object for `model_name`, bound to this project's Gemini client.

    Used for FR-7's run-level override, which must pass a **Model object** and not
    a bare model-name string: `RunConfig.model` accepts either, but a string is
    resolved through `RunConfig.model_provider`, which defaults to OpenAI's
    provider and would send the run to api.openai.com. A Model object carries the
    Gemini client with it.

    Cached per name so repeated overrides reuse one client, and still not a global
    default — callers hand it to a specific run (Article I.2).
    """
    config = load_config()
    return OpenAIChatCompletionsModel(
        model=model_name,
        openai_client=config.build_client(),
    )


def configure_tracing() -> None:
    """Point the SDK's trace exporter at the developer's own key (FR-13, Article
    VII.1). Call once at startup, from each entry point; safe to call again.

    Mechanism: `agents.set_tracing_export_api_key(key)`, the SDK's setter for the
    exporter's key. Without it the exporter falls back to OPENAI_API_KEY, which
    this project does not set — traces would be silently dropped.

    The key must be an OpenAI key (`sk-...`), not the Gemini key: the exporter
    ships traces to OpenAI's platform no matter which model answered the review.
    A Gemini-shaped key is rejected by name rather than failing later as an opaque
    401 in a background thread. The key's value is never included in any message
    (Article II.4).

    Raises `ConfigError` with one clear line if the key is missing, empty, or the
    wrong shape (Article II.3) — tracing is required, not optional.
    """
    load_dotenv()
    key = (os.getenv(TRACING_KEY_VAR) or "").strip()
    if not key:
        raise ConfigError(
            f"{TRACING_KEY_VAR} is missing or empty. Add a line "
            f"'{TRACING_KEY_VAR}=<your OpenAI key, sk-...>' to the .env file at the "
            f"project root (see .env.example)."
        )
    if key.startswith(GEMINI_KEY_PREFIX):
        raise ConfigError(
            f"{TRACING_KEY_VAR} looks like a Gemini API key. Traces are exported to "
            f"OpenAI's platform, so this must be an OpenAI key (it starts with sk-)."
        )
    if not key.startswith(OPENAI_KEY_PREFIX):
        raise ConfigError(
            f"{TRACING_KEY_VAR} does not look like an OpenAI API key (expected it to "
            f"start with sk-). Traces are exported to OpenAI's platform."
        )
    set_tracing_export_api_key(key)


def load_config() -> Config:
    """Load and validate configuration from `.env`.

    Raises `ConfigError` with one clear, tracebackless message if the API key is
    missing or empty (Article II.3).
    """
    load_dotenv()
    api_key = (os.getenv(API_KEY_VAR) or "").strip()
    if not api_key:
        raise ConfigError(
            f"{API_KEY_VAR} is missing or empty. Add a line "
            f"'{API_KEY_VAR}=<your-key>' to the .env file at the project root "
            f"(see .env.example for the expected shape)."
        )
    return Config(api_key=api_key)
