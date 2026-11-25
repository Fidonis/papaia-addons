"""Tests for tools/release_line.py; run with `python -m unittest discover -s tools`."""

from __future__ import annotations

import contextlib
import datetime
import io
import tempfile
import unittest
from pathlib import Path

import release_line
import yaml

HERE = Path(__file__).resolve().parent
WORKFLOWS = HERE.parent / ".github" / "workflows"


def entry(**overrides: object) -> dict:
    base: dict = {
        "id": "CVE-2099-1000",
        "package": "libdemo",
        "image": "docker.io/library/postgres",
        "digest": "sha256:" + "c" * 64,
        "severity": "HIGH",
        "reason": "Distribution fix available, the upstream image is not rebuilt yet.",
        "added": datetime.date(2026, 11, 20),
        "expires": datetime.date(2026, 12, 4),
        "approved_by": "security officer",
    }
    base.update(overrides)
    return base


class ReleaseLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, entries: list[dict]) -> Path:
        path = self.tmp / "exceptions.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "exceptions": entries}, sort_keys=False), encoding="utf-8")
        return path

    def run_check(self, path: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release_line.main(["--exceptions", str(path)])
        return code, out.getvalue(), err.getvalue()

    def test_pending_entry_refuses_the_release_and_is_named(self) -> None:
        path = self.write([
            entry(),
            entry(
                id="CVE-2099-2000",
                package="libpending",
                approved_by="pending (security officer countersignature)",
            ),
        ])
        code, _, err = self.run_check(path)
        self.assertEqual(code, 1)
        self.assertIn("CVE-2099-2000 libpending docker.io/library/postgres", err)
        self.assertNotIn("CVE-2099-1000", err)
        self.assertIn("1 exception entry pending", err)

    def test_every_spelling_of_pending_refuses(self) -> None:
        for approval in ("pending", "Pending (security officer)", "  PENDING"):
            with self.subTest(approval=approval):
                self.assertEqual(self.run_check(self.write([entry(approved_by=approval)]))[0], 1)

    def test_all_entries_approved_pass(self) -> None:
        code, out, _ = self.run_check(self.write([entry(), entry(id="CVE-2099-2")]))
        self.assertEqual(code, 0)
        self.assertIn("no exception entry pending", out)

    def test_empty_list_passes(self) -> None:
        self.assertEqual(self.run_check(self.write([]))[0], 0)

    def test_invalid_file_is_refused(self) -> None:
        self.assertEqual(self.run_check(self.write([entry(note="x")]))[0], 2)
        self.assertEqual(self.run_check(self.tmp / "missing.yaml")[0], 2)

    def test_ci_runs_the_check_on_the_release_line_only(self) -> None:
        jobs = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))["jobs"]
        steps = [s for s in jobs["components"]["steps"] if "tools/release_line.py" in str(s.get("run", ""))]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["if"], "github.ref == 'refs/heads/main' || github.base_ref == 'main'")

    def test_published_release_runs_the_check(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "release-drafter.yml").read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(triggers["release"], {"types": ["published"]})
        checks = [
            job for job in workflow["jobs"].values()
            if any("tools/release_line.py" in str(s.get("run", "")) for s in job["steps"])
        ]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["if"], "github.event_name == 'release'")
        self.assertEqual(workflow["jobs"]["update_release_draft"]["if"], "github.event_name != 'release'")


if __name__ == "__main__":
    unittest.main()
