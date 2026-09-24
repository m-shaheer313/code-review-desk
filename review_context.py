"""The per-review local run context (FR-2).

`ReviewContext` is the *only* channel through which repository, language,
ruleset id, and strictness reach tools and instruction builders. Per Article
IV.2 it MUST travel via the SDK's local run context, never interpolated into a
static prompt template — so grepping the text actually sent to the model finds no
repository name anywhere.

Validation is eager (Article IV / spec.md §4.2): an invalid `strictness` is
rejected the moment the context is constructed, before any review begins, rather
than lazily when a tool happens to read the field.
"""

from dataclasses import dataclass

STRICTNESS_VALUES = ("normal", "strict")


@dataclass
class ReviewContext:
    repo: str
    language: str
    ruleset_id: str
    strictness: str = "normal"  # "normal" | "strict" — validated in __post_init__

    def __post_init__(self) -> None:
        if self.strictness not in STRICTNESS_VALUES:
            raise ValueError(
                f"Invalid strictness: {self.strictness!r} "
                f"(expected one of {', '.join(map(repr, STRICTNESS_VALUES))})"
            )
