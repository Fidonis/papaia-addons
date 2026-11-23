"""Tests for tools/catalogue.py; run with `python -m unittest discover -s tools`."""

from __future__ import annotations

import contextlib
import copy
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import catalogue
import components
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

MODULES = {"papaia-studio", "papaia-data", "papaia-flow", "papaia-assist", "mssql-mcp"}

MANIFEST = """\
name: demo-module
version: 0.3.1
addon_repo: demo-module
requires:
  addon_api: [12]
  addons:
    - {id: papaia-data, optional: true}
    - papaia-flow
papaia_compat: ">=1.10.0"
description: "Demo module for the catalogue tests"
"""

COMPOSE = """\
services:
  app:
    image: ${DEMO_IMAGE:-ghcr.io/example/demo:0.3.1@sha256:%s}
""" % ("a" * 64)

SUITE = """\
schema: papaia.compat-matrix/1
components:
  - manager
  - demo-module
suites:
  - suite: 1.10.0
    status: candidate
    core: ">=1.10.0,<1.11.0"
    addon_api:
      min: 1
      current: 14
    versions:
      manager: ">=1.8.0,<1.9.0"
      demo-module: ">=0.3.0,<0.4.0"
"""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def make_module_repo(base: Path, manifest: str = MANIFEST) -> tuple[Path, str]:
    """A git repository with an add-on bundle under deploy/papaia-addon; returns (repo, commit)."""
    repo = base / "module"
    bundle = repo / "deploy" / "papaia-addon"
    (bundle / "integration").mkdir(parents=True)
    (bundle / "papaia-app.yaml").write_text(manifest, encoding="utf-8")
    (bundle / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    (bundle / "integration" / "client.json").write_text('{"clientId": "demo"}\n', encoding="utf-8")
    script = bundle / "start.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    (repo / "README.md").write_text("outside the bundle\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo, git(repo, "rev-parse", "HEAD")


def entry(commit: str = "1" * 40, digest: str = "sha256:" + "2" * 64) -> dict:
    return {
        "name": "demo-module",
        "title": "Demo",
        "version": "0.3.1",
        "description": "Demo module for the catalogue tests",
        "requires": {
            "addon_api": [12],
            "papaia_compat": ">=1.10.0",
            "addons": [
                {"name": "papaia-data", "optional": True},
                {"name": "papaia-flow", "optional": False},
            ],
        },
        "source": {
            "repo": "https://github.com/example/demo-module.git",
            "commit": commit,
            "path": "deploy/papaia-addon",
        },
        "digest": digest,
    }


def document(*entries: dict) -> dict:
    return {"schema": catalogue.SCHEMA, "entries": list(entries)}


class RepositoryCatalogueTests(unittest.TestCase):
    """The catalogue this repository ships."""

    def setUp(self) -> None:
        self.doc = catalogue.load(REPO_ROOT / catalogue.CATALOGUE)

    def test_catalogue_passes_the_check(self) -> None:
        self.assertEqual(catalogue.check(REPO_ROOT), [])

    def test_catalogue_lists_the_five_modules(self) -> None:
        names = {item["name"] for item in self.doc["entries"]}
        self.assertEqual(names, MODULES)

    def test_every_entry_is_pinned_by_version_commit_and_digest(self) -> None:
        for item in self.doc["entries"]:
            with self.subTest(item["name"]):
                self.assertRegex(item["version"], r"\A\d+\.\d+\.\d+\Z")
                self.assertRegex(item["source"]["commit"], r"\A[0-9a-f]{40}\Z")
                self.assertRegex(item["digest"], r"\Asha256:[0-9a-f]{64}\Z")
                self.assertEqual(item["source"]["path"], "deploy/papaia-addon")

    def test_every_entry_names_its_prerequisites(self) -> None:
        for item in self.doc["entries"]:
            with self.subTest(item["name"]):
                self.assertTrue(item["requires"]["addon_api"])
                self.assertTrue(item["requires"]["papaia_compat"].startswith(">="))

    def test_studio_names_data_as_its_only_optional_neighbour(self) -> None:
        studio = next(i for i in self.doc["entries"] if i["name"] == "papaia-studio")
        self.assertEqual(studio["requires"]["addons"], [{"name": "papaia-data", "optional": True}])

    def test_no_module_requires_another_one(self) -> None:
        for item in self.doc["entries"]:
            for neighbour in item["requires"].get("addons", []):
                self.assertTrue(neighbour["optional"], f"{item['name']} -> {neighbour['name']}")

    def test_file_is_in_canonical_form(self) -> None:
        text = (REPO_ROOT / catalogue.CATALOGUE).read_text(encoding="utf-8")
        self.assertEqual(text, catalogue.render(self.doc))

    def test_components_check_runs_the_catalogue_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / catalogue.CATALOGUE).write_text("schema: other/1\nentries: []\n", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(components.main(["--root", str(root), "check"]), 1)
            self.assertIn("catalogue", stderr.getvalue())


class ValidationTests(unittest.TestCase):
    def problems(self, doc: object, local: set[str] | None = None) -> list[str]:
        return catalogue.validate(doc, local or set())

    def test_valid_document_has_no_problems(self) -> None:
        self.assertEqual(self.problems(document(entry())), [])

    def test_wrong_schema_is_refused(self) -> None:
        doc = document(entry())
        doc["schema"] = "papaia.addon-catalogue/2"
        self.assertTrue(self.problems(doc))

    def test_unknown_fields_are_refused(self) -> None:
        cases = []
        doc = document(entry())
        doc["extra"] = 1
        cases.append(doc)
        for where in ("", "requires", "source"):
            item = entry()
            (item[where] if where else item)["extra"] = "x"
            cases.append(document(item))
        item = entry()
        item["requires"]["addons"][0]["extra"] = True
        cases.append(document(item))
        for doc in cases:
            with self.subTest(doc=doc):
                self.assertTrue(any("unknown" in p for p in self.problems(doc)))

    def test_missing_fields_are_refused(self) -> None:
        for field in ("name", "title", "version", "description", "requires", "source", "digest"):
            item = entry()
            del item[field]
            with self.subTest(field):
                self.assertTrue(self.problems(document(item)))
        for field in ("repo", "commit", "path"):
            item = entry()
            del item["source"][field]
            with self.subTest(field):
                self.assertTrue(self.problems(document(item)))

    def test_bad_values_are_refused(self) -> None:
        changes = {
            "name": ("name", "Demo Module"),
            "name-long": ("name", "a" * 65),
            "title": ("title", ""),
            "title-long": ("title", "t" * 41),
            "version": ("version", "1.0"),
            "description": ("description", "d" * 201),
            "description-newline": ("description", "two\nlines"),
            "digest": ("digest", "sha256:" + "A" * 64),
            "digest-short": ("digest", "sha256:abc"),
        }
        for label, (field, value) in changes.items():
            item = entry()
            item[field] = value
            with self.subTest(label):
                self.assertTrue(self.problems(document(item)))

    def test_bad_sources_are_refused(self) -> None:
        values = {
            "commit": ["abc", "G" * 40, "1" * 39],
            "path": ["/deploy/papaia-addon", "deploy/../etc", "", "deploy\\addon", "-x"],
            "repo": [
                "http://github.com/example/demo.git",
                "https://user:pw@github.com/example/demo.git",
                "https://github.com/example/demo.git?x=1",
                "file:///tmp/demo",
                "https:///demo.git",
                "-c core.fsmonitor=x",
            ],
        }
        for field, bad in values.items():
            for value in bad:
                item = entry()
                item["source"][field] = value
                with self.subTest(field=field, value=value):
                    self.assertTrue(self.problems(document(item)))

    def test_bad_requires_are_refused(self) -> None:
        bad = [
            {"addon_api": [], "papaia_compat": ">=1.10.0"},
            {"addon_api": ["12"], "papaia_compat": ">=1.10.0"},
            {"addon_api": [True], "papaia_compat": ">=1.10.0"},
            {"addon_api": [12], "papaia_compat": "1.10"},
            {"addon_api": [12], "papaia_compat": ">=1.10.0", "addons": [{"name": "Bad Name", "optional": True}]},
            {"addon_api": [12], "papaia_compat": ">=1.10.0", "addons": [{"name": "x", "optional": "yes"}]},
            {"addon_api": [12], "papaia_compat": ">=1.10.0", "addons": "papaia-data"},
        ]
        for requires in bad:
            item = entry()
            item["requires"] = requires
            with self.subTest(requires=requires):
                self.assertTrue(self.problems(document(item)))

    def test_duplicate_names_are_refused(self) -> None:
        self.assertTrue(any("twice" in p for p in self.problems(document(entry(), entry()))))

    def test_name_of_a_local_add_on_is_refused(self) -> None:
        problems = self.problems(document(entry()), {"demo-module"})
        self.assertTrue(any("directory" in p for p in problems))

    def test_not_a_mapping_is_refused(self) -> None:
        for doc in (None, [], "text", {"schema": catalogue.SCHEMA, "entries": {}}):
            with self.subTest(doc=doc):
                self.assertTrue(self.problems(doc))

    def test_load_refuses_oversized_and_non_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.yaml"
            big.write_text("#" * (catalogue.MAX_BYTES + 1), encoding="utf-8")
            with self.assertRaises(catalogue.CatalogueError):
                catalogue.load(big)
            with self.assertRaises(catalogue.CatalogueError):
                catalogue.load(Path(tmp))
            with self.assertRaises(catalogue.CatalogueError):
                catalogue.load(Path(tmp) / "missing.yaml")

    def test_check_reports_a_missing_catalogue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(catalogue.check(Path(tmp)))

    def test_check_reports_a_file_out_of_canonical_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = catalogue.render(document(entry()))
            (root / catalogue.CATALOGUE).write_text(text + "\n", encoding="utf-8")
            self.assertTrue(any("canonical" in p for p in catalogue.check(root)))
            (root / catalogue.CATALOGUE).write_text(text, encoding="utf-8")
            self.assertEqual(catalogue.check(root), [])

    def test_render_round_trips(self) -> None:
        doc = document(entry())
        self.assertEqual(yaml.safe_load(catalogue.render(doc)), doc)


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo, self.commit = make_module_repo(self.base)

    def test_files_are_the_bundle_only_with_modes(self) -> None:
        files = catalogue.bundle_files(self.repo, self.commit, "deploy/papaia-addon")
        paths = {f.path: f.mode for f in files}
        self.assertEqual(
            paths,
            {
                "docker-compose.yml": "100644",
                "integration/client.json": "100644",
                "papaia-app.yaml": "100644",
                "start.sh": "100755",
            },
        )

    def test_digest_is_stable_and_follows_content_and_mode(self) -> None:
        first = catalogue.bundle_digest(catalogue.bundle_files(self.repo, self.commit, "deploy/papaia-addon"))
        again = catalogue.bundle_digest(catalogue.bundle_files(self.repo, self.commit, "deploy/papaia-addon"))
        self.assertEqual(first, again)
        self.assertRegex(first, r"\Asha256:[0-9a-f]{64}\Z")

        (self.repo / "README.md").write_text("changed outside\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "outside")
        outside = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(
            catalogue.bundle_digest(catalogue.bundle_files(self.repo, outside, "deploy/papaia-addon")), first
        )

        (self.repo / "deploy/papaia-addon/start.sh").chmod(0o644)
        git(self.repo, "commit", "-qam", "mode")
        mode = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(
            catalogue.bundle_digest(catalogue.bundle_files(self.repo, mode, "deploy/papaia-addon")), first
        )

        (self.repo / "deploy/papaia-addon/docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "content")
        content = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(
            catalogue.bundle_digest(catalogue.bundle_files(self.repo, content, "deploy/papaia-addon")), first
        )

    def test_symbolic_links_are_refused(self) -> None:
        os.symlink("papaia-app.yaml", self.repo / "deploy/papaia-addon/link.yaml")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "link")
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.bundle_files(self.repo, git(self.repo, "rev-parse", "HEAD"), "deploy/papaia-addon")

    def test_unknown_commit_and_empty_path_are_refused(self) -> None:
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.bundle_files(self.repo, "f" * 40, "deploy/papaia-addon")
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.bundle_files(self.repo, self.commit, "deploy/missing")

    def test_describe_reads_the_manifest_at_the_commit(self) -> None:
        described = catalogue.describe(self.repo, self.commit, "deploy/papaia-addon")
        self.assertEqual(described["name"], "demo-module")
        self.assertEqual(described["version"], "0.3.1")
        self.assertEqual(described["description"], "Demo module for the catalogue tests")
        self.assertEqual(
            described["requires"],
            {
                "addon_api": [12],
                "papaia_compat": ">=1.10.0",
                "addons": [
                    {"name": "papaia-data", "optional": True},
                    {"name": "papaia-flow", "optional": False},
                ],
            },
        )
        self.assertRegex(described["digest"], r"\Asha256:[0-9a-f]{64}\Z")


class PinVerifyFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo, self.commit = make_module_repo(self.base)
        self.root = self.base / "addons"
        self.root.mkdir()
        (self.root / catalogue.CATALOGUE).write_text(catalogue.render(document()), encoding="utf-8")

    def pin(self, *extra: str) -> int:
        return catalogue.main([
            "--root", str(self.root), "pin", "demo-module", "--repo", str(self.repo),
            "--url", "https://github.com/example/demo-module.git", "--title", "Demo", *extra,
        ])

    def pinned(self) -> dict:
        doc = catalogue.load(self.root / catalogue.CATALOGUE)
        return next(i for i in doc["entries"] if i["name"] == "demo-module")

    def test_pin_writes_an_entry_that_verifies(self) -> None:
        self.assertEqual(self.pin(), 0)
        item = self.pinned()
        self.assertEqual(item["source"]["commit"], self.commit)
        self.assertEqual(item["version"], "0.3.1")
        self.assertEqual(catalogue.check(self.root), [])
        self.assertEqual(catalogue.verify(item, self.repo), [])

    def test_pin_refuses_a_bundle_named_differently(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = catalogue.main([
                "--root", str(self.root), "pin", "other-module", "--repo", str(self.repo),
                "--url", "https://github.com/example/demo-module.git", "--title", "Other",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(catalogue.load(self.root / catalogue.CATALOGUE)["entries"], [])

    def test_verify_reports_every_difference(self) -> None:
        self.assertEqual(self.pin(), 0)
        item = copy.deepcopy(self.pinned())
        item["version"] = "0.3.2"
        item["digest"] = "sha256:" + "0" * 64
        item["requires"]["addon_api"] = [11]
        problems = catalogue.verify(item, self.repo)
        self.assertEqual(len(problems), 3, problems)

    def test_verify_reports_an_unknown_commit(self) -> None:
        self.assertEqual(self.pin(), 0)
        item = copy.deepcopy(self.pinned())
        item["source"]["commit"] = "e" * 40
        self.assertTrue(catalogue.verify(item, self.repo))

    def test_verify_command(self) -> None:
        self.assertEqual(self.pin(), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = catalogue.main(["--root", str(self.root), "verify", "--repo", f"demo-module={self.repo}"])
        self.assertEqual(code, 0)
        self.assertIn("demo-module", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                catalogue.main(["--root", str(self.root), "verify", "--repo", f"unknown={self.repo}"]), 1
            )

    def test_fetch_copies_the_bundle_with_modes(self) -> None:
        self.assertEqual(self.pin(), 0)
        target = self.base / "copy"
        with contextlib.redirect_stdout(io.StringIO()):
            code = catalogue.main([
                "--root", str(self.root), "fetch", "demo-module", "--repo", str(self.repo), "--into", str(target),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(
            (target / "papaia-app.yaml").read_text(encoding="utf-8"), MANIFEST
        )
        self.assertTrue(os.access(target / "start.sh", os.X_OK))
        self.assertFalse(os.access(target / "docker-compose.yml", os.X_OK))
        self.assertFalse((target / "README.md").exists())

    def test_fetch_refuses_a_non_empty_target(self) -> None:
        self.assertEqual(self.pin(), 0)
        target = self.base / "copy"
        target.mkdir()
        (target / "keep").write_text("x", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = catalogue.main([
                "--root", str(self.root), "fetch", "demo-module", "--repo", str(self.repo), "--into", str(target),
            ])
        self.assertEqual(code, 1)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["keep"])

    def test_fetch_writes_nothing_when_the_digest_differs(self) -> None:
        self.assertEqual(self.pin(), 0)
        doc = catalogue.load(self.root / catalogue.CATALOGUE)
        doc["entries"][0]["digest"] = "sha256:" + "0" * 64
        (self.root / catalogue.CATALOGUE).write_text(catalogue.render(doc), encoding="utf-8")
        target = self.base / "copy"
        with contextlib.redirect_stderr(io.StringIO()):
            code = catalogue.main([
                "--root", str(self.root), "fetch", "demo-module", "--repo", str(self.repo), "--into", str(target),
            ])
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())

    def test_list_shows_version_description_and_prerequisites(self) -> None:
        self.assertEqual(self.pin(), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(catalogue.main(["--root", str(self.root), "list"]), 0)
        text = out.getvalue()
        for part in ("demo-module", "0.3.1", "Demo module for the catalogue tests", "addon_api 12",
                     ">=1.10.0", "papaia-data (optional)", "papaia-flow"):
            self.assertIn(part, text)

    def test_matrix_compares_versions_with_the_core_suite(self) -> None:
        self.assertEqual(self.pin(), 0)
        core = self.base / "core"
        (core / "compat").mkdir(parents=True)
        (core / "compat" / "suite.yaml").write_text(SUITE, encoding="utf-8")
        args = ["--root", str(self.root), "matrix", "--core", str(core), "--suite", "1.10.0"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(catalogue.main(args), 0)

        (core / "compat" / "suite.yaml").write_text(
            SUITE.replace('">=0.3.0,<0.4.0"', '">=0.4.0,<0.5.0"'), encoding="utf-8"
        )
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(catalogue.main(args), 1)

        (core / "compat" / "suite.yaml").write_text(SUITE.replace("current: 14", "current: 11"), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(catalogue.main(args), 1)

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(catalogue.main([*args[:-1], "9.9.9"]), 1)


if __name__ == "__main__":
    unittest.main()
