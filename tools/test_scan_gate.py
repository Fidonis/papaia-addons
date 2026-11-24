"""Tests for tools/scan_gate.py; run with `python -m unittest discover -s tools`."""

from __future__ import annotations

import contextlib
import copy
import datetime
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import scan_gate

HERE = Path(__file__).resolve().parent
SEEDED = HERE / "fixtures" / "scan" / "seeded-critical.json"
REPO_EXCEPTIONS = HERE.parent / "security" / "exceptions.yaml"
TODAY = "2026-11-21"
IMAGE = "ghcr.io/fidonis/papaia-mediawiki"
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


def report(*vulns: dict, artifact: str = f"{IMAGE}:1.43.11-1.0.0") -> dict:
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
    base: dict = {
        "id": "CVE-2099-1000",
        "package": "libdemo",
        "image": IMAGE,
        "severity": "HIGH",
        "reason": "Reached only through a code path the add-on never enables.",
        "added": datetime.date(2026, 11, 20),
        "expires": datetime.date(2027, 2, 1),
        "approved_by": "security officer",
    }
    base.update(overrides)
    return base


def review_problems(raw: dict) -> list[str]:
    """Problems with the review state of one raw exception entry.

    An entry is either waiting for the countersignature (`pending (<who signs>)`,
    which the gate lists without waiving anything) or names the role that
    countersigned it. Both are valid states of the file; an empty approval or the
    template placeholder is not. Every entry expires at most MAX_DAYS after it
    was added.
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

    def run_gate(self, report_path: Path, exceptions: Path | None = None) -> tuple[int, str, str]:
        argv = [str(report_path), "--today", TODAY]
        argv += ["--exceptions", str(exceptions or self.write_exceptions([]))]
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

    def test_image_is_matched_without_tag_and_digest(self) -> None:
        artifact = f"{IMAGE}:1.43.11-1.0.0@sha256:{'a' * 64}"
        code, _, _ = self.run_gate(
            self.write_report(report(vuln(), artifact=artifact)), self.write_exceptions([entry()])
        )
        self.assertEqual(code, 0)
        artifact = f"{IMAGE}@sha256:{'b' * 64}"
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
                [entry(added=datetime.date(2026, 9, 1), expires=datetime.date(2026, 11, 20))]
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("expired", err)

    def test_expired_exception_fails_even_when_unused(self) -> None:
        code, _, err = self.run_gate(
            self.write_report(report()),
            self.write_exceptions(
                [entry(added=datetime.date(2026, 9, 1), expires=datetime.date(2026, 11, 20))]
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("expired", err)

    def test_exception_expiring_today_still_holds(self) -> None:
        code, _, _ = self.run_gate(
            self.write_report(report(vuln())),
            self.write_exceptions(
                [entry(added=datetime.date(2026, 9, 1), expires=datetime.date(2026, 11, 21))]
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

    def test_tool_set_report_is_matched_by_its_path(self) -> None:
        tools = "tools/ci"
        code, _, _ = self.run_gate(
            self.write_report(report(vuln(), artifact=tools)),
            self.write_exceptions([entry(image=tools)]),
        )
        self.assertEqual(code, 0)

    def test_approved_exception_for_a_finding_without_fix_is_used(self) -> None:
        code, out, _ = self.run_gate(
            self.write_report(report(vuln(fixed=None))), self.write_exceptions([entry()])
        )
        self.assertEqual(code, 0)
        self.assertNotIn("unused", out)
        self.assertIn("CVE-2099-1000", out)

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
        stale = scan_gate.unpinned_entries(entries, {IMAGE})
        self.assertEqual([item.image for item in stale], ["docker.io/library/gone"])

    def test_entries_for_pinned_images_and_the_tool_set_pass(self) -> None:
        entries = scan_gate.load_exceptions(
            self.write_exceptions([entry(), entry(image=scan_gate.TOOL_SET)])
        )
        self.assertEqual(scan_gate.unpinned_entries(entries, {IMAGE}), [])

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
        code, out, _ = self.run_gate(
            self.write_report(report(vuln(), artifact=candidate)),
            self.write_exceptions([entry(image="ghcr.io/fidonis/qdrant-mcp-rbac")]),
        )
        self.assertEqual(code, 0, out)
        self.assertNotIn("unused", out)

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

    def test_repository_exceptions_carry_a_review_state_and_expire_in_time(self) -> None:
        import yaml

        data = yaml.safe_load(REPO_EXCEPTIONS.read_text(encoding="utf-8"))
        for raw in data["exceptions"] or []:
            with self.subTest(id=raw["id"], package=raw["package"], image=raw["image"]):
                self.assertEqual(review_problems(raw), [])

    def test_review_check_accepts_pending_and_countersigned_entries(self) -> None:
        base = {"added": datetime.date(2026, 11, 24), "expires": datetime.date(2027, 2, 22)}
        for approval in (
            "pending (security officer countersignature)",
            "security officer",
            "Security officer, 2026-11-30",
        ):
            with self.subTest(approval=approval):
                self.assertEqual(review_problems({**base, "approved_by": approval}), [])

    def test_review_check_rejects_missing_approval_placeholders_and_long_validity(self) -> None:
        base = {"added": datetime.date(2026, 11, 24), "expires": datetime.date(2027, 2, 22)}
        cases = {
            "empty": {**base, "approved_by": "  "},
            "not text": {**base, "approved_by": None},
            "pending without approver": {**base, "approved_by": "pending"},
            "template placeholder": {**base, "approved_by": "<role that countersigned>"},
            "too long": {**base, "approved_by": "security officer", "expires": datetime.date(2027, 2, 23)},
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

    def test_validity_longer_than_ninety_days_is_rejected(self) -> None:
        self.assert_rejected(
            self.write_exceptions(
                [entry(added=datetime.date(2026, 11, 1), expires=datetime.date(2027, 2, 1))]
            )
        )

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
