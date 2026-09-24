"""Unified-diff preprocessing (FR-1).

This module performs the split that happens **before any model ever sees the
diff**: it makes no API call, imports no agent, and has no knowledge of
`ReviewContext`.

Error-reporting pattern (picked once, used consistently): every public function
returns `(chunks, error_message)` and **never raises** for bad input, matching
plan.md §3 ("Empty/malformed diff → returns an empty list with a reported
reason; never raises") and Article VIII.1. `error_message` is `None` on success
and a short, user-ready sentence otherwise.

Per Article II.4, error messages describe the *shape* of the problem and never
echo diff content back — a malformed diff may itself contain real credentials.
"""

import re

# `diff --git a/path b/path`
_GIT_HEADER_RE = re.compile(r"^diff --git (\S+) (\S+)\s*$")
# `--- a/path` / `+++ b/path`, with optional trailing tab-separated timestamp.
_OLD_FILE_RE = re.compile(r"^--- (\S+)")
_NEW_FILE_RE = re.compile(r"^\+\+\+ (\S+)")
# `@@ -12,7 +12,9 @@ optional section heading`
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BINARY_RE = re.compile(r"^(?:GIT binary patch\b|Binary files .* differ\s*$)")

EMPTY_DIFF_MESSAGE = "no changes found to review"


def _strip_path_prefix(path: str) -> str:
    """Drop git's `a/` / `b/` prefix; leave everything else alone."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _new_section(from_git_header: bool) -> dict:
    return {
        "lines": [],
        "old_path": None,
        "new_path": None,
        "binary": False,
        "hunks": 0,
        # `diff --git` sections are delimited by that header alone; their own
        # `---` line must not be read as the start of the next file.
        "from_git_header": from_git_header,
    }


def _resolve_path(section: dict) -> str | None:
    """The file a chunk is *about*: the new path, or the old one for a deletion."""
    new_path, old_path = section["new_path"], section["old_path"]
    if new_path and new_path != "/dev/null":
        return _strip_path_prefix(new_path)
    if old_path and old_path != "/dev/null":
        return _strip_path_prefix(old_path)
    return None


def split_diff_by_file(diff_text: str) -> tuple[list[dict], str | None]:
    """Split a unified diff into one chunk per file.

    Returns `(chunks, error_message)`. Each chunk is a dict:

        {
            "file": "src/app.py",       # path the chunk is about
            "diff_text": "diff --git…", # that file's slice of the diff, verbatim
            "binary": False,            # binary patch / "Binary files … differ"
            "has_text_changes": True,   # False for binary or hunkless renames
        }

    On empty, whitespace-only, or non-diff input, returns `([], "<reason>")`.
    Never raises.
    """
    if not isinstance(diff_text, str) or not diff_text.strip():
        return [], EMPTY_DIFF_MESSAGE

    sections: list[dict] = []
    current: dict | None = None
    # Hunk body accounting, so that a *content* line such as a removed "-- x"
    # (which arrives as "--- x") is never mistaken for a file header.
    old_remaining = new_remaining = 0

    for line in diff_text.splitlines():
        # A hunk body line always starts with ' ', '+', '-' or '\' (or is bare
        # empty, which some tools emit for a blank context line). Requiring that
        # shape means a wrong line count in the `@@` header can never swallow the
        # next file's header.
        in_hunk_body = (old_remaining > 0 or new_remaining > 0) and (
            line == "" or line[0] in " +-\\"
        )
        if not in_hunk_body:
            old_remaining = new_remaining = 0

            git_header = _GIT_HEADER_RE.match(line)
            if git_header:
                if current is not None:
                    sections.append(current)
                current = _new_section(from_git_header=True)
                current["old_path"] = git_header.group(1)
                current["new_path"] = git_header.group(2)
                current["lines"].append(line)
                continue

            old_file = _OLD_FILE_RE.match(line)
            # A `---` starts a new section only in plain (non-`diff --git`)
            # diffs: either nothing is open yet, or the open plain section
            # already has its own `+++` / hunks behind it.
            starts_plain_section = old_file and (
                current is None
                or (
                    not current["from_git_header"]
                    and (current["new_path"] is not None or current["hunks"])
                )
            )
            if starts_plain_section:
                if current is not None:
                    sections.append(current)
                current = _new_section(from_git_header=False)
                current["old_path"] = old_file.group(1)
                current["lines"].append(line)
                continue

        if current is None:
            # Preamble before the first file header (commit message, cover
            # letter, stray blank lines) — not part of any chunk.
            continue

        current["lines"].append(line)

        if in_hunk_body:
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            if line.startswith("-"):
                old_remaining = max(0, old_remaining - 1)
            elif line.startswith("+"):
                new_remaining = max(0, new_remaining - 1)
            else:  # context line (leading space, or a bare empty line)
                old_remaining = max(0, old_remaining - 1)
                new_remaining = max(0, new_remaining - 1)
            continue

        hunk = _HUNK_RE.match(line)
        if hunk:
            current["hunks"] += 1
            old_remaining = int(hunk.group(2) or 1)
            new_remaining = int(hunk.group(4) or 1)
            continue

        old_file = _OLD_FILE_RE.match(line)
        if old_file and current["old_path"] in (None, old_file.group(1)):
            current["old_path"] = old_file.group(1)
            continue
        new_file = _NEW_FILE_RE.match(line)
        if new_file:
            current["new_path"] = new_file.group(1)
            continue
        if _BINARY_RE.match(line):
            current["binary"] = True

    if current is not None:
        sections.append(current)

    if not sections:
        return [], (
            "this does not look like a unified diff: no 'diff --git' or "
            "'--- / +++' file headers were found"
        )

    chunks: list[dict] = []
    for section in sections:
        path = _resolve_path(section)
        if path is None:
            continue
        chunks.append(
            {
                "file": path,
                "diff_text": "\n".join(section["lines"]),
                "binary": section["binary"],
                "has_text_changes": section["hunks"] > 0 and not section["binary"],
            }
        )

    if not chunks:
        return [], (
            "this does not look like a unified diff: file headers were found but "
            "no file path could be read from them"
        )

    return chunks, None
