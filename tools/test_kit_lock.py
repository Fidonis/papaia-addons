"""Tests for the pinned CI tool set (kit.lock, tools/ci/uv.lock)."""

from __future__ import annotations

import hashlib
import re
import tomllib
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_LOCK = REPO_ROOT / "kit.lock"
CI_PROJECT = REPO_ROOT / "tools" / "ci"
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_packages() -> dict[str, dict]:
    lock = tomllib.loads((CI_PROJECT / "uv.lock").read_text(encoding="utf-8"))
    return {normalise(p["name"]): p for p in lock["package"]}


class KitLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = tomllib.loads(KIT_LOCK.read_text(encoding="utf-8"))

    def test_lock_has_only_known_fields(self) -> None:
        self.assertEqual(set(self.lock), {"version", "commit", "files"})
        self.assertRegex(self.lock["version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(self.lock["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(len(self.lock["files"]), 1)
        self.assertEqual(set(self.lock["files"][0]), {"path", "sha256"})

    def test_vendored_wheel_matches_the_lock(self) -> None:
        version = self.lock["version"]
        entry = self.lock["files"][0]
        expected = f"vendor/papaia-kit/{version}/papaia_kit-{version}-py3-none-any.whl"
        self.assertEqual(entry["path"], expected)
        data = (REPO_ROOT / entry["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])

    def test_license_files_ship_next_to_the_wheel(self) -> None:
        folder = REPO_ROOT / "vendor" / "papaia-kit" / self.lock["version"]
        self.assertTrue((folder / "LICENSE").is_file())
        self.assertTrue((folder / "NOTICE").is_file())

    def test_every_runtime_dependency_of_the_kit_is_pinned(self) -> None:
        with zipfile.ZipFile(REPO_ROOT / self.lock["files"][0]["path"]) as wheel:
            metadata = next(
                wheel.read(name).decode()
                for name in wheel.namelist()
                if name.endswith(".dist-info/METADATA")
            )
        needed = {
            normalise(re.split(r"[\s<>=!~;\[(]", line.split(":", 1)[1].strip(), maxsplit=1)[0])
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist:") and "extra ==" not in line
        }
        self.assertTrue(needed)
        self.assertLessEqual(needed, set(locked_packages()))

    def test_ci_lock_pins_the_vendored_wheel(self) -> None:
        kit = locked_packages()["papaia-kit"]
        self.assertEqual(kit["version"], self.lock["version"])
        self.assertEqual(kit["source"], {"path": "../../" + self.lock["files"][0]["path"]})
        self.assertEqual(
            [w["hash"] for w in kit["wheels"]], ["sha256:" + self.lock["files"][0]["sha256"]]
        )


class CiLockTest(unittest.TestCase):
    def test_every_package_is_pinned_by_version_and_hash(self) -> None:
        packages = locked_packages()
        self.assertTrue(packages)
        for name, package in packages.items():
            if package.get("source", {}).get("virtual"):
                continue  # the tool project itself
            with self.subTest(package=name):
                self.assertRegex(package["version"], r"^\d")
                files = package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else [])
                self.assertTrue(files)
                for item in files:
                    self.assertRegex(item["hash"], HASH)

    def test_ci_tools_are_in_the_lock(self) -> None:
        self.assertLessEqual({"pyyaml", "ruff", "yamllint", "papaia-kit"}, set(locked_packages()))


if __name__ == "__main__":
    unittest.main()
