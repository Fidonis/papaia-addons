"""Helpers of the Paperless upgrade test (tools/test_paperless_upgrade_live.py).

A snapshot is what the live test reads from the Paperless API before and after the image
step: documents with the SHA-256 of their original and archive files (null when a file
cannot be read), the tag tree, users,
groups, object permissions, the results of fixed full-text searches and the documents each
user sees. `parse_snapshot` checks one strictly (it comes out of a container), `compare`
names every difference by kind, id or user name and field, never by content, and `counts`
gives the numbers for the protocol. `text_pdf` and `multipart` build the synthetic uploads.
"""

from __future__ import annotations

import re
import secrets
import zlib
from typing import Any

SNAPSHOT_FORMAT = "papaia.paperless-upgrade-snapshot/1"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,149}")
_KEY = re.compile(r"[a-z0-9_]{1,32}")
_PERMISSION = re.compile(r"[a-z_]{1,100}")
_VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
_MIGRATION = re.compile(
    r"^(?:\S+\s+\|\s)?\s*Applying ([a-z_]{1,64}\.[0-9]{4}_[A-Za-z0-9_]{1,100})\.\.\. OK\s*$"
)
_TEXT_MAX = 1024


class SnapshotError(ValueError):
    """A snapshot, an upload or a field that does not have the expected shape."""

_DOCUMENT_FIELDS = (
    "title", "original", "archive", "tags", "owner", "correspondent", "document_type",
    "storage_path", "custom_fields", "notes", "view_users", "view_groups", "change_users",
    "change_groups",
)
_OBJECT_KINDS = {
    "correspondents": "correspondent",
    "document_types": "document_type",
    "custom_fields": "custom_field",
    "storage_paths": "storage_path",
    "saved_views": "saved_view",
}


# --- strict reading -----------------------------------------------------------------------

def _object(value: Any, keys: tuple[str, ...], where: str) -> dict:
    if not isinstance(value, dict):
        raise SnapshotError(f"{where}: not an object")
    if set(value) != set(keys):
        raise SnapshotError(f"{where}: expected exactly the fields {sorted(keys)}")
    return value


def _id(value: Any, where: str) -> int:
    if type(value) is not int or not 0 < value < 2**31:
        raise SnapshotError(f"{where}: not a positive id")
    return value


def _optional_id(value: Any, where: str) -> int | None:
    return None if value is None else _id(value, where)


def _ids(value: Any, where: str) -> list[int]:
    if not isinstance(value, list):
        raise SnapshotError(f"{where}: not a list")
    ids = [_id(item, where) for item in value]
    if len(set(ids)) != len(ids):
        raise SnapshotError(f"{where}: duplicate id")
    return sorted(ids)


def _text(value: Any, where: str, limit: int = _TEXT_MAX) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise SnapshotError(f"{where}: not a string of at most {limit} characters")
    return value


def _match(pattern: re.Pattern, value: Any, where: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SnapshotError(f"{where}: malformed")
    return value


def _list(value: Any, where: str) -> list:
    if not isinstance(value, list) or len(value) > 10_000:
        raise SnapshotError(f"{where}: not a list")
    return value


def _unique(items: list[dict], where: str) -> list[dict]:
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise SnapshotError(f"{where}: duplicate id")
    return sorted(items, key=lambda item: item["id"])


def _document(raw: Any) -> dict:
    where = "document"
    doc = _object(raw, ("id", *_DOCUMENT_FIELDS), where)
    fields = []
    for pair in _list(doc["custom_fields"], "custom_fields"):
        if not isinstance(pair, list) or len(pair) != 2:
            raise SnapshotError("custom_fields: expected [field id, value]")
        value = None if pair[1] is None else _text(pair[1], "custom field value")
        fields.append([_id(pair[0], "custom field"), value])
    archive = doc["archive"]
    return {
        "id": _id(doc["id"], where),
        "title": _text(doc["title"], "title"),
        "original": None if doc["original"] is None else _match(_HEX64, doc["original"],
                                                                "original"),
        "archive": None if archive is None else _match(_HEX64, archive, "archive"),
        "tags": _ids(doc["tags"], "tags"),
        "owner": _optional_id(doc["owner"], "owner"),
        "correspondent": _optional_id(doc["correspondent"], "correspondent"),
        "document_type": _optional_id(doc["document_type"], "document_type"),
        "storage_path": _optional_id(doc["storage_path"], "storage_path"),
        "custom_fields": sorted(fields, key=lambda pair: (pair[0], pair[1] or "")),
        "notes": sorted(_text(note, "note") for note in _list(doc["notes"], "notes")),
        **{key: _ids(doc[key], key)
           for key in ("view_users", "view_groups", "change_users", "change_groups")},
    }


def _permissions(value: Any, where: str) -> list[str]:
    names = [_match(_PERMISSION, item, where) for item in _list(value, where)]
    return sorted(set(names))


def parse_snapshot(raw: Any) -> dict:
    """Check a snapshot field by field and return it with every list in a fixed order."""
    snap = _object(raw, ("format", "version", "documents", "tags", "objects", "users",
                         "groups", "searches", "visible"), "snapshot")
    if snap["format"] != SNAPSHOT_FORMAT:
        raise SnapshotError("snapshot: unknown format")
    objects = _object(snap["objects"], tuple(_OBJECT_KINDS), "objects")
    searches = snap["searches"]
    visible = snap["visible"]
    if not isinstance(searches, dict) or not isinstance(visible, dict):
        raise SnapshotError("searches and visible: not objects")
    return {
        "format": SNAPSHOT_FORMAT,
        "version": _match(_VERSION, snap["version"], "version"),
        "documents": _unique([_document(d) for d in _list(snap["documents"], "documents")],
                             "documents"),
        "tags": _unique([
            {"id": _id(t["id"], "tag"), "name": _text(t["name"], "tag name", 255),
             "parent": _optional_id(t["parent"], "tag parent")}
            for t in (_object(t, ("id", "name", "parent"), "tag")
                      for t in _list(snap["tags"], "tags"))
        ], "tags"),
        "objects": {
            kind: _unique([
                {"id": _id(o["id"], kind), "name": _text(o["name"], kind, 255)}
                for o in (_object(o, ("id", "name"), kind) for o in _list(objects[kind], kind))
            ], kind)
            for kind in _OBJECT_KINDS
        },
        "users": _unique([
            {"id": _id(u["id"], "user"), "username": _match(_NAME, u["username"], "username"),
             "groups": _ids(u["groups"], "user groups"),
             "is_superuser": _bool(u["is_superuser"], "is_superuser"),
             "permissions": _permissions(u["permissions"], "user permissions")}
            for u in (_object(u, ("id", "username", "groups", "is_superuser", "permissions"),
                              "user") for u in _list(snap["users"], "users"))
        ], "users"),
        "groups": _unique([
            {"id": _id(g["id"], "group"), "name": _text(g["name"], "group name", 255),
             "permissions": _permissions(g["permissions"], "group permissions")}
            for g in (_object(g, ("id", "name", "permissions"), "group")
                      for g in _list(snap["groups"], "groups"))
        ], "groups"),
        "searches": {_match(_KEY, key, "search key"): _ids(ids, "search results")
                     for key, ids in searches.items()},
        "visible": {_match(_NAME, key, "user name"): _ids(ids, "visible documents")
                    for key, ids in visible.items()},
    }


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise SnapshotError(f"{where}: not a boolean")
    return value


# --- comparison ---------------------------------------------------------------------------

def _compare_records(kind: str, before: list[dict], after: list[dict]) -> list[str]:
    old = {item["id"]: item for item in before}
    new = {item["id"]: item for item in after}
    findings = []
    for key in sorted(old.keys() | new.keys()):
        if key not in new:
            findings.append(f"{kind} {key}: missing after the upgrade")
        elif key not in old:
            findings.append(f"{kind} {key}: not there before the upgrade")
        else:
            findings += [f"{kind} {key}: {field} differs" for field in old[key]
                         if field != "id" and old[key][field] != new[key][field]]
    return findings


def _compare_map(kind: str, what: str, before: dict, after: dict) -> list[str]:
    findings = []
    for key in sorted(before.keys() | after.keys()):
        if key not in after:
            findings.append(f"{kind} {key}: missing after the upgrade")
        elif key not in before:
            findings.append(f"{kind} {key}: not there before the upgrade")
        elif before[key] != after[key]:
            findings.append(f"{kind} {key}: {what} differ")
    return findings


def compare(before: dict, after: dict) -> list[str]:
    """Every difference between two parsed snapshots, by kind, id or user and field only."""
    findings = _compare_records("document", before["documents"], after["documents"])
    findings += _compare_records("tag", before["tags"], after["tags"])
    for kind, label in _OBJECT_KINDS.items():
        findings += _compare_records(label, before["objects"][kind], after["objects"][kind])
    findings += _compare_records("user", before["users"], after["users"])
    findings += _compare_records("group", before["groups"], after["groups"])
    findings += _compare_map("search", "results", before["searches"], after["searches"])
    findings += _compare_map("user", "visible documents", before["visible"], after["visible"])
    return findings


def counts(snap: dict) -> dict[str, int]:
    """The numbers of a snapshot for the protocol; no name, title or content."""
    docs = snap["documents"]
    return {
        "documents": len(docs),
        "archive_files": sum(1 for d in docs if d["archive"] is not None),
        "notes": sum(len(d["notes"]) for d in docs),
        "tags": len(snap["tags"]),
        "tag_children": sum(1 for t in snap["tags"] if t["parent"] is not None),
        "users": len(snap["users"]),
        "groups": len(snap["groups"]),
        "searches": len(snap["searches"]),
        "search_hits": sum(len(ids) for ids in snap["searches"].values()),
        **{kind: len(snap["objects"][kind]) for kind in _OBJECT_KINDS},
    }


def migration_lines(log: str) -> list[str]:
    """`app.migration` of each migration Django reports as applied; nothing else of the log."""
    found = []
    for line in log.splitlines():
        match = _MIGRATION.match(line)
        if match:
            found.append(match.group(1))
    return found


# --- synthetic uploads --------------------------------------------------------------------

def _printable(value: str, limit: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit or not all(
        " " <= ch <= "~" for ch in value
    ):
        raise SnapshotError(f"expected 1 to {limit} printable ASCII characters")
    return value


def text_pdf(lines: list[str]) -> bytes:
    """A one-page PDF whose text layer holds the given lines (Helvetica, uncompressed font)."""
    if not 0 < len(lines) <= 50:
        raise SnapshotError("expected 1 to 50 lines")
    ops = ["BT", "/F1 12 Tf", "72 760 Td", "14 TL"]
    for line in lines:
        text = _printable(line, 120)
        text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({text}) Tj T*")
    ops.append("ET")
    stream = zlib.compress("\n".join(ops).encode("ascii"))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
        + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref)
    return bytes(out)


_FIELD = re.compile(r"[A-Za-z0-9_]{1,64}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.[a-z]{1,8}")


def multipart(fields: dict[str, str], filename: str, data: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body with text fields and one file in the field `document`."""
    if not _FILENAME.fullmatch(filename):
        raise SnapshotError("malformed file name")
    boundary = "upgrade-" + secrets.token_hex(16)
    out = bytearray()
    for name, value in fields.items():
        if not _FIELD.fullmatch(name):
            raise SnapshotError("malformed field name")
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{_printable(value, 255)}\r\n").encode("ascii")
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode("ascii")
    out += data + f"\r\n--{boundary}--\r\n".encode("ascii")
    return bytes(out), f"multipart/form-data; boundary={boundary}"
