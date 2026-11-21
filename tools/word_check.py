#!/usr/bin/env python3
"""Denied terms in the lines a revision range adds.

Uses the term list of papaia-kit (`papaia_kit.repo.find_denied_words`, the
same list as `papaia-kit repo words`), but reads only the lines that the range
adds, so a change is held to the rule without failing on text that was there
before it. Commit messages of the range are checked by
`papaia-kit repo commits`.

Usage: python tools/word_check.py BASE..HEAD
Exit codes: 0 clean, 1 denied terms in added lines, 2 usage error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from papaia_kit.repo import find_denied_words, valid_range

REPO_ROOT = Path(__file__).resolve().parent.parent
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


def added_lines(root: Path, revision_range: str) -> list[tuple[str, int, str]]:
    """(path, line number in the new file, text) of every added text line."""
    # Fixed argument list without a shell; the range is checked by valid_range.
    diff = subprocess.run(
        [
            "git", "-C", str(root), "diff", "--no-color", "--no-ext-diff", "--no-renames",
            "-U0", revision_range, "--",
        ],
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    ).stdout
    found: list[tuple[str, int, str]] = []
    path: str | None = None
    header = False
    number = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, header = None, True
            continue
        if header:
            # File header up to the first hunk; an added line that starts with
            # "++" cannot be mistaken for it.
            if line.startswith("+++ "):
                target = line[4:]
                path = target[2:] if target.startswith("b/") else None
            hunk = HUNK.match(line)
            if hunk:
                header, number = False, int(hunk["start"])
            continue
        hunk = HUNK.match(line)
        if hunk:
            number = int(hunk["start"])
            continue
        if path is not None and line.startswith("+"):
            found.append((path, number, line[1:]))
            number += 1
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("range", help="revision range, e.g. origin/main..HEAD")
    args = parser.parse_args(argv)
    if not valid_range(args.range):
        print(f"word check: not a revision range: {args.range!r}", file=sys.stderr)
        return 2
    try:
        lines = added_lines(args.root, args.range)
    except subprocess.CalledProcessError as exc:
        print(f"word check: git diff failed: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    failed = 0
    for path, number, text in lines:
        words = find_denied_words(text)
        if words:
            print(f"{path}:{number}: {', '.join(words)}", file=sys.stderr)
            failed += 1
    print(f"word check: {len(lines)} added lines, {failed} with denied terms")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
