"""Tests for tools/root_markdown.py; run with `python -m unittest discover -s tools`."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import root_markdown

REPO_ROOT = Path(__file__).resolve().parent.parent


def touch(path: Path) -> None:
    path.write_bytes(b"# placeholder\n")


class RootMarkdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        touch(self.root / "README.md")
        touch(self.root / "CHANGELOG.md")
        touch(self.root / "LICENSE")
        (self.root / "addon").mkdir()
        touch(self.root / "addon" / "README.md")
        touch(self.root / "addon" / "NOTES.md")

    def test_allowed_files_only_pass(self) -> None:
        self.assertEqual(root_markdown.disallowed(self.root), [])

    def test_seeded_extra_root_markdown_file_fails(self) -> None:
        touch(self.root / "CONTRIBUTING.md")
        self.assertEqual(root_markdown.disallowed(self.root), ["CONTRIBUTING.md"])

    def test_other_markdown_extensions_and_spellings_fail(self) -> None:
        for name in ("notes.markdown", "Readme.mkd", "Guide.MD", "page.mdx"):
            touch(self.root / name)
        self.assertEqual(
            root_markdown.disallowed(self.root),
            ["Guide.MD", "Readme.mkd", "notes.markdown", "page.mdx"],
        )

    def test_directory_with_markdown_name_is_ignored(self) -> None:
        (self.root / "docs.md").mkdir()
        self.assertEqual(root_markdown.disallowed(self.root), [])

    def test_main_exit_codes(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(root_markdown.main([str(self.root)]), 0)
            touch(self.root / "EXTRA.md")
            self.assertEqual(root_markdown.main([str(self.root)]), 1)
        self.assertIn("EXTRA.md", err.getvalue())

    def test_main_rejects_missing_directory(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            root_markdown.main([str(self.root / "missing")])
        self.assertEqual(raised.exception.code, 2)


class RepositoryRootTests(unittest.TestCase):
    def test_repository_root_holds_only_allowed_markdown_files(self) -> None:
        self.assertEqual(root_markdown.disallowed(REPO_ROOT), [])


if __name__ == "__main__":
    unittest.main()
