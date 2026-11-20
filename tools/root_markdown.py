#!/usr/bin/env python3
"""Fail when the repository root holds a Markdown file other than the allowed ones.

The root carries only the files a reader of the catalog needs first:
README.md and CHANGELOG.md. Contributor and development documentation
lives under docs/, add-on documentation next to each add-on.

Usage: python tools/root_markdown.py [ROOT]

ROOT defaults to the repository this script belongs to. Exits 0 when the
root is clean, 1 when it holds another Markdown file, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ALLOWED = frozenset({"CHANGELOG.md", "README.md"})
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".mkd", ".mdx"})
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def is_markdown(path: Path) -> bool:
    return path.suffix.lower() in MARKDOWN_SUFFIXES


def disallowed(root: Path) -> list[str]:
    """Return the sorted names of root-level Markdown files that are not allowed."""
    return sorted(
        entry.name
        for entry in root.iterdir()
        if not entry.is_dir() and is_markdown(entry) and entry.name not in ALLOWED
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("root", nargs="?", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    root: Path = args.root
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    found = disallowed(root)
    if found:
        allowed = ", ".join(sorted(ALLOWED))
        for name in found:
            print(f"{name}: Markdown file at the repository root; only {allowed} are allowed there", file=sys.stderr)
        return 1
    print(f"root Markdown files OK ({', '.join(sorted(ALLOWED))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
