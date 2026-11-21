#!/usr/bin/env python3
"""Hold every add-on image to a fixed version and digest.

Each `${VAR:-ref}` default in an add-on's `docker-compose.yml` must read
`[registry/]name:tag@sha256:<digest>`:

* the tag names a release (`components.is_fixed`: a patch level such as
  `3.0.5`, or major.minor such as `18.7` only for an upstream without patch
  levels) and no moving name such as `latest`, `dev`, `main` or `nightly`;
* the digest is the sha256 of the image index the tag pointed to when it was
  pinned, so a re-pushed tag cannot change what an installation pulls.

The tag stays next to the digest because people and tools read the version
from it (`components.py`, the README tables, papaia-manager); the digest is
what Docker pulls.

Commands:
  check   fail on any default that is not pinned this way (CI runs this)
  list    print the pinned defaults as a sorted JSON list, each once (the
          vulnerability scan's matrix); fails like `check` on any unpinned one
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import components

REPO_ROOT = Path(__file__).resolve().parent.parent

# Add-ons outside the delivered set; their directories are not read here.
EXCLUDED = {
    "n8n": "not part of the integrated delivery; maintained outside this check",
}

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
)
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
# Tag segments that name a moving target rather than a release.
MOVING = frozenset(
    {"latest", "dev", "develop", "edge", "nightly", "main", "master", "stable", "snapshot", "trunk"}
)


def pin_problem(ref: str) -> str | None:
    """Why `ref` is not pinned by fixed tag and digest, or None."""
    name_tag, sep, digest = ref.partition("@")
    if not sep:
        return f"`{ref}` has no digest; pin it as name:tag@sha256:<digest>"
    if not DIGEST.match(digest):
        return f"`{ref}`: digest must be sha256: followed by 64 lower-case hex digits"
    head, _, last = name_tag.rpartition("/")
    if ":" not in last:
        return f"`{ref}` has no tag; keep the version tag next to the digest"
    repo_last, tag = last.split(":", 1)
    name = f"{head}/{repo_last}" if head else repo_last
    if not NAME.match(name):
        return f"`{ref}`: `{name}` is not an image name"
    if not TAG.match(tag):
        return f"`{ref}`: `{tag}` is not a tag"
    segments = set(re.split(r"[._+-]", tag.lower()))
    if segments & MOVING:
        return f"`{ref}`: tag `{tag}` names a moving target"
    if not components.is_fixed(ref):
        return f"`{ref}`: tag `{tag}` floats; name the release with its patch level"
    return None


def checked_addons(root: Path) -> list[Path]:
    return [p for p in components.addon_dirs(root) if p.name not in EXCLUDED]


def check_addon(path: Path) -> list[str]:
    compose = path / components.COMPOSE
    if not compose.is_file():
        return []
    images, unreadable = components.read_compose_images(compose.read_text(encoding="utf-8"))
    problems = [f"image `{value}` is not a ${{VAR:-ref}} default" for value in unreadable]
    for var, ref in sorted(images.items()):
        problem = pin_problem(ref)
        if problem:
            problems.append(f"{var}: {problem}")
    return problems


def pinned_refs(addons: list[Path]) -> set[str]:
    refs: set[str] = set()
    for path in addons:
        compose = path / components.COMPOSE
        if compose.is_file():
            images, _ = components.read_compose_images(compose.read_text(encoding="utf-8"))
            refs.update(images.values())
    return refs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    failed = False
    addons = checked_addons(args.root)
    for path in addons:
        for problem in check_addon(path):
            print(f"{path.name}: {problem}", file=sys.stderr)
            failed = True
    if args.command == "list":
        if failed:
            return 1
        print(json.dumps(sorted(pinned_refs(addons))))
        return 0
    print(f"image pins: {len(addons)} add-ons checked, {'failed' if failed else 'ok'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
