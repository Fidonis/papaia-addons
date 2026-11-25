"""Tests for tools/paperless_upgrade.py; run with `python -m unittest discover -s tools`.

The live upgrade test itself is tools/test_paperless_upgrade_live.py (opt-in, needs Docker).
"""

from __future__ import annotations

import copy
import json
import re
import unittest
import zlib

import paperless_upgrade as pu

HEX = "0123456789abcdef" * 4


def snapshot() -> dict:
    return {
        "format": pu.SNAPSHOT_FORMAT,
        "version": "3.4.2",
        "documents": [
            {
                "id": 1, "title": "upgrade doc 1", "original": HEX, "archive": HEX[::-1],
                "tags": [3, 1], "owner": 2, "correspondent": 1, "document_type": 1,
                "storage_path": 1, "custom_fields": [[1, "value one"]],
                "notes": ["first note"],
                "view_users": [], "view_groups": [1], "change_users": [], "change_groups": [],
            },
            {
                "id": 2, "title": "upgrade doc 2", "original": HEX[::-1], "archive": None,
                "tags": [], "owner": None, "correspondent": None, "document_type": None,
                "storage_path": None, "custom_fields": [], "notes": [],
                "view_users": [3], "view_groups": [], "change_users": [3], "change_groups": [],
            },
        ],
        "tags": [
            {"id": 1, "name": "root", "parent": None},
            {"id": 2, "name": "middle", "parent": 1},
            {"id": 3, "name": "leaf", "parent": 2},
        ],
        "objects": {
            "correspondents": [{"id": 1, "name": "c"}],
            "document_types": [{"id": 1, "name": "t"}],
            "custom_fields": [{"id": 1, "name": "f"}],
            "storage_paths": [{"id": 1, "name": "p"}],
            "saved_views": [{"id": 1, "name": "v"}],
        },
        "users": [
            {"id": 2, "username": "upgrade-a", "groups": [1], "is_superuser": False,
             "permissions": ["view_document"]},
            {"id": 3, "username": "upgrade-b", "groups": [], "is_superuser": False,
             "permissions": ["view_document"]},
        ],
        "groups": [{"id": 1, "name": "upgrade-group", "permissions": ["view_document"]}],
        "searches": {"s1": [1], "s2": [1, 2], "s3": []},
        "visible": {"upgrade-a": [1], "upgrade-b": [2]},
    }


class ParseSnapshotTest(unittest.TestCase):
    def test_a_valid_snapshot_is_accepted_and_normalised(self) -> None:
        got = pu.parse_snapshot(snapshot())
        self.assertEqual(got["documents"][0]["tags"], [1, 3], "lists of ids are sorted")
        self.assertEqual(got["searches"]["s2"], [1, 2])

    def test_it_round_trips_through_json(self) -> None:
        got = pu.parse_snapshot(json.loads(json.dumps(snapshot())))
        self.assertEqual(pu.compare(got, got), [])

    def test_unknown_fields_are_rejected(self) -> None:
        for path in ((), ("documents", 0), ("tags", 0), ("users", 0), ("groups", 0), ("objects",)):
            with self.subTest(path=path):
                raw = snapshot()
                node = raw
                for key in path:
                    node = node[key]
                node["extra"] = 1
                with self.assertRaises(ValueError):
                    pu.parse_snapshot(raw)

    def test_missing_fields_are_rejected(self) -> None:
        raw = snapshot()
        del raw["documents"][0]["archive"]
        with self.assertRaises(ValueError):
            pu.parse_snapshot(raw)

    def test_wrong_types_and_values_are_rejected(self) -> None:
        cases = [
            ("format", "other/1"),
            ("documents", "x"),
            ("searches", {"s1": ["1"]}),
            ("searches", {"bad key!": []}),
            ("visible", {"upgrade-a": [0]}),
            ("visible", {"x" * 300: []}),
            ("version", "3.4.2; rm"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=str(value)[:30]):
                raw = snapshot()
                raw[key] = value
                with self.assertRaises(ValueError):
                    pu.parse_snapshot(raw)
        doc_cases = [
            ("id", True), ("id", -1), ("original", "xyz"), ("archive", 5),
            ("owner", "2"), ("tags", [1, 1]), ("custom_fields", [[1]]),
            ("notes", [1]), ("title", "x" * 2000),
        ]
        for key, value in doc_cases:
            with self.subTest(document_field=key):
                raw = snapshot()
                raw["documents"][0][key] = value
                with self.assertRaises(ValueError):
                    pu.parse_snapshot(raw)

    def test_duplicate_ids_are_rejected(self) -> None:
        raw = snapshot()
        raw["documents"][1]["id"] = 1
        with self.assertRaises(ValueError):
            pu.parse_snapshot(raw)

    def test_a_non_object_is_rejected(self) -> None:
        for raw in (None, [], "x", 3):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pu.parse_snapshot(raw)


class CompareTest(unittest.TestCase):
    def changed(self, change) -> list[str]:
        before = pu.parse_snapshot(snapshot())
        raw = snapshot()
        change(raw)
        after = pu.parse_snapshot(raw)
        return pu.compare(before, after)

    def test_identical_snapshots_have_no_finding(self) -> None:
        self.assertEqual(self.changed(lambda raw: None), [])

    def test_the_version_is_not_compared(self) -> None:
        self.assertEqual(self.changed(lambda raw: raw.update(version="3.5.1")), [])

    def test_each_document_field_is_checked(self) -> None:
        changes = {
            "title": "renamed", "original": "f" * 64, "archive": None, "tags": [1],
            "owner": None, "correspondent": None, "document_type": None, "storage_path": None,
            "custom_fields": [[1, "other"]], "notes": [], "view_users": [2],
            "view_groups": [], "change_users": [2], "change_groups": [1],
        }
        for key, value in changes.items():
            with self.subTest(field=key):
                got = self.changed(lambda raw, k=key, v=value: raw["documents"][0].update({k: v}))
                self.assertEqual(got, [f"document 1: {key} differs"])

    def test_an_unreadable_file_is_a_finding(self) -> None:
        got = self.changed(lambda raw: raw["documents"][1].update(original=None))
        self.assertEqual(got, ["document 2: original differs"])

    def test_a_missing_and_an_extra_document_are_named(self) -> None:
        got = self.changed(lambda raw: raw["documents"].pop(1))
        self.assertEqual(got, ["document 2: missing after the upgrade"])
        extra = copy.deepcopy(snapshot()["documents"][1])
        extra["id"] = 9
        got = self.changed(lambda raw: raw["documents"].append(extra))
        self.assertEqual(got, ["document 9: not there before the upgrade"])

    def test_the_tag_tree_is_checked(self) -> None:
        got = self.changed(lambda raw: raw["tags"][2].update(parent=1))
        self.assertEqual(got, ["tag 3: parent differs"])
        got = self.changed(lambda raw: raw["tags"].pop(0))
        self.assertIn("tag 1: missing after the upgrade", got)

    def test_searches_visibility_users_groups_and_objects_are_checked(self) -> None:
        cases = [
            (lambda raw: raw["searches"].update(s2=[1]), ["search s2: results differ"]),
            (lambda raw: raw["searches"].pop("s3"), ["search s3: missing after the upgrade"]),
            (lambda raw: raw["visible"].update({"upgrade-a": [1, 2]}),
             ["user upgrade-a: visible documents differ"]),
            (lambda raw: raw["users"][0].update(groups=[]), ["user 2: groups differs"]),
            (lambda raw: raw["users"][1].update(is_superuser=True), ["user 3: is_superuser differs"]),
            (lambda raw: raw["users"][1].update(permissions=[]), ["user 3: permissions differs"]),
            (lambda raw: raw["groups"][0].update(permissions=[]), ["group 1: permissions differs"]),
            (lambda raw: raw["objects"]["saved_views"].clear(),
             ["saved_view 1: missing after the upgrade"]),
            (lambda raw: raw["objects"]["custom_fields"][0].update(name="g"),
             ["custom_field 1: name differs"]),
        ]
        for change, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.changed(change), expected)

    def test_findings_never_carry_content(self) -> None:
        def scramble(raw: dict) -> None:
            doc = raw["documents"][0]
            doc.update(title="secret title", notes=["secret note"],
                       custom_fields=[[1, "secret value"]])
            raw["tags"][0]["name"] = "secret tag"
        got = self.changed(scramble)
        self.assertTrue(got)
        self.assertNotIn("secret", " ".join(got))


class CountsTest(unittest.TestCase):
    def test_counts_hold_numbers_only(self) -> None:
        got = pu.counts(pu.parse_snapshot(snapshot()))
        self.assertEqual(got, {
            "documents": 2, "archive_files": 1, "notes": 1, "tags": 3, "tag_children": 2,
            "users": 2, "groups": 1, "searches": 3, "search_hits": 3,
            "correspondents": 1, "document_types": 1, "custom_fields": 1,
            "storage_paths": 1, "saved_views": 1,
        })


class MigrationLinesTest(unittest.TestCase):
    def test_applied_migrations_are_extracted(self) -> None:
        lines = [
            "paperless-1  | Apply all migrations: account, documents",
            "paperless-1  |   Applying documents.1070_tag_tree... OK",
            "  Applying paperless_mail.0030_x_y... OK",
            "  Applying documents.1071_broken... FAILED",
            "Applying documents.bad name... OK",
            "some document content Applying x.0001_y... OK and more",
        ]
        log = "\n".join(lines)
        self.assertEqual(pu.migration_lines(log),
                         ["documents.1070_tag_tree", "paperless_mail.0030_x_y"])

    def test_no_migration_gives_an_empty_list(self) -> None:
        self.assertEqual(pu.migration_lines("No migrations to apply.\n"), [])


class TextPdfTest(unittest.TestCase):
    def test_the_pdf_is_well_formed_and_holds_the_text(self) -> None:
        data = pu.text_pdf(["Upgrade smoke quillmarsh", "second line (with) parens"])
        self.assertTrue(data.startswith(b"%PDF-1.4\n"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        start = int(re.search(rb"startxref\n(\d+)\n", data).group(1))
        self.assertEqual(data[start:start + 4], b"xref")
        table = data[start:].split(b"\n")
        count = int(table[1].split()[1])
        offsets = [int(line.split()[0]) for line in table[3:2 + count]]
        for number, offset in enumerate(offsets, start=1):
            self.assertTrue(data[offset:].startswith(f"{number} 0 obj".encode()), number)
        stream = re.search(rb"stream\n(.*?)\nendstream", data, re.DOTALL).group(1)
        text = zlib.decompress(stream)
        self.assertIn(b"(Upgrade smoke quillmarsh) Tj", text)
        self.assertIn(rb"(second line \(with\) parens) Tj", text)

    def test_only_short_printable_ascii_lines_are_accepted(self) -> None:
        for lines in ([], ["ä"], ["a\nb"], ["x" * 200], ["ok"] * 60):
            with self.subTest(lines=str(lines)[:20]), self.assertRaises(ValueError):
                pu.text_pdf(lines)


class MultipartTest(unittest.TestCase):
    def test_the_body_holds_the_fields_and_the_file(self) -> None:
        body, content_type = pu.multipart({"title": "t1"}, "doc.pdf", b"%PDF-1.4 x")
        boundary = content_type.split("boundary=", 1)[1]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(f"--{boundary}\r\n".encode(), body)
        self.assertIn(b'Content-Disposition: form-data; name="title"\r\n\r\nt1\r\n', body)
        self.assertIn(b'name="document"; filename="doc.pdf"', body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_unsafe_names_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pu.multipart({'ti"tle': "x"}, "doc.pdf", b"x")
        with self.assertRaises(ValueError):
            pu.multipart({"title": "x"}, "../doc.pdf", b"x")


if __name__ == "__main__":
    unittest.main()
