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

from dotenv import load_dotenv
from openai import AsyncOpenAI

# Article I.1, as amended (constitution.md v1.1.0, Article IX): gemini-2.5-flash
# errors on this project's API key, so gemini-3.6-flash is the mandated default.
MODEL_NAME = "gemini-3.6-flash"

# Gemini's OpenAI-compatible endpoint.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# plan.md §10 — per-reviewer turn ceiling. Independent per reviewer: three
# concurrent reviewers each get their own budget, not a shared one.
REVIEWER_MAX_TURNS = 6

API_KEY_VAR = "GEMINI_API_KEY"


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
        global default — callers hand it to the agent that needs it."""
        return AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)


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
