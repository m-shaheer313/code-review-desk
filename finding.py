"""The structured reviewer output type (FR-3).

A reviewer returns `list[Finding]` — never prose. Finding nothing means an empty
list, not a sentence saying "no issues found".

`severity` is a `Literal`, so a value outside the three allowed strings fails
validation and surfaces as a structured-output parsing error rather than being
silently coerced (spec.md §4.3).
"""

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "major", "minor"]


class Finding(BaseModel):
    file: str
    line: int
    severity: Severity
    message: str = Field(min_length=1)
