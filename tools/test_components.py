"""Tests for tools/components.py; run with `python -m unittest discover -s tools`."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import components

COMPOSE = """\
services:
  app:
    image: ${DEMO_IMAGE:-docker.io/example/demo:1.2.3}
  db:
    image: ${DEMO_DB_IMAGE:-docker.io/library/postgres:18}
"""

MANIFEST = """\
name: demo
version: 1.0.0
components:
  - id: demo
    name: Demo
    role: primary
    kind: upstream
    image_var: DEMO_IMAGE
    version: "1.2.3"
    source: https://example.com/demo
  - id: postgres
    name: PostgreSQL
    role: sidecar
    kind: upstream
    image_var: DEMO_DB_IMAGE
    version: "18"
    source: https://www.postgresql.org
    pinned: false
"""

ENV = "# DEMO_IMAGE=docker.io/example/demo:1.2.3\n# DEMO_DB_IMAGE=docker.io/library/postgres:18\n"


def write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def make_addon(root: Path, manifest: str = MANIFEST, compose: str = COMPOSE) -> Path:
    addon = root / "demo"
    addon.mkdir()
    write(addon / components.MANIFEST, manifest)
    write(addon / components.COMPOSE, compose)
    write(addon / components.ENV_EXAMPLE, ENV)
    listed = components.load_manifest(addon).get("components") or []
    write(addon / components.README, "# demo\n\n" + components.render_block(listed) + "\n")
    return addon


class TagTests(unittest.TestCase):
    def test_image_tag(self) -> None:
        self.assertEqual(components.image_tag("ghcr.io/o/n:0.3.0"), "0.3.0")
        self.assertEqual(components.image_tag("localhost:5000/n:1.0.0@sha256:abc"), "1.0.0")
        self.assertEqual(components.image_tag("localhost:5000/n"), "latest")

    def test_exact_versus_floating(self) -> None:
        for tag in ("3.0.5", "v1.19.0", "1.43.11-1.0.0", "3.2.3.0", "v7.15.3"):
            self.assertTrue(components.is_exact(tag), tag)
        for tag in ("18", "8.34", "9-alpine", "1.31-alpine", "latest"):
            self.assertFalse(components.is_exact(tag), tag)


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_consistent_addon_passes(self) -> None:
        addon = make_addon(self.root)
        self.assertEqual(components.check_addon(addon), [])

    def test_compose_tag_change_is_reported_then_fixed(self) -> None:
        addon = make_addon(self.root)
        compose = addon / components.COMPOSE
        write(compose, compose.read_text(encoding="utf-8").replace("1.2.3", "1.3.0"))
        problems = components.check_addon(addon)
        self.assertTrue(any("version is `1.2.3`" in p for p in problems), problems)
        self.assertTrue(any(components.ENV_EXAMPLE in p for p in problems), problems)
        self.assertEqual(
            set(components.fix_addon(addon)),
            {components.ENV_EXAMPLE, components.MANIFEST, components.README},
        )
        self.assertEqual(components.check_addon(addon), [])
        self.assertIn('version: "1.3.0"', (addon / components.MANIFEST).read_text(encoding="utf-8"))

    def test_image_without_component_is_reported(self) -> None:
        extra = (
            COMPOSE + "  cache:\n    image: ${DEMO_CACHE_IMAGE:-docker.io/library/redis:7.4.1}\n"
        )
        addon = make_addon(self.root, compose=extra)
        self.assertTrue(
            any("DEMO_CACHE_IMAGE" in p and "no entry" in p for p in components.check_addon(addon))
        )

    def test_bare_image_is_reported(self) -> None:
        addon = make_addon(self.root, compose=COMPOSE + "  x:\n    image: redis:7\n")
        self.assertTrue(any("does not follow" in p for p in components.check_addon(addon)))

    def test_floating_tag_must_be_declared(self) -> None:
        addon = make_addon(self.root, manifest=MANIFEST.replace("    pinned: false\n", ""))
        self.assertTrue(any("floats" in p for p in components.check_addon(addon)))

    def test_primary_component_needs_an_exact_tag(self) -> None:
        compose = COMPOSE.replace("demo:1.2.3", "demo:1")
        manifest = MANIFEST.replace('version: "1.2.3"', 'version: "1"')
        addon = make_addon(self.root, manifest=manifest, compose=compose)
        problems = components.check_addon(addon)
        self.assertTrue(any("needs an exact tag" in p for p in problems), problems)

    def test_unquoted_numeric_version_is_reported(self) -> None:
        addon = make_addon(self.root, manifest=MANIFEST.replace('version: "18"', "version: 18"))
        self.assertTrue(any("quote the version" in p for p in components.check_addon(addon)))

    def test_readme_without_markers_is_reported(self) -> None:
        addon = make_addon(self.root)
        write(addon / components.README, "# demo\n")
        self.assertTrue(any("markers" in p for p in components.check_addon(addon)))

    def test_manifest_without_components_is_reported(self) -> None:
        addon = make_addon(self.root, manifest="name: demo\nversion: 1.0.0\n")
        self.assertEqual(len(components.check_addon(addon)), 1)


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        self.addon = make_addon(self.root)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "tag", "v1.0.0")

    def commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def bump_compose(self) -> None:
        compose = self.addon / components.COMPOSE
        write(compose, compose.read_text(encoding="utf-8").replace("1.2.3", "1.3.0"))
        components.fix_addon(self.addon)

    def test_untouched_addon_passes(self) -> None:
        self.assertEqual(components.release_check(self.root, "v1.0.0"), [])

    def test_change_without_bump_fails_and_bump_passes(self) -> None:
        self.bump_compose()
        self.commit("raise the demo image")
        problems = components.release_check(self.root, "v1.0.0")
        self.assertEqual(len(problems), 1, problems)
        manifest = self.addon / components.MANIFEST
        write(
            manifest,
            manifest.read_text(encoding="utf-8").replace("version: 1.0.0", "version: 1.0.1"),
        )
        self.commit("bump")
        self.assertEqual(components.release_check(self.root, "v1.0.0"), [])

    def test_bump_without_change_fails(self) -> None:
        manifest = self.addon / components.MANIFEST
        write(
            manifest,
            manifest.read_text(encoding="utf-8").replace("version: 1.0.0", "version: 1.1.0"),
        )
        self.commit("bump")
        self.assertEqual(len(components.release_check(self.root, "v1.0.0")), 1)

    def test_docs_only_change_needs_no_bump(self) -> None:
        write(self.addon / "notes.md", "more words\n")
        self.commit("docs")
        self.assertEqual(components.release_check(self.root, "v1.0.0"), [])

    def test_report_names_the_changed_component(self) -> None:
        self.bump_compose()
        manifest = self.addon / components.MANIFEST
        write(
            manifest,
            manifest.read_text(encoding="utf-8").replace("version: 1.0.0", "version: 1.0.1"),
        )
        self.commit("raise the demo image")
        table = components.report(self.root, "v1.0.0", everything=False)
        self.assertIn("| demo | 1.0.0 → 1.0.1 | Demo 1.2.3 → 1.3.0; PostgreSQL 18 |", table)

    def test_report_reads_compose_for_a_ref_before_components_existed(self) -> None:
        write(self.addon / components.MANIFEST, "name: demo\nversion: 1.0.0\n")
        self.commit("strip components")
        git(self.root, "tag", "v0.9.0")
        write(self.addon / components.MANIFEST, MANIFEST)
        self.commit("add components")
        table = components.report(self.root, "v0.9.0", everything=True)
        self.assertIn("Demo 1.2.3; PostgreSQL 18", table)


if __name__ == "__main__":
    unittest.main()
