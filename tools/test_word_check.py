"""Tests for tools/word_check.py; run with `python -m unittest discover -s tools`.

Needs papaia-kit (tools/ci); skipped where it is not installed, as in the
plain `python -m unittest` run without the CI tool set.
"""

from __future__ import annotations

import base64
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import papaia_kit.repo  # noqa: F401
except ImportError:  # pragma: no cover - the CI job installs the kit
    word_check = None
else:
    import word_check

# A denied term, encoded so that this file itself stays clean.
DENIED = base64.b64decode("Q29waWxvdA==").decode()


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


@unittest.skipIf(word_check is None, "papaia-kit is not installed")
class WordCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "dev@example.com")
        git(self.root, "config", "user.name", "Dev")
        self.commit("README.md", f"old line naming {DENIED}\nclean line\n")
        self.base = git(self.root, "rev-parse", "HEAD").strip()

    def commit(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")
        git(self.root, "add", name)
        git(self.root, "commit", "-q", "-m", "change")

    def run_check(self, revision_range: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = word_check.main(["--root", str(self.root), revision_range])
        return code, err.getvalue()

    def test_added_line_with_denied_term_fails(self) -> None:
        self.commit("new.md", f"a new line naming {DENIED}\n")
        code, err = self.run_check(f"{self.base}..HEAD")
        self.assertEqual(code, 1)
        self.assertIn("new.md:1", err)

    def test_existing_lines_are_not_reported(self) -> None:
        self.commit("README.md", f"old line naming {DENIED}\nclean line\nanother clean line\n")
        self.assertEqual(self.run_check(f"{self.base}..HEAD"), (0, ""))

    def test_removed_line_is_not_reported(self) -> None:
        self.commit("README.md", "clean line\n")
        self.assertEqual(self.run_check(f"{self.base}..HEAD"), (0, ""))

    def test_added_line_number_is_the_new_line(self) -> None:
        self.commit("README.md", f"old line naming {DENIED}\nclean line\nx {DENIED}\n")
        code, err = self.run_check(f"{self.base}..HEAD")
        self.assertEqual(code, 1)
        self.assertIn("README.md:3", err)
        self.assertNotIn("README.md:1", err)

    def test_binary_file_is_skipped(self) -> None:
        (self.root / "blob.bin").write_bytes(b"\0\1" + DENIED.encode())
        git(self.root, "add", "blob.bin")
        git(self.root, "commit", "-q", "-m", "binary")
        self.assertEqual(self.run_check(f"{self.base}..HEAD"), (0, ""))

    def test_invalid_range_is_a_usage_error(self) -> None:
        code, _ = self.run_check("main..HEAD;true")
        self.assertEqual(code, 2)
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            word_check.main(["--root", str(self.root), "--output=/tmp/x"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
