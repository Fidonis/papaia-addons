"""Tests for the start boundary of the commit check (commit-boundary.toml).

`papaia-kit repo commits` reads the file at the repository root: the named
commit and its ancestors are not checked, every later commit is. The commit
rules apply from the first commit after the one named here.
"""

from __future__ import annotations

import subprocess
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOUNDARY = REPO_ROOT / "commit-boundary.toml"

START_COMMIT = "e0de06ffc2a94d5b819368a9e93bd3ddef545829"
REASON = (
    "Commit metadata rules apply from this commit on; the author of this commit "
    "confirmed the Developer Certificate of Origin 1.1 for it in writing on 2026-11-24."
)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False
    )


class CommitBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = tomllib.loads(BOUNDARY.read_text(encoding="utf-8"))

    def test_file_names_exactly_one_commit_and_its_reason(self) -> None:
        self.assertEqual(
            self.config, {"version": 1, "commit": START_COMMIT, "reason": REASON}
        )

    def test_reason_is_one_line(self) -> None:
        self.assertNotIn("\n", self.config["reason"])
        self.assertLessEqual(len(self.config["reason"]), 300)

    def test_commit_is_an_ancestor_of_head(self) -> None:
        if git("cat-file", "-e", f"{START_COMMIT}^{{commit}}").returncode != 0:
            self.skipTest("history not available in this clone")
        result = git("merge-base", "--is-ancestor", START_COMMIT, "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
