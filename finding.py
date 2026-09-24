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
    # Which reviewer produced this finding. FR-6's remediation handoff fires on a
    # critical finding *from the Security Reviewer* specifically, so the source
    # has to travel with the finding.
    #
    # The model is not trusted for this value: `review_runner` overwrites it on
    # every finding with the name of the reviewer whose run actually produced it.
    # Anything the model puts here is discarded. Keep the default empty so an
    # unstamped finding is visibly unstamped rather than plausibly mislabeled.
    source_reviewer: str = ""
