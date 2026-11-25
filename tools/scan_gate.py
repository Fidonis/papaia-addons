#!/usr/bin/env python3
"""Fail an image scan on findings that need action.

Reads a Trivy report (`trivy image --format json`, schema version 2, written
complete: every severity, fixed and unfixed) and the repository's exception
list `security/exceptions.yaml`, and decides:

* CRITICAL or HIGH with a fixed version available: fails unless an approved
  exception covers it.
* CRITICAL or HIGH without a fixed version: does not fail; it is listed in the
  output, in the `--no-fix` list and counted in the `--summary` table, and
  reviewed once a month (docs/development.md, "Vulnerability scan"). It needs
  no exception; an entry that matches only such a finding waives nothing and
  is reported for removal.
* MEDIUM, LOW and UNKNOWN are not gated.
* An expired exception fails the run whether or not it still matches.

An exception covers a finding when `id`, `package`, `severity` and `image`
(the repository, without tag or digest; for a release candidate from a local
test registry the repository it is published to; for a scan of the CI tool
set the scanned path, `tools/ci`) are equal, and only once it is approved: an
entry whose `approved_by` starts with `pending` is listed but waives nothing.
Each entry carries a reason and the date it was added.

* Own images (`ghcr.io/fidonis/...`) and the CI tool set: expiry at most 90
  days after `added`, no `digest`.
* Third-party images: the entry also names `digest`, the sha256 of the pinned
  image index, and expires at most 14 days after `added`. It covers a finding
  only in a scan of that digest, and the run is refused while the digest is no
  longer pinned by an add-on default (`--root`).

See `security/exceptions.yaml` for the format. Titles and descriptions of the
report are never printed.

Exit codes: 0 nothing to act on, 1 failing findings or expired exceptions,
2 the report, the exception list or an argument is unreadable or invalid.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXCEPTIONS = REPO_ROOT / "security" / "exceptions.yaml"
# The scan of the CI tool set reports this path as its artifact.
TOOL_SET = "tools/ci"
# A release candidate in a local test registry (`127.0.0.1:<port>/unpublished/<name>`)
# is published as `<PUBLISHED_NAMESPACE>/<name>`; exceptions name the published
# repository, so they hold for the candidate and the release alike.
PUBLISHED_NAMESPACE = "ghcr.io/fidonis"
CANDIDATE = re.compile(
    r"^(?:localhost|127(?:\.[0-9]{1,3}){3})(?::[0-9]{1,5})?/unpublished/([a-z0-9]+(?:[._-][a-z0-9]+)*)$"
)

# Own images: the repository's published namespace (candidates map onto it).
OWN_PREFIX = PUBLISHED_NAMESPACE + "/"

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
EXCEPTABLE = ("CRITICAL", "HIGH")
MAX_DAYS = 90
# A third-party image whose distribution ships the fix but whose upstream image
# is not rebuilt yet: one digest, at most this many days.
THIRD_PARTY_MAX_DAYS = 14
MIN_REASON = 20
MAX_REPORT_BYTES = 256 * 1024 * 1024
# One entry per waived CRITICAL or HIGH finding with a fix of every pinned image.
MAX_EXCEPTIONS_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 4000
ENTRY_FIELDS = ("id", "package", "image", "severity", "reason", "added", "expires", "approved_by")
# Required for third-party images, refused for the own images and the CI tool set.
OPTIONAL_FIELDS = ("digest",)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PLATFORM = re.compile(r"^[a-z0-9]{1,16}/[a-z0-9_]{1,16}(?:/[a-z0-9]{1,8})?$")
TOP_FIELDS = ("version", "exceptions")
VULN_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9._:-]{1,80}$")
PACKAGE = re.compile(r"^[^\s]{1,214}$")
# A repository without tag or digest: `[registry[:port]/]path`.
IMAGE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$")


class GateError(Exception):
    """The report or the exception list cannot be used."""


@dataclasses.dataclass(frozen=True)
class Finding:
    vuln_id: str
    package: str
    installed: str
    severity: str
    fixed: str

    @property
    def fixable(self) -> bool:
        return bool(self.fixed)

    def describe(self) -> str:
        fix = f"fixed in {self.fixed}" if self.fixed else "no fix yet"
        return f"{self.severity} {self.vuln_id} {self.package} {self.installed} ({fix})"


@dataclasses.dataclass(frozen=True)
class Entry:
    vuln_id: str
    package: str
    image: str
    severity: str
    added: datetime.date
    expires: datetime.date
    approved: bool = True
    digest: str = ""

    def key(self) -> tuple[str, str, str, str]:
        return (self.image, self.vuln_id, self.package, self.severity)


def is_third_party(image: str) -> bool:
    """`image` (as an exception names it) is neither an own image nor the CI tool set."""
    return image != TOOL_SET and not image.startswith(OWN_PREFIX)


def repository(artifact: str) -> str:
    """`registry/path:tag@digest` -> `registry/path`."""
    name = artifact.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    if ":" in last:
        last = last.split(":", 1)[0]
    return f"{head}/{last}" if head else last


def artifact_digest(artifact: str) -> str:
    """`registry/path:tag@sha256:...` -> `sha256:...`, or "" without a valid digest."""
    _, sep, digest = artifact.partition("@")
    return digest if sep and DIGEST.match(digest) else ""


def exception_image(repo: str) -> str:
    """The repository an exception names for `repo`: the published one for a candidate."""
    match = CANDIDATE.match(repo)
    return f"{PUBLISHED_NAMESPACE}/{match.group(1)}" if match else repo


def _text(value: Any, where: str, *, required: bool = True, limit: int = 1024) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value) or len(value) > limit:
        raise GateError(f"{where}: expected a string of at most {limit} characters")
    return value


def load_report(path: Path) -> tuple[str, list[Finding]]:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise GateError(f"{path}: report larger than {MAX_REPORT_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("SchemaVersion") != 2:
        raise GateError(f"{path}: not a Trivy JSON report of schema version 2")
    artifact = _text(data.get("ArtifactName"), f"{path}: ArtifactName")
    results = data.get("Results", [])
    if not isinstance(results, list):
        raise GateError(f"{path}: Results must be a list")
    findings: list[Finding] = []
    for i, result in enumerate(results):
        if not isinstance(result, dict):
            raise GateError(f"{path}: Results[{i}] must be an object")
        vulns = result.get("Vulnerabilities") or []
        if not isinstance(vulns, list):
            raise GateError(f"{path}: Results[{i}].Vulnerabilities must be a list")
        for j, vuln in enumerate(vulns):
            where = f"{path}: Results[{i}].Vulnerabilities[{j}]"
            if not isinstance(vuln, dict):
                raise GateError(f"{where} must be an object")
            severity = _text(vuln.get("Severity"), f"{where}.Severity")
            if severity not in SEVERITIES:
                raise GateError(f"{where}.Severity: unknown severity {severity!r}")
            findings.append(
                Finding(
                    vuln_id=_text(vuln.get("VulnerabilityID"), f"{where}.VulnerabilityID"),
                    package=_text(vuln.get("PkgName"), f"{where}.PkgName"),
                    installed=_text(
                        vuln.get("InstalledVersion"), f"{where}.InstalledVersion", required=False
                    ),
                    severity=severity,
                    fixed=_text(vuln.get("FixedVersion"), f"{where}.FixedVersion", required=False),
                )
            )
    return artifact, findings


def _date(value: Any, where: str) -> datetime.date:
    # YAML reads an unquoted ISO date as a date; a quoted one stays text and is refused,
    # so a typo cannot turn into a string that never expires.
    if not isinstance(value, datetime.date) or isinstance(value, datetime.datetime):
        raise GateError(f"{where}: expected an unquoted date (YYYY-MM-DD)")
    return value


def load_exceptions(path: Path) -> list[Entry]:
    """Load and validate the list; expiry is judged in evaluate(), so an expired entry is reported."""
    try:
        if path.stat().st_size > MAX_EXCEPTIONS_BYTES:
            raise GateError(f"{path}: larger than {MAX_EXCEPTIONS_BYTES} bytes")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise GateError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GateError(f"{path}: expected a mapping with version and exceptions")
    unknown = sorted(set(data) - set(TOP_FIELDS))
    if unknown:
        raise GateError(f"{path}: unknown field(s) {', '.join(map(str, unknown))}")
    if data.get("version") != 1:
        raise GateError(f"{path}: version must be 1")
    entries = data.get("exceptions")
    if entries is None:
        entries = []
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise GateError(f"{path}: exceptions must be a list of at most {MAX_ENTRIES} entries")
    loaded: list[Entry] = []
    seen: set[tuple[str, str, str, str]] = set()
    for i, raw in enumerate(entries):
        where = f"{path}: exceptions[{i}]"
        if not isinstance(raw, dict):
            raise GateError(f"{where}: expected a mapping")
        unknown = sorted(set(raw) - set(ENTRY_FIELDS) - set(OPTIONAL_FIELDS))
        if unknown:
            raise GateError(f"{where}: unknown field(s) {', '.join(map(str, unknown))}")
        missing = [name for name in ENTRY_FIELDS if name not in raw]
        if missing:
            raise GateError(f"{where}: missing field(s) {', '.join(missing)}")
        vuln_id = _text(raw["id"], f"{where}.id", limit=96)
        if not VULN_ID.match(vuln_id):
            raise GateError(f"{where}.id: not a vulnerability id")
        package = _text(raw["package"], f"{where}.package", limit=214)
        if not PACKAGE.match(package):
            raise GateError(f"{where}.package: not a package name")
        image = _text(raw["image"], f"{where}.image", limit=255)
        if not IMAGE.match(image):
            raise GateError(f"{where}.image: expected a repository without tag or digest")
        if CANDIDATE.match(image):
            raise GateError(
                f"{where}.image: name the published repository {exception_image(image)}, "
                "not the test registry"
            )
        severity = _text(raw["severity"], f"{where}.severity", limit=16)
        if severity not in EXCEPTABLE:
            raise GateError(f"{where}.severity: one of {', '.join(EXCEPTABLE)}")
        reason = _text(raw["reason"], f"{where}.reason", limit=2000)
        if len(reason.strip()) < MIN_REASON:
            raise GateError(f"{where}.reason: at least {MIN_REASON} characters")
        approved_by = _text(raw["approved_by"], f"{where}.approved_by", limit=200)
        approved = bool(approved_by.strip()) and not approved_by.strip().lower().startswith("pending")
        added = _date(raw["added"], f"{where}.added")
        expires = _date(raw["expires"], f"{where}.expires")
        if expires < added:
            raise GateError(f"{where}: expires before it was added")
        if (expires - added).days > MAX_DAYS:
            raise GateError(f"{where}: valid for more than {MAX_DAYS} days")
        digest = ""
        if is_third_party(image):
            if "digest" not in raw:
                raise GateError(
                    f"{where}: a third-party image entry names the pinned digest (digest: sha256:...)"
                )
            digest = _text(raw["digest"], f"{where}.digest", limit=71)
            if not DIGEST.match(digest):
                raise GateError(f"{where}.digest: expected sha256: followed by 64 lower-case hex digits")
            if (expires - added).days > THIRD_PARTY_MAX_DAYS:
                raise GateError(
                    f"{where}: a third-party image entry is valid for at most "
                    f"{THIRD_PARTY_MAX_DAYS} days"
                )
        elif "digest" in raw:
            raise GateError(f"{where}.digest: only third-party image entries name a digest")
        item = Entry(vuln_id, package, image, severity, added, expires, approved, digest)
        if item.key() in seen:
            raise GateError(f"{where}: duplicate of an earlier entry")
        seen.add(item.key())
        loaded.append(item)
    return loaded


def pinned_repositories(root: Path) -> set[str]:
    """Repositories of the image defaults of the delivered add-ons under `root`."""
    import image_refs

    refs = image_refs.pinned_refs(image_refs.checked_addons(root))
    return {exception_image(repository(ref)) for ref in refs}


def unpinned_entries(entries: list[Entry], pinned: set[str]) -> list[Entry]:
    """Entries naming an image that no add-on default pins (nor the CI tool set)."""
    return [e for e in entries if e.image != TOOL_SET and e.image not in pinned]


def pinned_digests(refs: set[str]) -> set[tuple[str, str]]:
    """(exception image, digest) of each pinned reference."""
    return {(exception_image(repository(ref)), artifact_digest(ref)) for ref in refs}


def stale_digest_entries(entries: list[Entry], refs: set[str]) -> list[Entry]:
    """Third-party entries whose digest no add-on default pins any more."""
    pinned = pinned_digests(refs)
    return [e for e in entries if e.digest and (e.image, e.digest) not in pinned]


@dataclasses.dataclass
class Verdict:
    failing: list[Finding]
    excepted: list[Finding]
    unfixed: list[Finding]
    expired: list[Entry]
    unused: list[Entry]
    pending: list[Entry]
    blocked: list[Finding]
    not_needed: list[Entry]

    @property
    def ok(self) -> bool:
        return not self.failing and not self.expired


def evaluate(
    artifact: str, findings: list[Finding], exceptions: list[Entry], today: datetime.date
) -> Verdict:
    image = exception_image(repository(artifact))
    digest = artifact_digest(artifact)
    # A digest-scoped entry applies to a scan of that digest only.
    current = [
        e for e in exceptions if e.expires >= today and (not e.digest or e.digest == digest)
    ]
    valid = {e.key(): e for e in current if e.approved}
    waiting = {e.key(): e for e in current if not e.approved}
    used: set[tuple[str, str, str, str]] = set()
    no_fix_keys: set[tuple[str, str, str, str]] = set()
    failing: list[Finding] = []
    excepted: list[Finding] = []
    unfixed: list[Finding] = []
    blocked: list[Finding] = []
    for finding in findings:
        if finding.severity not in EXCEPTABLE:
            continue
        key = (image, finding.vuln_id, finding.package, finding.severity)
        if not finding.fixable:
            unfixed.append(finding)
            no_fix_keys.add(key)
            continue
        if key in valid:
            used.add(key)
            excepted.append(finding)
        else:
            failing.append(finding)
            if key in waiting:
                blocked.append(finding)
    expired = [e for e in exceptions if e.expires < today]
    not_needed = [e for e in valid.values() if e.key() not in used and e.key() in no_fix_keys]
    unused = [
        e for e in valid.values()
        if e.key() not in used and e.key() not in no_fix_keys and e.image == image
    ]
    pending = [e for e in waiting.values() if e.image == image]
    return Verdict(failing, excepted, unfixed, expired, unused, pending, blocked, not_needed)


def no_fix_lines(artifact: str, platform: str, verdict: Verdict) -> list[str]:
    """One tab-separated line per CRITICAL or HIGH finding without a fix."""
    return [
        "\t".join((artifact, platform, f.vuln_id, f.package, f.installed or "-", f.severity))
        for f in verdict.unfixed
    ]


def _cell(value: str) -> str:
    """`value` safe inside a Markdown table cell and a code span."""
    return re.sub(r"[^A-Za-z0-9._/:@+-]", "_", value)[:300]


def summary_table(artifact: str, platform: str, verdict: Verdict) -> str:
    return (
        "| Image | Platform | Failing | Excepted | Without fix |\n"
        "|---|---|---:|---:|---:|\n"
        f"| `{_cell(artifact)}` | {_cell(platform)} | {len(verdict.failing)} | "
        f"{len(verdict.excepted)} | {len(verdict.unfixed)} |\n"
    )


def _platform(value: str) -> str:
    if value != "-" and not PLATFORM.match(value):
        raise argparse.ArgumentTypeError("expected os/architecture[/variant], e.g. linux/amd64")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("report", type=Path, help="Trivy JSON report")
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT,
        help="repository whose add-on defaults pin the digests of third-party entries",
    )
    parser.add_argument(
        "--platform", type=_platform, default="-", help="scanned platform for the report, e.g. linux/amd64"
    )
    parser.add_argument(
        "--no-fix", type=Path, default=None,
        help="write the CRITICAL and HIGH findings without a fix here, one per line",
    )
    parser.add_argument(
        "--summary", type=Path, default=None,
        help="append the counts as a Markdown table here (CI: $GITHUB_STEP_SUMMARY)",
    )
    parser.add_argument(
        "--today", type=datetime.date.fromisoformat, default=None, help="YYYY-MM-DD (tests)"
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    today: datetime.date = args.today or datetime.datetime.now(datetime.UTC).date()
    try:
        artifact, findings = load_report(args.report)
        exceptions = load_exceptions(args.exceptions)
        if any(e.digest for e in exceptions):
            import image_refs

            refs = image_refs.pinned_refs(image_refs.checked_addons(args.root))
            stale = stale_digest_entries(exceptions, refs)
            if stale:
                names = "; ".join(f"{e.vuln_id} {e.package} {e.image}@{e.digest}" for e in stale)
                raise GateError(
                    f"{args.exceptions}: digest no longer pinned by an add-on default, "
                    f"re-check the entries against the scan of the new pin: {names}"
                )
    except GateError as exc:
        print(f"scan gate: {exc}", file=sys.stderr)
        return 2

    verdict = evaluate(artifact, findings, exceptions, today)
    for finding in verdict.unfixed:
        print(f"listed: {finding.describe()}")
    for finding in verdict.excepted:
        print(f"excepted: {finding.describe()}")
    for item in verdict.not_needed:
        print(
            f"exception not needed (finding has no fix): {item.vuln_id} {item.package} "
            f"{item.severity}; remove it, the finding is listed in the report"
        )
    for item in verdict.unused:
        print(f"unused exception: {item.vuln_id} {item.package} {item.severity}")
    for item in verdict.pending:
        print(f"exception pending approval: {item.vuln_id} {item.package} {item.severity}")
    for finding in verdict.failing:
        note = " (exception pending approval)" if finding in verdict.blocked else ""
        print(f"FAIL {artifact}: {finding.describe()}{note}", file=sys.stderr)
    for item in verdict.expired:
        print(
            f"FAIL exception {item.vuln_id} {item.package} expired on {item.expires.isoformat()}",
            file=sys.stderr,
        )
    print(
        f"{artifact}: {len(verdict.failing)} failing, {len(verdict.excepted)} excepted, "
        f"{len(verdict.unfixed)} without fix (CRITICAL/HIGH), {len(verdict.expired)} expired exceptions"
    )
    try:
        if args.no_fix is not None:
            lines = no_fix_lines(artifact, args.platform, verdict)
            args.no_fix.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        if args.summary is not None:
            with args.summary.open("a", encoding="utf-8") as handle:
                handle.write(summary_table(artifact, args.platform, verdict))
    except OSError as exc:
        print(f"scan gate: {exc}", file=sys.stderr)
        return 2
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
