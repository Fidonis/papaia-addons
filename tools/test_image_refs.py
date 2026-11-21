"""Tests for tools/image_refs.py; run with `python -m unittest discover -s tools`."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import image_refs

REPO_ROOT = Path(__file__).resolve().parent.parent
DIGEST = "sha256:" + "0123456789abcdef" * 4


def compose(*refs: str) -> str:
    lines = ["services:"]
    for i, ref in enumerate(refs):
        lines += [f"  svc{i}:", f"    image: ${{SVC{i}_IMAGE:-{ref}}}"]
    return "\n".join(lines) + "\n"


class PinRuleTest(unittest.TestCase):
    def test_tag_and_digest_pass(self) -> None:
        for ref in (
            f"ghcr.io/fidonis/papaia-mediawiki:1.43.11-1.0.0@{DIGEST}",
            f"docker.io/library/postgres:18.7@{DIGEST}",
            f"docker.io/valkey/valkey:9.2.0-alpine@{DIGEST}",
            f"docker.io/qdrant/qdrant:v1.19.0@{DIGEST}",
            f"docker.io/apache/tika:3.2.3.0@{DIGEST}",
            f"registry.example:5000/team/app:1.2.3@{DIGEST}",
        ):
            with self.subTest(ref=ref):
                self.assertIsNone(image_refs.pin_problem(ref))

    def test_tag_without_digest_fails(self) -> None:
        self.assertIn("digest", image_refs.pin_problem("docker.io/library/postgres:18.7") or "")

    def test_digest_without_tag_fails(self) -> None:
        self.assertIn("tag", image_refs.pin_problem(f"docker.io/library/postgres@{DIGEST}") or "")

    def test_moving_tags_fail(self) -> None:
        for tag in ("latest", "dev", "main", "master", "edge", "nightly", "stable", "18", "9-alpine"):
            with self.subTest(tag=tag):
                self.assertIsNotNone(image_refs.pin_problem(f"docker.io/library/app:{tag}@{DIGEST}"))

    def test_branch_like_tag_fails(self) -> None:
        self.assertIsNotNone(
            image_refs.pin_problem(f"ghcr.io/fidonis/app:1.2.3-dev@{DIGEST}")
        )
        self.assertIsNotNone(
            image_refs.pin_problem(f"ghcr.io/fidonis/app:feature-x@{DIGEST}")
        )

    def test_malformed_digest_fails(self) -> None:
        for digest in ("sha256:abc", "sha256:" + "G" * 64, "sha512:" + "a" * 128, "sha256:" + "A" * 64):
            with self.subTest(digest=digest):
                self.assertIsNotNone(
                    image_refs.pin_problem(f"docker.io/library/postgres:18.7@{digest}")
                )

    def test_major_minor_tag_of_a_patch_level_upstream_fails(self) -> None:
        self.assertIn(
            "floats", image_refs.pin_problem(f"docker.io/gotenberg/gotenberg:8.34@{DIGEST}") or ""
        )

    def test_variable_inside_the_reference_fails(self) -> None:
        self.assertIsNotNone(image_refs.pin_problem(f"docker.io/library/postgres:${{TAG}}@{DIGEST}"))


class ComposeCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def addon(self, name: str, text: str) -> None:
        path = self.root / name
        path.mkdir()
        (path / "papaia-app.yaml").write_text(f"name: {name}\nversion: 1.0.0\n", encoding="utf-8")
        (path / "docker-compose.yml").write_text(text, encoding="utf-8")

    def run_check(self) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = image_refs.main(["--root", str(self.root), "check"])
        return code, err.getvalue()

    def test_pinned_compose_passes(self) -> None:
        self.addon("demo", compose(f"docker.io/library/postgres:18.7@{DIGEST}"))
        self.assertEqual(self.run_check(), (0, ""))

    def test_unpinned_default_fails_and_names_the_variable(self) -> None:
        self.addon("demo", compose(f"docker.io/library/postgres:18.7@{DIGEST}", "docker.io/library/redis:8"))
        code, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("demo", err)
        self.assertIn("SVC1_IMAGE", err)

    def test_image_without_variable_fails(self) -> None:
        self.addon("demo", "services:\n  app:\n    image: docker.io/library/redis:8.0.0\n")
        code, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("redis", err)

    def test_addon_without_compose_file_passes(self) -> None:
        path = self.root / "empty"
        path.mkdir()
        (path / "papaia-app.yaml").write_text("name: empty\nversion: 1.0.0\n", encoding="utf-8")
        self.assertEqual(self.run_check(), (0, ""))

    def test_excluded_addon_is_not_read(self) -> None:
        for name in image_refs.EXCLUDED:
            self.addon(name, compose("docker.io/library/app:latest"))
        self.assertEqual(self.run_check(), (0, ""))


class ListTest(ComposeCheckTest):
    def run_list(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = image_refs.main(["--root", str(self.root), "list"])
        return code, out.getvalue(), err.getvalue()

    def test_lists_each_pinned_reference_once_as_json(self) -> None:
        postgres = f"docker.io/library/postgres:18.7@{DIGEST}"
        tika = f"docker.io/apache/tika:3.2.3.0@{DIGEST}"
        self.addon("one", compose(tika, postgres))
        self.addon("two", compose(postgres))
        code, out, _ = self.run_list()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), sorted([postgres, tika]))

    def test_refuses_to_list_an_unpinned_reference(self) -> None:
        self.addon("demo", compose("docker.io/library/redis:8"))
        code, out, err = self.run_list()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("redis", err)

    def test_excluded_addon_is_not_listed(self) -> None:
        for name in image_refs.EXCLUDED:
            self.addon(name, compose(f"docker.io/library/app:1.0.0@{DIGEST}"))
        code, out, _ = self.run_list()
        self.assertEqual((code, json.loads(out)), (0, []))


class RepositoryTest(unittest.TestCase):
    def test_every_addon_image_is_pinned_by_tag_and_digest(self) -> None:
        problems = []
        for path in image_refs.checked_addons(REPO_ROOT):
            problems += [f"{path.name}: {p}" for p in image_refs.check_addon(path)]
        self.assertEqual(problems, [])

    def test_check_covers_the_delivered_addons(self) -> None:
        names = {p.name for p in image_refs.checked_addons(REPO_ROOT)}
        self.assertLessEqual(
            {"mediawiki", "paperless", "paperless-connect", "qdrant", "qdrant-connect", "qdrant-ingest"},
            names,
        )


if __name__ == "__main__":
    unittest.main()
