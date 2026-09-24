"""Terminal entry point — FR-1 slice only.

Reads a unified diff from a path on the command line, splits it per file, and
reports how many chunks were found. No agent, tool, or ReviewContext is involved
yet; nothing here calls a model.

The entry point is async and launched through `asyncio.run` (FR-1's third
acceptance criterion). Per Article VIII.2, every expected failure — missing
credential, unreadable path, empty or malformed diff — is caught here, at this
one boundary, and printed as a plain line.
"""

import asyncio
import sys
from pathlib import Path

from config import ConfigError, load_config
from diff_utils import split_diff_by_file

USAGE = "usage: python main.py <path-to-diff-file>"


def _read_diff_file(path: Path) -> tuple[str | None, str | None]:
    """Return `(diff_text, error_message)` — never raises for a bad path."""
    # Checked up front: on Windows, reading a directory surfaces as
    # PermissionError, which would otherwise produce a misleading message.
    if path.is_dir():
        return None, f"that path is a directory, not a diff file: {path}"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except FileNotFoundError:
        return None, f"no such diff file: {path}"
    except PermissionError:
        return None, f"permission denied reading: {path}"
    except OSError as exc:
        return None, f"could not read {path}: {exc.strerror or exc}"


async def main() -> int:
    if len(sys.argv) != 2:
        print(USAGE)
        return 2

    try:
        # Validates GEMINI_API_KEY now, so a misconfigured run fails before any
        # work rather than halfway through a review (Article II.3).
        load_config()
    except ConfigError as exc:
        print(str(exc))
        return 1

    diff_path = Path(sys.argv[1])
    diff_text, read_error = _read_diff_file(diff_path)
    if read_error is not None:
        print(read_error)
        return 1

    chunks, split_error = split_diff_by_file(diff_text)
    if split_error is not None:
        print(split_error)
        return 1

    print(f"{len(chunks)} file chunk{'s' if len(chunks) != 1 else ''} found:")
    for chunk in chunks:
        note = "" if chunk["has_text_changes"] else "  (no reviewable text changes)"
        print(f"  - {chunk['file']}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
