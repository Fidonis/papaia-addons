"""Tests for the pinned CI tool set (kit.lock, tools/ci/uv.lock).

The repository vendors the final papaia-kit 0.1.0, the kit's first release.
An earlier build carried the same version number, so only the release record
tells them apart: kit commit, SOURCE_DATE_EPOCH and the sha256 of the wheel in
the release's SHA256SUMS are pinned here, so a different build of the same
version number cannot slip in.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_LOCK = REPO_ROOT / "kit.lock"
CI_PROJECT = REPO_ROOT / "tools" / "ci"
VENDOR = REPO_ROOT / "vendor" / "papaia-kit"
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")

# Release record of the final papaia-kit 0.1.0.
RELEASE_VERSION = "0.1.0"
RELEASE_COMMIT = "849e344064472d94a5c934170f58162ef296c94c"
RELEASE_EPOCH = 1795592298
RELEASE_WHEEL = "papaia_kit-0.1.0-py3-none-any.whl"
RELEASE_WHEEL_SHA256 = "7ecccf90d47cce795a5b07712f9f4806839dcd0211aba71bf2f16eb6e7b1211e"

# Kit version numbers that were never published: major 0, minor 1 with patch 1
# to 9 (local cuts, folded into 0.1.0), minor 2 with any patch, and both minor
# series written with "x". Built from parts so this file names none of them.
_ZERO = "0"
UNPUBLISHED = re.compile(
    rf"(?<![\w.]){_ZERO}\.(?:1\.[1-9]|2\.[0-9]|[12]\.x)(?![\w-]|\.\d)"
)
# Kit commits of builds the final 0.1.0 replaces: the earlier 0.1.0 build and
# the two local cuts. Abbreviated to seven characters and built from parts, so
# this file names none of them; any longer spelling starts with the prefix.
SUPERSEDED_COMMITS = re.compile(
    r"(?<![0-9a-f])(?:" + "|".join(("0ce" + "4127", "14b" + "9c83", "fd6" + "9b66")) + r")",
    re.IGNORECASE,
)
KIT_WORD = re.compile(r"kit", re.IGNORECASE)
KIT_PATH = re.compile(r"papaia[-_]kit[-/](?P<version>\d+\.\d+\.\d+)")

# History lines stay in changelogs. n8n/ is not part of the integrated delivery
# and is maintained outside the repository checks (as in tools/image_refs.py).
SKIPPED_NAMES = {"CHANGELOG.md"}
SKIPPED_TOP = {"n8n"}


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_packages() -> dict[str, dict]:
    lock = tomllib.loads((CI_PROJECT / "uv.lock").read_text(encoding="utf-8"))
    return {normalise(p["name"]): p for p in lock["package"]}


def wheel_member(suffix: str) -> bytes:
    with zipfile.ZipFile(REPO_ROOT / KitLockTest.wheel_path()) as wheel:
        return next(wheel.read(n) for n in wheel.namelist() if n.endswith(suffix))


def tracked_text_files() -> list[Path]:
    names = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode().split("\0")
    files = []
    for name in filter(None, names):
        path = Path(name)
        if path.parts[0] in SKIPPED_TOP or path.name in SKIPPED_NAMES:
            continue
        files.append(path)
    return files


def kit_version_findings(text: str) -> list[str]:
    """Lines that name a kit version other than the vendored release."""
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if (KIT_WORD.search(line) and UNPUBLISHED.search(line)) or SUPERSEDED_COMMITS.search(line):
            findings.append(f"{number}: {line.strip()}")
            continue
        for match in KIT_PATH.finditer(line):
            if match["version"] != RELEASE_VERSION:
                findings.append(f"{number}: {line.strip()}")
                break
    return findings


class KitLockTest(unittest.TestCase):
    @staticmethod
    def wheel_path() -> str:
        return f"vendor/papaia-kit/{RELEASE_VERSION}/{RELEASE_WHEEL}"

    def setUp(self) -> None:
        self.lock = tomllib.loads(KIT_LOCK.read_text(encoding="utf-8"))

    def test_lock_has_only_known_fields(self) -> None:
        self.assertEqual(set(self.lock), {"version", "commit", "source_date_epoch", "files"})
        self.assertEqual(len(self.lock["files"]), 1)
        self.assertEqual(set(self.lock["files"][0]), {"path", "sha256"})

    def test_lock_names_the_kit_release_record(self) -> None:
        self.assertEqual(self.lock["version"], RELEASE_VERSION)
        self.assertEqual(self.lock["commit"], RELEASE_COMMIT)
        self.assertEqual(self.lock["source_date_epoch"], RELEASE_EPOCH)
        self.assertEqual(
            self.lock["files"],
            [{"path": self.wheel_path(), "sha256": RELEASE_WHEEL_SHA256}],
        )

    def test_vendored_wheel_matches_the_lock(self) -> None:
        entry = self.lock["files"][0]
        data = (REPO_ROOT / entry["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])

    def test_only_the_release_is_vendored(self) -> None:
        self.assertEqual(sorted(p.name for p in VENDOR.iterdir()), [RELEASE_VERSION])
        self.assertEqual(
            sorted(p.name for p in (VENDOR / RELEASE_VERSION).iterdir()),
            ["LICENSE", RELEASE_WHEEL],
        )

    def test_wheel_is_the_mit_licensed_release(self) -> None:
        metadata = wheel_member(".dist-info/METADATA").decode()
        self.assertIn(f"\nVersion: {RELEASE_VERSION}\n", metadata)
        self.assertIn("\nLicense-Expression: MIT\n", metadata)
        # The license text next to the wheel is the one the wheel ships.
        self.assertEqual(
            (VENDOR / RELEASE_VERSION / "LICENSE").read_bytes(),
            wheel_member(".dist-info/licenses/LICENSE"),
        )

    def test_every_runtime_dependency_of_the_kit_is_pinned(self) -> None:
        metadata = wheel_member(".dist-info/METADATA").decode()
        needed = {
            normalise(re.split(r"[\s<>=!~;\[(]", line.split(":", 1)[1].strip(), maxsplit=1)[0])
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist:") and "extra ==" not in line
        }
        self.assertTrue(needed)
        self.assertLessEqual(needed, set(locked_packages()))

    def test_ci_project_pins_the_vendored_wheel(self) -> None:
        project = tomllib.loads((CI_PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn(f"papaia-kit=={RELEASE_VERSION}", project["dependency-groups"]["ci"])
        self.assertEqual(
            project["tool"]["uv"]["sources"]["papaia-kit"],
            {"path": "../../" + self.wheel_path()},
        )

    def test_ci_lock_pins_the_vendored_wheel(self) -> None:
        kit = locked_packages()["papaia-kit"]
        self.assertEqual(kit["version"], self.lock["version"])
        self.assertEqual(kit["source"], {"path": "../../" + self.lock["files"][0]["path"]})
        self.assertEqual(
            [w["hash"] for w in kit["wheels"]], ["sha256:" + self.lock["files"][0]["sha256"]]
        )


class KitVersionNamesTest(unittest.TestCase):
    def test_patterns(self) -> None:
        unpublished = [f"{_ZERO}.1.{n}" for n in range(1, 10)]
        unpublished += [f"{_ZERO}.2.{n}" for n in range(10)] + [f"{_ZERO}.1.x", f"{_ZERO}.2.x"]
        for version in unpublished:
            with self.subTest(version=version):
                self.assertTrue(kit_version_findings(f"papaia-kit {version}"))
                self.assertTrue(kit_version_findings(f"see vendor/papaia-kit/{version}/"))
        for commit in ("0ce" + "4127", "14b" + "9c83", "fd6" + "9b66"):
            for spelling in (commit, commit.upper(), commit + "0" * 33):
                with self.subTest(commit=spelling):
                    self.assertTrue(kit_version_findings(f"kit commit {spelling}"))
                    self.assertTrue(kit_version_findings(f"built from {spelling}"))
        for line in (
            "papaia-kit 0.1.0",
            f"commit = \"{RELEASE_COMMIT}\"",
            "paperless-mcp-rbac 0.1.1",
            "qdrant-ingest 0.1.1 and paperless-mcp-rbac 0.1.2",
            "a" + "0ce" + "4127",
            f"vendor/papaia-kit/{RELEASE_VERSION}/{RELEASE_WHEEL}",
            "papaia-kit 1.2.0",
            "papaia-kit 10.1.1",
            "paperless-mcp-rbac 0.2.0-rc.1",
            f"tool {_ZERO}.2.0 next to nothing",
        ):
            with self.subTest(line=line):
                self.assertEqual(kit_version_findings(line), [])

    def test_no_file_names_another_kit_version(self) -> None:
        findings = []
        for path in tracked_text_files():
            try:
                text = (REPO_ROOT / path).read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError):
                continue  # binary files (the wheel) or a deletion not yet staged
            findings += [f"{path}:{line}" for line in kit_version_findings(text)]
        self.assertEqual(findings, [])


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
