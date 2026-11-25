#!/usr/bin/env python3
"""Refuse a release while an exception entry waits for its countersignature.

An entry of `security/exceptions.yaml` whose `approved_by` starts with
`pending` waives nothing in the scan gate (tools/scan_gate.py), and on a
milestone branch the gate lists it and goes on. A release must not carry one:
this check names id, package and image of each pending entry and exits
non-zero. CI runs it for `main` and every pull request into it, and again
when a release is published.

Exit codes: 0 no entry pending, 1 at least one entry pending, 2 the exception
list is unreadable or invalid.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import scan_gate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--exceptions", type=Path, default=scan_gate.DEFAULT_EXCEPTIONS)
    args = parser.parse_args(argv)
    try:
        entries = scan_gate.load_exceptions(args.exceptions)
    except scan_gate.GateError as exc:
        print(f"release line: {exc}", file=sys.stderr)
        return 2
    pending = [e for e in entries if not e.approved]
    for item in pending:
        print(f"pending: {item.vuln_id} {item.package} {item.image}", file=sys.stderr)
    if pending:
        noun = "entry" if len(pending) == 1 else "entries"
        print(
            f"release line: {len(pending)} exception {noun} pending the security officer's "
            f"countersignature in {args.exceptions}; no release until each is signed or removed",
            file=sys.stderr,
        )
        return 1
    print(f"release line: no exception entry pending ({len(entries)} approved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
