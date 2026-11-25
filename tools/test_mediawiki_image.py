"""Checks of the Debian security upgrades in mediawiki/image/Dockerfile.

Run with `python -m unittest discover -s tools`.

The wiki image raises single Debian packages that its base digest does not
carry in a fixed version yet. These tests hold that list to what the image's
security conditions allow: exact versions only, each one named next to the
advisory it fixes, applied with `--only-upgrade` (nothing new is installed by
it), no blanket upgrade and no switch that turns apt's signature checks off.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "mediawiki" / "image" / "Dockerfile"

ARG_NAME = "DEBIAN_SECURITY_UPGRADES"
# Debian package names and versions (Debian Policy 5.6.1 and 5.6.12).
PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
VERSION = re.compile(r"^(?:[0-9]+:)?[0-9][A-Za-z0-9.+~-]*$")
ADVISORY = re.compile(r"\b(?:CVE-[0-9]{4}-[0-9]{4,}|DSA-[0-9]{4,}-[0-9]+)\b")
# Options that make apt accept unsigned or unverifiable packages.
INSECURE_APT = re.compile(
    r"--allow-unauthenticated|--allow-insecure-repositories|--allow-downgrades"
    r"|AllowUnauthenticated|AllowInsecureRepositories|AllowDowngradeToInsecureRepositories"
    r"|trusted=yes|--force-yes",
    re.IGNORECASE,
)
BLANKET_UPGRADE = re.compile(r"apt(?:-get)?\s+(?:-\S+\s+)*(?:dist-|full-)?upgrade\b")


def security_upgrades(text: str) -> list[tuple[str, str]] | None:
    """The `name=version` pins of the ARG, or None when the image has none."""
    match = re.search(rf'^ARG {ARG_NAME}="((?:[^"\\]|\\\n)*)"$', text, re.MULTILINE)
    if match is None:
        return None
    pins = []
    for item in match.group(1).replace("\\\n", " ").split():
        name, sep, version = item.partition("=")
        pins.append((name, version if sep else ""))
    return pins


def advisory_lines(text: str) -> dict[str, list[str]]:
    """Comment lines `#   <package>  <version>  <advisory ids>`, by package."""
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^#\s+([a-z0-9][a-z0-9+.-]+)\s+(\S+)\s+(.*)$", line)
        if match and ADVISORY.search(match.group(3)):
            found.setdefault(match.group(1), []).append(match.group(2))
    return found


class DebianSecurityUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DOCKERFILE.read_text(encoding="utf-8")
        self.pins = security_upgrades(self.text)

    def test_pins_are_exact_versions_and_unique(self) -> None:
        if self.pins is None:
            self.skipTest(f"{ARG_NAME} is not set: the base digest carries every fix")
        self.assertTrue(self.pins, f"{ARG_NAME} is empty; drop the ARG and its step instead")
        names = [name for name, _ in self.pins]
        self.assertEqual(len(names), len(set(names)), f"package pinned twice: {names}")
        for name, version in self.pins:
            with self.subTest(package=name):
                self.assertRegex(name, PACKAGE)
                self.assertRegex(version, VERSION, f"{name} needs an exact version (name=version)")

    def test_every_pin_names_its_advisory(self) -> None:
        if self.pins is None:
            self.skipTest(f"{ARG_NAME} is not set: the base digest carries every fix")
        documented = advisory_lines(self.text)
        for name, version in self.pins:
            with self.subTest(package=name):
                self.assertIn(
                    version,
                    documented.get(name, []),
                    f"no comment line '#   {name}  {version}  CVE-...' above {ARG_NAME}",
                )
        pinned = dict(self.pins)
        for name, versions in documented.items():
            with self.subTest(comment=name):
                self.assertIn(name, pinned, f"advisory comment for {name}, which is not pinned")
                self.assertEqual(versions, [pinned[name]])

    def test_upgrades_are_applied_only_as_upgrades(self) -> None:
        if self.pins is None:
            self.assertNotIn(ARG_NAME, self.text)
            return
        self.assertRegex(
            self.text,
            r"apt-get install -y --no-install-recommends --only-upgrade \"\$\{upgrades\[@\]\}\"",
        )

    def test_no_blanket_upgrade_and_signature_checks_stay_on(self) -> None:
        code = "\n".join(
            line for line in self.text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIsNone(BLANKET_UPGRADE.search(code), "blanket apt upgrade in the image")
        self.assertIsNone(INSECURE_APT.search(code), "apt signature checks weakened")


class ParserTests(unittest.TestCase):
    """The helpers above, on seeded Dockerfile text."""

    SAMPLE = (
        "# Debian security updates:\n"
        "#\n"
        "#   libzstd1  1.5.7+dfsg-1+deb13u1  CVE-2026-124851\n"
        "#   zlib1g    1:1.3.dfsg+really1.3.1-1+deb13u1  CVE-2026-85091\n"
        f'ARG {ARG_NAME}="\\\n'
        "libzstd1=1.5.7+dfsg-1+deb13u1 \\\n"
        'zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1"\n'
    )

    def test_reads_pins_and_advisories(self) -> None:
        self.assertEqual(
            security_upgrades(self.SAMPLE),
            [
                ("libzstd1", "1.5.7+dfsg-1+deb13u1"),
                ("zlib1g", "1:1.3.dfsg+really1.3.1-1+deb13u1"),
            ],
        )
        self.assertEqual(
            advisory_lines(self.SAMPLE),
            {
                "libzstd1": ["1.5.7+dfsg-1+deb13u1"],
                "zlib1g": ["1:1.3.dfsg+really1.3.1-1+deb13u1"],
            },
        )

    def test_unpinned_entry_has_no_version(self) -> None:
        text = f'ARG {ARG_NAME}="libzstd1"\n'
        self.assertEqual(security_upgrades(text), [("libzstd1", "")])
        self.assertNotRegex("", VERSION)

    def test_missing_arg(self) -> None:
        self.assertIsNone(security_upgrades("FROM scratch\n"))

    def test_comment_without_advisory_is_ignored(self) -> None:
        self.assertEqual(advisory_lines("#   libzstd1  1.5.7  fixed upstream\n"), {})

    def test_insecure_apt_and_blanket_upgrade_are_detected(self) -> None:
        for text in (
            "apt-get install -y --allow-unauthenticated foo",
            "echo 'deb [trusted=yes] http://x trixie main'",
            "apt-get -o Acquire::AllowInsecureRepositories=true update",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(INSECURE_APT.search(text))
        for text in ("apt-get upgrade -y", "apt-get -y dist-upgrade", "apt full-upgrade"):
            with self.subTest(text=text):
                self.assertIsNotNone(BLANKET_UPGRADE.search(text))
        self.assertIsNone(BLANKET_UPGRADE.search("apt-get install -y --only-upgrade foo=1"))


if __name__ == "__main__":
    unittest.main()
