"""Checks of the security upgrades in mediawiki/image/Dockerfile.

Run with `python -m unittest discover -s tools`.

The wiki image raises single Debian packages that its base digest does not
carry in a fixed version yet. These tests hold that list to what the image's
security conditions allow: exact versions only, each one named next to the
advisory it fixes, applied with `--only-upgrade` (nothing new is installed by
it), no blanket upgrade and no switch that turns apt's signature checks off.

The same holds for the PHP packages of the vendor directory of MediaWiki core
(/var/www/html/vendor): each upgrade is an entry of mediawiki/image/
core-vendor.lock with an exact version and the checksum of its dist archive,
named in the Dockerfile next to its advisory, and installed with Composer
without dev packages, scripts or plugins.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "mediawiki" / "image" / "Dockerfile"
CORE_VENDOR_LOCK = REPO_ROOT / "mediawiki" / "image" / "core-vendor.lock"
CONTENTS_CHECK = REPO_ROOT / "mediawiki" / "tests" / "image_contents.sh"
IMAGE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mediawiki-image.yml"

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


# Lowest version that carries the fix, per core vendor package with a finding.
CORE_VENDOR_FIXED = {
    "symfony/yaml": ((5, 4, 54), "CVE-2026-109413"),
}
COMPOSER_PACKAGE = re.compile(r"^[a-z0-9]([_.-]?[a-z0-9]+)*/[a-z0-9](([_.]|-{1,2})?[a-z0-9]+)*$")
STABLE_VERSION = re.compile(r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def core_vendor_lines(text: str) -> dict[str, list[tuple[str, str]]]:
    """Comment lines `#   <vendor>/<package>  <version>  <advisory ids>`, by package."""
    found: dict[str, list[tuple[str, str]]] = {}
    for line in text.splitlines():
        match = re.match(r"^#\s+([a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*)\s+(\S+)\s+(.*)$", line)
        if match and ADVISORY.search(match.group(3)):
            found.setdefault(match.group(1), []).append((match.group(2), match.group(3)))
    return found


def run_steps(text: str) -> list[str]:
    """The RUN instructions of a Dockerfile, continuation lines joined."""
    joined = re.sub(r"\\\n", " ", text)
    return [line for line in joined.splitlines() if line.startswith("RUN ")]


class CoreVendorPinTests(unittest.TestCase):
    """The committed pins of the MediaWiki core vendor directory."""

    def setUp(self) -> None:
        self.text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertTrue(CORE_VENDOR_LOCK.is_file(), f"{CORE_VENDOR_LOCK.name} is missing")
        self.lock = json.loads(CORE_VENDOR_LOCK.read_text(encoding="utf-8"))
        self.packages = {p["name"]: p for p in self.lock["packages"]}

    def test_fixed_packages_are_pinned_at_or_above_the_fix(self) -> None:
        for name, (minimum, advisory) in CORE_VENDOR_FIXED.items():
            with self.subTest(package=name):
                self.assertIn(name, self.packages, f"{name} ({advisory}) is not pinned")
                match = STABLE_VERSION.match(self.packages[name]["version"])
                self.assertIsNotNone(match, f"{name} needs an exact stable version")
                version = tuple(int(part) for part in match.groups())
                self.assertGreaterEqual(version, minimum, f"{name} {version} does not fix {advisory}")
                self.assertEqual(version[:2], minimum[:2], f"{name} leaves the {minimum[0]}.{minimum[1]} line")

    def test_every_pin_is_exact_with_a_checksummed_dist(self) -> None:
        self.assertEqual(len(self.packages), len(self.lock["packages"]), "package pinned twice")
        for name, package in self.packages.items():
            with self.subTest(package=name):
                self.assertRegex(name, COMPOSER_PACKAGE)
                self.assertRegex(package["version"], STABLE_VERSION)
                dist = package["dist"]
                self.assertEqual(dist["type"], "zip")
                self.assertRegex(dist["reference"], SHA)
                self.assertRegex(dist["shasum"], SHA, "the dist archive needs its sha1 checksum")
                self.assertTrue(dist["url"].startswith("https://"), dist["url"])
                self.assertTrue(dist["url"].endswith(dist["reference"]), "url and reference disagree")
                # Without a source, Composer cannot fall back to an unchecked checkout.
                self.assertNotIn("source", package)
                self.assertNotIn("scripts", package)
                self.assertNotIn("extra", package)

    def test_every_pin_names_its_advisory_in_the_dockerfile(self) -> None:
        documented = core_vendor_lines(self.text)
        for name, package in self.packages.items():
            with self.subTest(package=name):
                versions = [version for version, _ in documented.get(name, [])]
                self.assertEqual(
                    versions,
                    [package["version"]],
                    f"no comment line '#   {name}  {package['version']}  CVE-...' in the Dockerfile",
                )
                if name in CORE_VENDOR_FIXED:
                    self.assertIn(CORE_VENDOR_FIXED[name][1], documented[name][0][1])
        for name in documented:
            with self.subTest(comment=name):
                self.assertIn(name, self.packages, f"advisory comment for {name}, which is not pinned")

    def test_dockerfile_installs_the_pins_from_the_lock(self) -> None:
        steps = [step for step in run_steps(self.text) if "core-vendor.lock" in step]
        self.assertEqual(len(steps), 1, "one RUN step reads core-vendor.lock")
        step = steps[0]
        self.assertIn("--mount=type=bind,source=core-vendor.lock,", step)
        self.assertIn("repositories.packagist.org false", step)
        update = re.search(r"composer update ([^&;|]*)", step)
        self.assertIsNotNone(update, "the pins are installed with `composer update`")
        for flag in ("--no-dev", "--no-scripts", "--no-plugins", "--prefer-dist", "--working-dir=vendor"):
            self.assertIn(flag, update.group(1))
        self.assertNotRegex(update.group(1), r"(?<!\S)(-w|-W|--with-(all-)?dependencies)(?!\S)")
        # MediaWiki's own check (update.php, checkComposerLockUpToDate) holds
        # the vendor directory to core's composer.json: the same pins go there.
        core = re.search(r"composer require (?![^&;|]*--working-dir)([^&;|]*)", step)
        self.assertIsNotNone(core, "core composer.json gets the pinned versions")
        for flag in ("--no-update", "--no-scripts", "--no-plugins", '"${pins[@]}"'):
            self.assertIn(flag, core.group(1))
        # Composer leaves the image in the same layer it was used in.
        self.assertGreater(step.index("rm /usr/local/bin/composer"), update.start())

    def test_image_test_job_checks_the_built_contents(self) -> None:
        import yaml

        self.assertTrue(CONTENTS_CHECK.is_file(), f"{CONTENTS_CHECK.name} is missing")
        workflow = yaml.safe_load(IMAGE_WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["test"]["steps"]
        runs = [step.get("run", "") for step in steps]
        check = [n for n, run in enumerate(runs) if "mediawiki/tests/image_contents.sh" in run]
        self.assertEqual(len(check), 1, "the test job runs mediawiki/tests/image_contents.sh once")
        step = steps[check[0]]
        self.assertNotIn("if", step, "the contents check runs on both platforms")
        self.assertNotIn("continue-on-error", step)
        script = CONTENTS_CHECK.read_text(encoding="utf-8")
        for needle in (
            "core-vendor.lock",
            "vendor/composer/installed.json",
            "Symfony\\Component\\Yaml\\Yaml::parse",
            "maintenance/run.php Version",
            "maintenance/run.php checkComposerLockUpToDate",
        ):
            self.assertIn(needle, script)


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

    def test_reads_core_vendor_comment_lines(self) -> None:
        text = "#   symfony/yaml  v5.4.57  CVE-2026-109413\n#   libzstd1  1.5.7  CVE-2026-1\n"
        self.assertEqual(core_vendor_lines(text), {"symfony/yaml": [("v5.4.57", "CVE-2026-109413")]})
        self.assertEqual(core_vendor_lines("#   symfony/yaml  v5.4.57  fixed upstream\n"), {})

    def test_run_steps_join_continuation_lines(self) -> None:
        text = "FROM scratch\nRUN a \\\n    && b\nRUN c\n"
        self.assertEqual(run_steps(text), ["RUN a      && b", "RUN c"])

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
