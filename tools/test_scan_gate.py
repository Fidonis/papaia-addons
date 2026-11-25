"""Tests for tools/scan_gate.py; run with `python -m unittest discover -s tools`."""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import datetime
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import scan_gate

HERE = Path(__file__).resolve().parent
SEEDED = HERE / "fixtures" / "scan" / "seeded-critical.json"
SEEDED_FIXABLE = HERE / "fixtures" / "scan" / "seeded-fixable.json"
SEEDED_NO_FIX = HERE / "fixtures" / "scan" / "seeded-no-fix.json"
REPO_EXCEPTIONS = HERE.parent / "security" / "exceptions.yaml"
TODAY = "2026-11-21"
IMAGE = "ghcr.io/fidonis/papaia-mediawiki"
THIRD_PARTY = "docker.io/library/postgres"
DIGEST = "sha256:" + "c" * 64
OTHER_DIGEST = "sha256:" + "d" * 64
OWN_DIGEST = "sha256:" + "e" * 64
THIRD_PARTY_REF = f"{THIRD_PARTY}:18.7@{DIGEST}"
OWN_REF = f"{IMAGE}:1.43.11-1.0.0@{OWN_DIGEST}"
# The CI tool set is pinned by tools/ci/uv.lock; its digest is the lock's sha256.
TOOL_LOCK = b"version = 1\n# demo lock of the CI tool set\n"
TOOL_DIGEST = "sha256:" + hashlib.sha256(TOOL_LOCK).hexdigest()
PENDING_APPROVAL = re.compile(r"^pending \([^()<>]+\)$", re.IGNORECASE)
# A role (optionally with name or date), not the `<role that countersigned>` placeholder.
COUNTERSIGNED = re.compile(r"^[A-Za-z][^<>]*$")


def vuln(
    vid: str = "CVE-2099-1000",
    pkg: str = "libdemo",
    severity: str = "HIGH",
    fixed: str | None = "1.0.1",
) -> dict:
    found = {
        "VulnerabilityID": vid,
        "PkgName": pkg,
        "InstalledVersion": "1.0.0",
        "Severity": severity,
    }
    if fixed is not None:
        found["FixedVersion"] = fixed
    return found


def report(*vulns: dict, artifact: str = THIRD_PARTY_REF) -> dict:
    return {
        "SchemaVersion": 2,
        "ArtifactName": artifact,
        "ArtifactType": "container_image",
        "Results": [
            {"Target": "demo (debian 13.2)", "Class": "os-pkgs", "Vulnerabilities": list(vulns)},
            {"Target": "composer.lock", "Class": "lang-pkgs", "Vulnerabilities": None},
        ],
    }


def entry(**overrides: object) -> dict:
    """An approved 14-day entry for a fixable finding of the pinned third-party image.

    Every entry names the digest it was reviewed for; `digest=None` drops it.
    """
    base: dict = {
        "id": "CVE-2099-1000",
        "package": "libdemo",
        "image": THIRD_PARTY,
        # Field order of the committed file: digest right after image.
        "digest": DIGEST,
        "severity": "HIGH",
        "reason": "Distribution fix available, the upstream image is not rebuilt yet.",
        "added": datetime.date(2026, 11, 20),
        "expires": datetime.date(2026, 12, 4),
        "approved_by": "security officer",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


third_party_entry = entry


def own_entry(**overrides: object) -> dict:
    """An entry for the own wiki image, bound to its pinned digest."""
    return entry(**{"image": IMAGE, "digest": OWN_DIGEST, **overrides})


def tool_entry(**overrides: object) -> dict:
    """An entry for the CI tool set, bound to the digest of its lock."""
    return entry(**{"image": scan_gate.TOOL_SET, "digest": TOOL_DIGEST, **overrides})


def pinning_root(base: Path, *refs: str, lock: bytes | None = TOOL_LOCK) -> Path:
    """A repository root whose only add-on pins `refs` as image defaults.

    `lock` is the CI tool set lock (tools/ci/uv.lock) of that root; None leaves it out.
    """
    root = base / "pins"
    addon = root / "demo"
    addon.mkdir(parents=True, exist_ok=True)
    tool_set = root / "tools" / "ci"
    tool_set.mkdir(parents=True, exist_ok=True)
    if lock is None:
        (tool_set / "uv.lock").unlink(missing_ok=True)
    else:
        (tool_set / "uv.lock").write_bytes(lock)
    (addon / "papaia-app.yaml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    services = "".join(
        f"  s{n}:\n    image: ${{S{n}_IMAGE:-{ref}}}\n" for n, ref in enumerate(refs)
    )
    (addon / "docker-compose.yml").write_text(f"services:\n{services}", encoding="utf-8")
    return root


def review_problems(raw: dict) -> list[str]:
    """Problems with the review state of one raw exception entry.

    An entry is either waiting for the countersignature (`pending (<who signs>)`,
    which the gate lists without waiving anything) or names the role that
    countersigned it. Both are valid states of the file; an empty approval or the
    template placeholder is not. Every entry expires at most MAX_DAYS (14) after
    it was added.
    """
    problems = []
    approval = raw.get("approved_by")
    if not isinstance(approval, str) or not approval.strip():
        problems.append("approved_by is empty")
    elif approval.strip().lower().startswith("pending"):
        if not PENDING_APPROVAL.match(approval.strip()):
            problems.append("a pending approval names who countersigns: `pending (<role>)`")
    elif not COUNTERSIGNED.match(approval.strip()):
        problems.append("approved_by does not name the countersigning role")
    added, expires = raw.get("added"), raw.get("expires")
    if not isinstance(added, datetime.date) or not isinstance(expires, datetime.date):
        problems.append("added and expires are dates")
    elif expires < added:
        problems.append("expires before it was added")
    elif (expires - added).days > scan_gate.MAX_DAYS:
        problems.append(f"valid for more than {scan_gate.MAX_DAYS} days")
    return problems


def pinned_tags(root: Path) -> dict[str, set[str]]:
    """Exception image -> tags its add-on defaults pin (one per release line)."""
    import image_refs

    tags: dict[str, set[str]] = {}
    for ref in image_refs.pinned_refs(image_refs.checked_addons(root)):
        name = ref.split("@", 1)[0]
        tag = name.rpartition(":")[2]
        tags.setdefault(scan_gate.exception_image(scan_gate.repository(ref)), set()).add(tag)
    return tags


def reviewed_problems(item: scan_gate.Entry, tags: dict[str, set[str]], reason: str | None = None) -> list[str]:
    """Pinned tags of the entry's image that its reason does not name.

    The reason names the scanned build as `<image>:<tag>`; an image pinned on
    two release lines names both tags (`<image>:<tag> and <tag>`).
    """
    if reason is None:
        reason = REASONS[(item.image, item.vuln_id, item.package, item.severity)]
    if f"{item.image}:" not in reason:
        return sorted(tags.get(item.image, set()))
    return sorted(tag for tag in tags.get(item.image, set()) if not re.search(
        rf"(?<![\w.-]){re.escape(tag)}(?![\w.-]|\.\d)", reason))


def _reasons() -> dict[tuple[str, str, str, str], str]:
    import yaml

    data = yaml.safe_load(REPO_EXCEPTIONS.read_text(encoding="utf-8"))
    return {
        (raw["image"], raw["id"], raw["package"], raw["severity"]): raw["reason"]
        for raw in data["exceptions"] or []
    }


REASONS = _reasons()


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def write_report(self, data: object) -> Path:
        path = self.tmp / "report.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def write_exceptions(self, entries: list[dict] | None, **top: object) -> Path:
        import yaml

        data: dict = {"version": 1, "exceptions": entries if entries is not None else []}
        data.update(top)
        path = self.tmp / "exceptions.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    def run_gate(
        self,
        report_path: Path,
        exceptions: Path | None = None,
        *extra: str,
        root: Path | None = None,
    ) -> tuple[int, str, str]:
        argv = [str(report_path), "--today", TODAY]
        argv += ["--exceptions", str(exceptions or self.write_exceptions([]))]
        # The committed file is checked against the pins of this repository,
        # a test file against a root that pins the demo images and tool set.
        if root is None and exceptions != REPO_EXCEPTIONS:
            root = pinning_root(self.tmp, THIRD_PARTY_REF, OWN_REF)
        if root is not None:
            argv += ["--root", str(root)]
        argv += list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scan_gate.main(argv)
        return code, out.getvalue(), err.getvalue()


class SeededCriticalTest(GateCase):
    def test_seeded_critical_vulnerability_fails_the_scan(self) -> None:
        code, _, err = self.run_gate(SEEDED)
        self.assertEqual(code, 1)
        self.assertIn("CVE-2099-0001", err)
        self.assertIn("seeded-critical-lib", err)
        self.assertNotIn("CVE-2099-0002", err)

    def test_seeded_report_fails_against_the_repository_exceptions(self) -> None:
        code, _, _ = self.run_gate(SEEDED, REPO_EXCEPTIONS)
        self.assertEqual(code, 1)


class SeededFixtureTest(GateCase):
    """The fixtures the CI self test runs through the gate on every pull request."""

    def test_seeded_fixable_critical_and_high_fail_and_are_named(self) -> None:
        code, _, err = self.run_gate(SEEDED_FIXABLE, REPO_EXCEPTIONS)
        self.assertEqual(code, 1)
        for name in ("CVE-2099-0011", "seeded-fixable-critical-lib", "CVE-2099-0012", "seeded-fixable-high-lib"):
            self.assertIn(name, err)
        self.assertIn("2 failing", self.run_gate(SEEDED_FIXABLE)[1])

    def test_seeded_findings_without_fix_pass_and_are_reported(self) -> None:
        no_fix = self.tmp / "no-fix.txt"
        code, out, err = self.run_gate(
            SEEDED_NO_FIX, REPO_EXCEPTIONS, "--no-fix", str(no_fix), "--platform", "linux/arm64"
        )
        self.assertEqual(code, 0, err)
        artifact = "ghcr.io/fidonis/papaia-mediawiki:1.43.11-1.0.0"
        self.assertEqual(
            no_fix.read_text(encoding="utf-8").splitlines(),
            [
                f"{artifact}\tlinux/arm64\tCVE-2099-0021\tseeded-no-fix-critical-lib\t1.0.0-1\tCRITICAL",
                f"{artifact}\tlinux/arm64\tCVE-2099-0022\tseeded-no-fix-high-lib\t4.0.0-1\tHIGH",
            ],
        )
        for name in ("CVE-2099-0021", "seeded-no-fix-critical-lib", "CVE-2099-0022", "seeded-no-fix-high-lib"):
            self.assertIn(name, out)
        self.assertIn("2 without fix", out)

    def test_ci_runs_the_seeded_fixtures_on_every_pull_request(self) -> None:
        import yaml

        workflow = yaml.safe_load((HERE.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True))
        self.assertIn("pull_request", triggers)
        steps = workflow["jobs"]["scan-gate"]["steps"]
        script = "\n".join(str(step.get("run", "")) for step in steps)
        self.assertIn("tools/fixtures/scan/seeded-fixable.json", script)
        self.assertIn("tools/fixtures/scan/seeded-no-fix.json", script)
        self.assertNotIn("if", workflow["jobs"]["scan-gate"])


class NoFixReportTest(GateCase):
    def test_no_fix_list_holds_critical_and_high_without_fix_only(self) -> None:
        no_fix = self.tmp / "no-fix.txt"
        data = report(
            vuln("CVE-2099-1", "liba", "CRITICAL", fixed=None),
            vuln("CVE-2099-2", "libb", "HIGH", fixed=""),
            vuln("CVE-2099-3", "libc", "MEDIUM", fixed=None),
            vuln("CVE-2099-4", "libd", "LOW", fixed=None),
            artifact=THIRD_PARTY_REF,
        )
        code, _, _ = self.run_gate(
            self.write_report(data), None, "--no-fix", str(no_fix), "--platform", "linux/amd64"
        )
        self.assertEqual(code, 0)
        lines = no_fix.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            [
                f"{THIRD_PARTY_REF}\tlinux/amd64\tCVE-2099-1\tliba\t1.0.0\tCRITICAL",
                f"{THIRD_PARTY_REF}\tlinux/amd64\tCVE-2099-2\tlibb\t1.0.0\tHIGH",
            ],
        )

    def test_no_fix_list_is_written_when_empty_and_platform_defaults_to_a_dash(self) -> None:
        no_fix = self.tmp / "no-fix.txt"
        no_fix.write_text("stale\n", encoding="utf-8")
        code, _, _ = self.run_gate(self.write_report(report()), None, "--no-fix", str(no_fix))
        self.assertEqual(code, 0)
        self.assertEqual(no_fix.read_text(encoding="utf-8"), "")
        self.run_gate(self.write_report(report(vuln(fixed=None))), None, "--no-fix", str(no_fix))
        self.assertIn("\t-\tCVE-2099-1000\t", no_fix.read_text(encoding="utf-8"))

    def test_no_fix_list_is_written_when_the_gate_fails(self) -> None:
        no_fix = self.tmp / "no-fix.txt"
        data = report(vuln(), vuln("CVE-2099-2", fixed=None))
        code, _, _ = self.run_gate(self.write_report(data), None, "--no-fix", str(no_fix))
        self.assertEqual(code, 1)
        self.assertIn("CVE-2099-2", no_fix.read_text(encoding="utf-8"))

    def test_summary_gets_the_counts_per_image(self) -> None:
        summary = self.tmp / "summary.md"
        summary.write_text("earlier step\n", encoding="utf-8")
        data = report(
            vuln("CVE-2099-1", fixed="2"),
            vuln("CVE-2099-1000"),
            vuln("CVE-2099-3", fixed=None),
            vuln("CVE-2099-4", severity="CRITICAL", fixed=None),
        )
        code, _, _ = self.run_gate(
            self.write_report(data), self.write_exceptions([entry()]),
            "--summary", str(summary), "--platform", "linux/arm64",
        )
        self.assertEqual(code, 1)
        text = summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("earlier step\n"))
        self.assertIn("| Image | Platform | Failing | Excepted | Without fix |", text)
        self.assertIn(f"| `{THIRD_PARTY_REF}` | linux/arm64 | 1 | 1 | 2 |", text)

    def test_titles_and_descriptions_are_not_printed(self) -> None:
        found = vuln(fixed=None)
        found["Title"] = "title-text-from-the-report"
        found["Description"] = "description-text-from-the-report"
        no_fix, summary = self.tmp / "no-fix.txt", self.tmp / "summary.md"
        _, out, err = self.run_gate(
            self.write_report(report(found, vuln("CVE-2099-2"))), None,
            "--no-fix", str(no_fix), "--summary", str(summary),
        )
        for text in (out, err, no_fix.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")):
            self.assertNotIn("title-text", text)
            self.assertNotIn("description-text", text)

    def test_markup_in_the_artifact_name_does_not_reach_the_summary_table(self) -> None:
        summary = self.tmp / "summary.md"
        self.run_gate(
            self.write_report(report(artifact="demo|`x`\n| row")), None, "--summary", str(summary)
        )
        row = summary.read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(row.count("|"), 6, row)
        self.assertNotIn("`x`", row)

    def test_invalid_platform_is_refused(self) -> None:
        for platform in ("amd64", "linux/amd64; rm", "", "linux/" + "a" * 80):
            with self.subTest(platform=platform):
                code, _, _ = self.run_gate(self.write_report(report()), None, "--platform", platform)
                self.assertEqual(code, 2)

    def test_scan_workflows_keep_report_and_no_fix_list_for_ninety_days(self) -> None:
        import yaml

        root = HERE.parent / ".github" / "workflows"
        for name, job in (("ci.yml", "vulnerability"), ("ci.yml", "vulnerability-tools"), ("mediawiki-image.yml", "test")):
            with self.subTest(workflow=name, job=job):
                steps = yaml.safe_load((root / name).read_text(encoding="utf-8"))["jobs"][job]["steps"]
                gate = [s for s in steps if "scan_gate.py" in str(s.get("run", "")) and "fixtures" not in str(s.get("run", ""))]
                self.assertEqual(len(gate), 1)
                self.assertIn("--no-fix", gate[0]["run"])
                self.assertIn("--summary", gate[0]["run"])
                self.assertIn("GITHUB_STEP_SUMMARY", gate[0]["run"])
                uploads = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
                self.assertEqual(len(uploads), 1)
                self.assertGreater(steps.index(uploads[0]), steps.index(gate[0]))
                self.assertEqual(uploads[0]["if"], "always()")
                self.assertEqual(uploads[0]["with"]["retention-days"], 90)
                self.assertIn("no-fix", str(uploads[0]["with"]["path"]))
                scans = [s for s in steps if str(s.get("uses", "")).startswith("aquasecurity/trivy-action@")]
                for scan in scans:
                    options = scan["with"]
                    self.assertNotIn("ignore-unfixed", options)
                    self.assertNotIn("severity", options)
                    self.assertEqual(options["format"], "json")


class ThirdPartyEntryTest(GateCase):
    """A fixable finding of a third-party image: digest-scoped entry, at most 14 days."""

    def gate(self, data: dict, entries: list[dict], *pins: str) -> tuple[int, str, str]:
        root = pinning_root(self.tmp, *(pins or (THIRD_PARTY_REF,)))
        return self.run_gate(self.write_report(data), self.write_exceptions(entries), root=root)

    def test_fourteen_day_entry_with_the_scanned_digest_waives_exactly_that_finding(self) -> None:
        code, out, err = self.gate(report(vuln(), artifact=THIRD_PARTY_REF), [third_party_entry()])
        self.assertEqual(code, 0, err)
        self.assertIn("1 excepted", out)
        data = report(vuln(), vuln(pkg="libother"), vuln("CVE-2099-2"), artifact=THIRD_PARTY_REF)
        code, out, err = self.gate(data, [third_party_entry()])
        self.assertEqual(code, 1)
        self.assertIn("1 excepted", out)
        self.assertIn("2 failing", out)
        self.assertIn("libother", err)
        self.assertIn("CVE-2099-2", err)

    def test_fifteen_days_are_refused(self) -> None:
        code, _, err = self.gate(
            report(vuln(), artifact=THIRD_PARTY_REF),
            [third_party_entry(expires=datetime.date(2026, 12, 5))],
        )
        self.assertEqual(code, 2)
        self.assertIn("14 days", err)

    def test_missing_digest_is_refused(self) -> None:
        code, _, err = self.gate(
            report(vuln(), artifact=THIRD_PARTY_REF), [third_party_entry(digest=None)]
        )
        self.assertEqual(code, 2)
        self.assertIn("digest", err)

    def test_malformed_digest_is_refused(self) -> None:
        for digest in ("sha256:abc", "c" * 64, "sha256:" + "C" * 64, 5):
            with self.subTest(digest=digest):
                code, _, _ = self.gate(report(artifact=THIRD_PARTY_REF), [third_party_entry(digest=digest)])
                self.assertEqual(code, 2)

    def test_another_digest_waives_nothing(self) -> None:
        other = f"{THIRD_PARTY}:18.7@{OTHER_DIGEST}"
        code, _, err = self.gate(report(vuln(), artifact=other), [third_party_entry()], THIRD_PARTY_REF)
        self.assertEqual(code, 1)
        self.assertIn("CVE-2099-1000", err)
        # A scan without a digest cannot match a digest-scoped entry either.
        code, _, _ = self.gate(report(vuln(), artifact=f"{THIRD_PARTY}:18.7"), [third_party_entry()])
        self.assertEqual(code, 1)

    def test_entry_for_a_digest_no_longer_pinned_is_refused(self) -> None:
        code, _, err = self.gate(
            report(artifact=THIRD_PARTY_REF), [third_party_entry()], f"{THIRD_PARTY}:18.7@{OTHER_DIGEST}"
        )
        self.assertEqual(code, 2)
        self.assertIn("no longer pinned", err)
        self.assertIn(DIGEST, err)

    def test_expired_entry_fails_the_run(self) -> None:
        code, _, err = self.gate(
            report(artifact=THIRD_PARTY_REF),
            [third_party_entry(added=datetime.date(2026, 11, 1), expires=datetime.date(2026, 11, 15))],
        )
        self.assertEqual(code, 1)
        self.assertIn("expired", err)

    def test_entry_for_a_finding_without_fix_waives_nothing(self) -> None:
        code, out, _ = self.gate(report(vuln(fixed=None), artifact=THIRD_PARTY_REF), [third_party_entry()])
        self.assertEqual(code, 0)
        self.assertIn("0 excepted", out)
        self.assertIn("exception not needed (finding has no fix)", out)

    def test_pending_entry_waives_nothing_on_a_milestone(self) -> None:
        code, _, err = self.gate(
            report(vuln(), artifact=THIRD_PARTY_REF),
            [third_party_entry(approved_by="pending (security officer countersignature)")],
        )
        self.assertEqual(code, 1)
        self.assertIn("exception pending approval", err)

    def test_own_images_are_the_published_namespace_only(self) -> None:
        for image in (IMAGE, "ghcr.io/fidonis/qdrant-mcp-rbac"):
            with self.subTest(image=image):
                self.assertTrue(scan_gate.is_own(image))
        for image in (scan_gate.TOOL_SET, THIRD_PARTY, "ghcr.io/paperless-ngx/paperless-ngx", "ghcr.io/fidonisx/demo"):
            with self.subTest(image=image):
                self.assertFalse(scan_gate.is_own(image))


class DigestBoundEntryTest(GateCase):
    """Condition D1 of the security officer (2026-11-25), for every entry.

    Every entry names the digest it was reviewed for and covers a scan of that
    digest only; an entry for a finding with a fix runs at most 14 days; own
    images fix, they do not except.
    """

    def test_own_image_or_tool_set_entry_without_digest_is_refused(self) -> None:
        for raw in (own_entry(digest=None), tool_entry(digest=None)):
            with self.subTest(image=raw["image"]):
                code, _, err = self.run_gate(self.write_report(report()), self.write_exceptions([raw]))
                self.assertEqual(code, 2, err)
                self.assertIn("digest", err)

    def test_tool_set_entry_with_the_scanned_digest_waives_its_finding(self) -> None:
        code, out, err = self.run_gate(
            self.write_report(report(vuln(), artifact=scan_gate.TOOL_SET)),
            self.write_exceptions([tool_entry()]),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("1 excepted", out)

    def test_tool_set_entry_with_another_digest_waives_nothing(self) -> None:
        findings = [scan_gate.Finding("CVE-2099-1000", "libdemo", "1.0.0", "HIGH", "1.0.1")]
        path = self.write_exceptions([tool_entry(digest=OTHER_DIGEST)])
        entries = scan_gate.load_exceptions(path)
        verdict = scan_gate.evaluate(
            scan_gate.TOOL_SET, findings, entries, datetime.date(2026, 11, 21), digest=TOOL_DIGEST
        )
        self.assertEqual((len(verdict.failing), len(verdict.excepted)), (1, 0))
        # Through the gate, a lock that moved leaves the entry unused: refused.
        code, _, err = self.run_gate(self.write_report(report(vuln(), artifact=scan_gate.TOOL_SET)), path)
        self.assertEqual(code, 2)
        self.assertIn("no longer pinned", err)
        self.assertIn(OTHER_DIGEST, err)

    def test_tool_set_entry_is_refused_once_the_lock_is_gone(self) -> None:
        root = pinning_root(self.tmp, THIRD_PARTY_REF, lock=None)
        code, _, err = self.run_gate(
            self.write_report(report(artifact=scan_gate.TOOL_SET)), self.write_exceptions([tool_entry()]), root=root
        )
        self.assertEqual(code, 2)
        self.assertIn("no longer pinned", err)

    def test_tool_set_digest_is_the_sha256_of_its_lock(self) -> None:
        root = pinning_root(self.tmp, THIRD_PARTY_REF)
        self.assertEqual(scan_gate.tool_set_digest(root), TOOL_DIGEST)
        self.assertIn((scan_gate.TOOL_SET, TOOL_DIGEST), scan_gate.pinned_digests(root))
        self.assertIn((THIRD_PARTY, DIGEST), scan_gate.pinned_digests(root))
        self.assertEqual(scan_gate.tool_set_digest(pinning_root(self.tmp, lock=None)), "")

    def test_own_image_entry_with_another_digest_waives_nothing(self) -> None:
        findings = [scan_gate.Finding("CVE-2099-1000", "libdemo", "1.0.0", "HIGH", "1.0.1")]
        entries = scan_gate.load_exceptions(self.write_exceptions([own_entry()]))
        verdict = scan_gate.evaluate(
            f"{IMAGE}:1.43.11-1.0.0@{OTHER_DIGEST}", findings, entries, datetime.date(2026, 11, 21)
        )
        self.assertEqual((len(verdict.failing), len(verdict.excepted), verdict.refused), (1, 0, []))

    def test_own_image_entry_for_a_finding_with_a_fix_is_refused(self) -> None:
        # Regardless of term and signature, and for a release candidate too.
        cases = {
            "approved, 14 days": own_entry(),
            "approved, 1 day": own_entry(expires=datetime.date(2026, 11, 21)),
            "pending": own_entry(approved_by="pending (security officer countersignature)"),
            "critical": own_entry(severity="CRITICAL"),
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                found = vuln(severity=raw["severity"])
                code, out, err = self.run_gate(
                    self.write_report(report(found, artifact=OWN_REF)), self.write_exceptions([raw])
                )
                self.assertEqual(code, 2, out)
                self.assertIn("own images fix", err)
                self.assertIn("CVE-2099-1000 libdemo", err)
                self.assertIn("0 excepted", out)
        candidate = f"127.0.0.1:57500/unpublished/papaia-mediawiki:1.43.11-1.0.0@{OWN_DIGEST}"
        code, _, err = self.run_gate(
            self.write_report(report(vuln(), artifact=candidate)), self.write_exceptions([own_entry()])
        )
        self.assertEqual(code, 2)
        self.assertIn("own images fix", err)

    def test_own_image_entry_for_a_finding_without_fix_is_not_needed(self) -> None:
        code, out, err = self.run_gate(
            self.write_report(report(vuln(fixed=None), artifact=OWN_REF)), self.write_exceptions([own_entry()])
        )
        self.assertEqual(code, 0, err)
        self.assertIn("exception not needed (finding has no fix)", out)

    def test_fifteen_days_are_refused_for_every_image(self) -> None:
        long = {"added": datetime.date(2026, 11, 20), "expires": datetime.date(2026, 12, 5)}
        for raw in (entry(**long), own_entry(**long), tool_entry(**long)):
            with self.subTest(image=raw["image"]):
                code, _, err = self.run_gate(self.write_report(report()), self.write_exceptions([raw]))
                self.assertEqual(code, 2, err)
                self.assertIn("14 days", err)
                with self.assertRaises(scan_gate.GateError):
                    scan_gate.load_exceptions(self.write_exceptions([raw]))

    def test_fourteen_days_pass_for_third_party_and_tool_set(self) -> None:
        self.assertEqual(scan_gate.MAX_DAYS, 14)
        for raw, artifact in ((entry(), THIRD_PARTY_REF), (tool_entry(), scan_gate.TOOL_SET)):
            with self.subTest(image=raw["image"]):
                self.assertEqual((raw["expires"] - raw["added"]).days, 14)
                code, out, err = self.run_gate(
                    self.write_report(report(vuln(), artifact=artifact)), self.write_exceptions([raw])
                )
                self.assertEqual(code, 0, err)
                self.assertIn("1 excepted", out)


class RepositoryEntryFormTest(GateCase):
    """The committed exception file under the new form."""

    def test_committed_file_loads_and_names_pinned_digests_only(self) -> None:
        # A digest that no add-on default or CI tool pin uses any more is an
        # unused entry: re-check it against the scan of the new pin.
        entries = scan_gate.load_exceptions(REPO_EXCEPTIONS)
        stale = scan_gate.stale_digest_entries(entries, scan_gate.pinned_digests(HERE.parent))
        self.assertEqual([f"{e.image}@{e.digest} {e.vuln_id} {e.package}" for e in stale], [])

    def test_committed_entries_are_digest_bound_and_short(self) -> None:
        for item in scan_gate.load_exceptions(REPO_EXCEPTIONS):
            with self.subTest(id=item.vuln_id, package=item.package, image=item.image):
                self.assertRegex(item.digest, r"^sha256:[0-9a-f]{64}$")
                self.assertLessEqual((item.expires - item.added).days, 14)

    def test_committed_file_has_no_entry_for_a_finding_without_fix(self) -> None:
        stale = [key for key, reason in REASONS.items() if "no fixed version" in reason]
        self.assertEqual(stale, [])

    def test_no_entry_waives_a_finding_of_the_mediawiki_image(self) -> None:
        # Condition M1 of the security officer: a finding with a fix in the
        # wiki image is fixed in the image, never excepted. Findings without a
        # fix take no entry, so the image has none at all.
        covered = [
            f"{item.vuln_id} {item.package}"
            for item in scan_gate.load_exceptions(REPO_EXCEPTIONS)
            if item.image == IMAGE
        ]
        self.assertEqual(covered, [])

    def test_no_own_image_entry_covers_a_finding_with_a_fix(self) -> None:
        fixable = [
            f"{image} {vid} {package}"
            for (image, vid, package, _), reason in REASONS.items()
            if image.startswith("ghcr.io/fidonis/") and re.search(r"\bfixed in\b", reason)
        ]
        self.assertEqual(fixable, [])

    def test_entry_of_fifteen_days_or_without_digest_is_refused(self) -> None:
        cases = [
            make(**change)
            for make in (entry, own_entry, tool_entry)
            for change in ({"expires": datetime.date(2026, 12, 5)}, {"digest": None})
        ]
        for raw in cases:
            with self.subTest(entry=raw), self.assertRaises(scan_gate.GateError):
                scan_gate.load_exceptions(self.write_exceptions([raw]))

    def test_seeded_critical_still_fails(self) -> None:
        self.assertEqual(self.run_gate(SEEDED, REPO_EXCEPTIONS)[0], 1)


class ThresholdTest(GateCase):
    def test_clean_report_passes(self) -> None:
        code, out, _ = self.run_gate(self.write_report(report(vuln(severity="MEDIUM"))))
        self.assertEqual(code, 0)
        self.assertIn("0 failing", out)

    def test_report_without_results_passes(self) -> None:
        data = report()
        del data["Results"]
        code, _, _ = self.run_gate(self.write_report(data))
        self.assertEqual(code, 0)

    def test_critical_without_fix_is_listed_but_does_not_fail(self) -> None:
        code, out, _ = self.run_gate(
            self.write_report(report(vuln(severity="CRITICAL", fixed=None)))
        )
        self.assertEqual(code, 0)
        self.assertIn("CVE-2099-1000", out)

    def test_empty_fixed_version_counts_as_no_fix(self) -> None:
        code, _, _ = self.run_gate(self.write_report(report(vuln(severity="CRITICAL", fixed=""))))
        self.assertEqual(code, 0)

    def test_high_with_fix_fails_without_exception(self) -> None:
        code, _, err = self.run_gate(self.write_report(report(vuln())))
        self.assertEqual(code, 1)
        self.assertIn("HIGH", err)

    def test_high_without_fix_does_not_fail(self) -> None:
        code, _, _ = self.run_gate(self.write_report(report(vuln(fixed=None))))
        self.assertEqual(code, 0)


class ExceptionTest(GateCase):
    def test_matching_exception_lets_the_finding_pass(self) -> None:
        code, out, _ = self.run_gate(
            self.write_report(report(vuln())), self.write_exceptions([entry()])
        )
        self.assertEqual(code, 0)
        self.assertIn("1 excepted", out)

    def test_exception_covers_a_critical_finding_too(self) -> None:
        code, _, _ = self.run_gate(
            self.write_report(report(vuln(severity="CRITICAL"))),
            self.write_exceptions([entry(severity="CRITICAL")]),
        )
        self.assertEqual(code, 0)

    def test_exception_with_other_severity_does_not_cover(self) -> None:
        code, _, err = self.run_gate(
            self.write_report(report(vuln(severity="CRITICAL"))),
            self.write_exceptions([entry(severity="HIGH")]),
        )
        self.assertEqual(code, 1)
        self.assertIn("CVE-2099-1000", err)

    def test_exception_for_another_image_does_not_cover(self) -> None:
        code, _, _ = self.run_gate(
            self.write_report(report(vuln(), artifact="ghcr.io/fidonis/other:1.0.0")),
            self.write_exceptions([entry()]),
        )
        self.assertEqual(code, 1)

    def test_image_is_matched_by_repository_and_digest_whatever_the_tag(self) -> None:
        for artifact in (f"{THIRD_PARTY}:18.7@{DIGEST}", f"{THIRD_PARTY}:other@{DIGEST}", f"{THIRD_PARTY}@{DIGEST}"):
            with self.subTest(artifact=artifact):
                code, _, _ = self.run_gate(
                    self.write_report(report(vuln(), artifact=artifact)), self.write_exceptions([entry()])
                )
                self.assertEqual(code, 0)

    def test_exception_for_another_package_does_not_cover(self) -> None:
        code, _, _ = self.run_gate(
            self.write_report(report(vuln())), self.write_exceptions([entry(package="libother")])
        )
        self.assertEqual(code, 1)

    def test_expired_exception_fails(self) -> None:
        code, _, err = self.run_gate(
            self.write_report(report(vuln())),
            self.write_exceptions(
                [entry(added=datetime.date(2026, 11, 10), expires=datetime.date(2026, 11, 20))]
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("expired", err)

    def test_expired_exception_fails_even_when_unused(self) -> None:
        code, _, err = self.run_gate(
            self.write_report(report()),
            self.write_exceptions(
                [entry(added=datetime.date(2026, 11, 10), expires=datetime.date(2026, 11, 20))]
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("expired", err)

    def test_exception_expiring_today_still_holds(self) -> None:
        code, _, _ = self.run_gate(
            self.write_report(report(vuln())),
            self.write_exceptions(
                [entry(added=datetime.date(2026, 11, 10), expires=datetime.date(2026, 11, 21))]
            ),
        )
        self.assertEqual(code, 0)

    def test_pending_approval_does_not_cover_the_finding(self) -> None:
        for approval in ("pending (security officer countersignature)", "Pending", "  pending"):
            with self.subTest(approval=approval):
                code, _, err = self.run_gate(
                    self.write_report(report(vuln())),
                    self.write_exceptions([entry(approved_by=approval)]),
                )
                self.assertEqual(code, 1)
                self.assertIn("CVE-2099-1000", err)
                self.assertIn("pending approval", err)

    def test_unused_pending_exception_does_not_fail(self) -> None:
        code, out, _ = self.run_gate(
            self.write_report(report()), self.write_exceptions([entry(approved_by="pending")])
        )
        self.assertEqual(code, 0)
        self.assertIn("pending approval", out)

    def test_tool_set_report_is_matched_by_its_path_and_lock_digest(self) -> None:
        code, _, _ = self.run_gate(
            self.write_report(report(vuln(), artifact="tools/ci")),
            self.write_exceptions([tool_entry()]),
        )
        self.assertEqual(code, 0)

    def test_exception_for_a_finding_without_fix_is_not_needed_and_reported(self) -> None:
        # Findings without a fix are listed in the report; an entry for one
        # waives nothing and is reported for removal.
        code, out, _ = self.run_gate(
            self.write_report(report(vuln(fixed=None))), self.write_exceptions([entry()])
        )
        self.assertEqual(code, 0)
        self.assertIn("listed: HIGH CVE-2099-1000", out)
        self.assertIn("exception not needed (finding has no fix): CVE-2099-1000 libdemo", out)
        self.assertIn("0 excepted", out)

    def test_pending_exception_for_a_finding_without_fix_waives_nothing_and_passes(self) -> None:
        code, out, _ = self.run_gate(
            self.write_report(report(vuln(fixed=None))),
            self.write_exceptions([entry(approved_by="pending (security officer countersignature)")]),
        )
        self.assertEqual(code, 0)
        self.assertIn("pending approval", out)

    def test_unused_exception_is_reported_without_failing(self) -> None:
        code, out, _ = self.run_gate(self.write_report(report()), self.write_exceptions([entry()]))
        self.assertEqual(code, 0)
        self.assertIn("unused", out)


class PinnedImageTest(GateCase):
    """Every exception names an image that an add-on default still pins."""

    def test_entry_for_an_image_no_addon_pins_is_reported(self) -> None:
        entries = scan_gate.load_exceptions(
            self.write_exceptions([entry(), entry(image="docker.io/library/gone")])
        )
        stale = scan_gate.unpinned_entries(entries, {THIRD_PARTY})
        self.assertEqual([item.image for item in stale], ["docker.io/library/gone"])

    def test_entries_for_pinned_images_and_the_tool_set_pass(self) -> None:
        entries = scan_gate.load_exceptions(
            self.write_exceptions([entry(), tool_entry()])
        )
        self.assertEqual(scan_gate.unpinned_entries(entries, {THIRD_PARTY}), [])

    def test_pinned_repositories_follow_the_compose_defaults(self) -> None:
        root = self.tmp / "repo"
        addon = root / "demo"
        addon.mkdir(parents=True)
        (addon / "papaia-app.yaml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
        (addon / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: ${APP_IMAGE:-docker.io/library/postgres:18.7@sha256:"
            + "a" * 64
            + "}\n  mcp:\n    image: ${MCP_IMAGE:-127.0.0.1:57500/unpublished/qdrant-mcp-rbac:0.3.1@sha256:"
            + "b" * 64
            + "}\n",
            encoding="utf-8",
        )
        self.assertEqual(
            scan_gate.pinned_repositories(root),
            {"docker.io/library/postgres", "ghcr.io/fidonis/qdrant-mcp-rbac"},
        )

    def test_candidate_report_is_matched_by_the_published_repository(self) -> None:
        candidate = "127.0.0.1:57500/unpublished/qdrant-mcp-rbac:0.3.1@sha256:" + "b" * 64
        root = pinning_root(self.tmp, candidate)
        published = own_entry(image="ghcr.io/fidonis/qdrant-mcp-rbac", digest="sha256:" + "b" * 64)
        code, out, _ = self.run_gate(
            self.write_report(report(vuln(fixed=None), artifact=candidate)),
            self.write_exceptions([published]),
            root=root,
        )
        self.assertEqual(code, 0, out)
        self.assertIn("exception not needed (finding has no fix): CVE-2099-1000", out)

    def test_candidate_report_without_entry_fails(self) -> None:
        candidate = "127.0.0.1:57500/unpublished/qdrant-mcp-rbac:0.3.1@sha256:" + "b" * 64
        code, _, _ = self.run_gate(self.write_report(report(vuln(), artifact=candidate)))
        self.assertEqual(code, 1)

    def test_entry_naming_the_test_registry_is_rejected(self) -> None:
        path = self.write_exceptions([entry(image="127.0.0.1:57500/unpublished/qdrant-mcp-rbac")])
        code, _, err = self.run_gate(self.write_report(report()), path)
        self.assertEqual(code, 2)
        self.assertIn("ghcr.io/fidonis/qdrant-mcp-rbac", err)

    def test_repository_exceptions_name_only_pinned_images(self) -> None:
        entries = scan_gate.load_exceptions(REPO_EXCEPTIONS)
        stale = scan_gate.unpinned_entries(entries, scan_gate.pinned_repositories(HERE.parent))
        self.assertEqual([f"{e.image} {e.vuln_id} {e.package}" for e in stale], [])

    def test_repository_exceptions_were_reviewed_for_the_pinned_tags(self) -> None:
        # An entry is written for the scan of one pinned build; its reason names
        # that image and tag. When a pin moves, every entry of the image is
        # re-checked against the scan of the new pin, and entries whose finding
        # is gone are dropped, so no entry stays behind from an earlier pin.
        tags = pinned_tags(HERE.parent)
        entries = scan_gate.load_exceptions(REPO_EXCEPTIONS)
        stale = [
            f"{e.image} {e.vuln_id} {e.package}"
            for e in entries
            if e.image != scan_gate.TOOL_SET and reviewed_problems(e, tags)
        ]
        self.assertEqual(stale, [])

    def test_reviewed_check_requires_every_pinned_tag_of_the_image(self) -> None:
        tags = {IMAGE: {"1.43.11-1.0.1"}, "ghcr.io/fidonis/demo": {"0.1.2", "0.2.0-rc.2"}}
        ok = scan_gate.Entry("CVE-2099-1", "libdemo", IMAGE, "HIGH", datetime.date(2026, 11, 25),
                             datetime.date(2027, 2, 1), False)
        reasons = {
            "pinned tag": (f"libdemo 1.0 in {IMAGE}:1.43.11-1.0.1; no fix yet", []),
            "old tag": (f"libdemo 1.0 in {IMAGE}:1.43.11-1.0.0; no fix yet", ["1.43.11-1.0.1"]),
            "tag without image": ("libdemo 1.0 in 1.43.11-1.0.1; no fix yet", ["1.43.11-1.0.1"]),
        }
        for name, (reason, missing) in reasons.items():
            with self.subTest(case=name):
                self.assertEqual(reviewed_problems(ok, tags, reason), missing)
        both = dataclasses.replace(ok, image="ghcr.io/fidonis/demo")
        self.assertEqual(
            reviewed_problems(both, tags, "x in ghcr.io/fidonis/demo:0.1.2 and 0.2.0-rc.2; y"), []
        )
        self.assertEqual(
            reviewed_problems(both, tags, "x in ghcr.io/fidonis/demo:0.1.2; y"), ["0.2.0-rc.2"]
        )

    def test_repository_exceptions_hold_one_entry_per_id_package_and_image(self) -> None:
        seen: dict[tuple[str, str, str], str] = {}
        twice = []
        for item in scan_gate.load_exceptions(REPO_EXCEPTIONS):
            key = (item.image, item.vuln_id, item.package)
            if key in seen:
                twice.append(" ".join(key))
            seen[key] = item.severity
        self.assertEqual(twice, [])

    def test_repository_exceptions_carry_a_review_state_and_expire_in_time(self) -> None:
        import yaml

        data = yaml.safe_load(REPO_EXCEPTIONS.read_text(encoding="utf-8"))
        for raw in data["exceptions"] or []:
            with self.subTest(id=raw["id"], package=raw["package"], image=raw["image"]):
                self.assertEqual(review_problems(raw), [])

    def test_review_check_accepts_pending_and_countersigned_entries(self) -> None:
        base = {"added": datetime.date(2026, 11, 24), "expires": datetime.date(2026, 12, 8)}
        for approval in (
            "pending (security officer countersignature)",
            "security officer",
            "Security officer, 2026-11-30",
        ):
            with self.subTest(approval=approval):
                self.assertEqual(review_problems({**base, "approved_by": approval}), [])

    def test_review_check_rejects_missing_approval_placeholders_and_long_validity(self) -> None:
        base = {"added": datetime.date(2026, 11, 24), "expires": datetime.date(2026, 12, 8)}
        cases = {
            "empty": {**base, "approved_by": "  "},
            "not text": {**base, "approved_by": None},
            "pending without approver": {**base, "approved_by": "pending"},
            "template placeholder": {**base, "approved_by": "<role that countersigned>"},
            "too long": {**base, "approved_by": "security officer", "expires": datetime.date(2026, 12, 9)},
            "expires before added": {
                **base,
                "approved_by": "security officer",
                "expires": datetime.date(2026, 11, 23),
            },
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(review_problems(raw), [])


class ExceptionFileValidationTest(GateCase):
    def assert_rejected(self, path: Path) -> str:
        code, _, err = self.run_gate(self.write_report(report()), path)
        self.assertEqual(code, 2, err)
        return err

    def test_repository_exception_file_is_valid(self) -> None:
        entries = scan_gate.load_exceptions(REPO_EXCEPTIONS)
        for item in entries:
            self.assertLessEqual((item.expires - item.added).days, scan_gate.MAX_DAYS)

    def test_unknown_entry_field_is_rejected(self) -> None:
        self.assertIn("unknown", self.assert_rejected(self.write_exceptions([entry(note="x")])))

    def test_unknown_top_level_field_is_rejected(self) -> None:
        self.assert_rejected(self.write_exceptions([], extra=True))

    def test_missing_field_is_rejected(self) -> None:
        broken = entry()
        del broken["approved_by"]
        self.assert_rejected(self.write_exceptions([broken]))

    def test_more_entries_than_the_limit_are_rejected(self) -> None:
        entries = [entry(id=f"CVE-2099-{n:05d}") for n in range(scan_gate.MAX_ENTRIES + 1)]
        self.assertIn("at most", self.assert_rejected(self.write_exceptions(entries)))

    def test_a_file_above_the_size_limit_is_rejected(self) -> None:
        path = self.write_exceptions([entry()])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("#" * scan_gate.MAX_EXCEPTIONS_BYTES + "\n")
        self.assertIn("larger than", self.assert_rejected(path))

    def test_limits_hold_one_entry_per_remaining_finding_of_every_pinned_image(self) -> None:
        # Each remaining CRITICAL or HIGH finding of each pinned image has its
        # own entry; the limits leave room for that, but stay bounded.
        size = REPO_EXCEPTIONS.stat().st_size
        count = len(scan_gate.load_exceptions(REPO_EXCEPTIONS))
        self.assertLessEqual(count * 2, scan_gate.MAX_ENTRIES)
        self.assertLessEqual(size * 2, scan_gate.MAX_EXCEPTIONS_BYTES)
        self.assertLessEqual(scan_gate.MAX_ENTRIES, 5000)
        self.assertLessEqual(scan_gate.MAX_EXCEPTIONS_BYTES, 4 * 1024 * 1024)

    def test_short_reason_is_rejected(self) -> None:
        self.assert_rejected(self.write_exceptions([entry(reason="no fix yet")]))

    def test_validity_longer_than_fourteen_days_is_rejected(self) -> None:
        err = self.assert_rejected(
            self.write_exceptions(
                [entry(added=datetime.date(2026, 11, 1), expires=datetime.date(2026, 11, 16))]
            )
        )
        self.assertIn("14 days", err)

    def test_expiry_before_added_is_rejected(self) -> None:
        self.assert_rejected(
            self.write_exceptions(
                [entry(added=datetime.date(2026, 11, 20), expires=datetime.date(2026, 11, 19))]
            )
        )

    def test_date_as_text_is_rejected(self) -> None:
        self.assert_rejected(self.write_exceptions([entry(expires="2027-02-01")]))

    def test_unknown_severity_is_rejected(self) -> None:
        self.assert_rejected(self.write_exceptions([entry(severity="MEDIUM")]))

    def test_image_with_tag_is_rejected(self) -> None:
        self.assert_rejected(self.write_exceptions([entry(image=f"{IMAGE}:1.0.0")]))

    def test_malformed_id_is_rejected(self) -> None:
        self.assert_rejected(self.write_exceptions([entry(id="CVE 2099 1000")]))

    def test_duplicate_entry_is_rejected(self) -> None:
        self.assert_rejected(self.write_exceptions([entry(), entry()]))

    def test_wrong_version_is_rejected(self) -> None:
        self.assert_rejected(self.write_exceptions([], version=2))

    def test_missing_file_is_rejected(self) -> None:
        self.assert_rejected(self.tmp / "missing.yaml")


class ReportValidationTest(GateCase):
    def assert_rejected(self, data: object) -> None:
        code, _, err = self.run_gate(self.write_report(data))
        self.assertEqual(code, 2, err)

    def test_other_schema_version_is_rejected(self) -> None:
        data = report()
        data["SchemaVersion"] = 1
        self.assert_rejected(data)

    def test_results_not_a_list_is_rejected(self) -> None:
        data = report()
        data["Results"] = {}
        self.assert_rejected(data)

    def test_unknown_severity_is_rejected(self) -> None:
        self.assert_rejected(report(vuln(severity="SEVERE")))

    def test_missing_package_is_rejected(self) -> None:
        broken = vuln()
        del broken["PkgName"]
        self.assert_rejected(report(broken))

    def test_fixed_version_of_wrong_type_is_rejected(self) -> None:
        broken = vuln()
        broken["FixedVersion"] = 1
        self.assert_rejected(report(broken))

    def test_missing_artifact_name_is_rejected(self) -> None:
        data = copy.deepcopy(report())
        del data["ArtifactName"]
        self.assert_rejected(data)

    def test_not_json_is_rejected(self) -> None:
        path = self.tmp / "report.json"
        path.write_text("{not json", encoding="utf-8")
        code, _, _ = self.run_gate(path)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
