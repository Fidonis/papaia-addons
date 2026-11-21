"""Tests for tools/exposes.py; run with `python -m unittest discover -s tools`."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import components
import exposes

REPO_ROOT = Path(__file__).resolve().parent.parent

# The generation table of the core's ADDON_API file (papaia 1.10.0), rows 6 to 12.
CORE_ADDON_API = """\
# Generations (all additive, so `min` stays 1). Binding meaning; the same table
# is ADDON_API_GENERATIONS in tools/lib/compat.py and in the contract snapshot.
#   6  `knowledge:` (ADR 0014)
#   7  `exposes:` (also `exposes.yaml` of core profiles), rendering of `services.json` and
#      `groups.json`, declared roles, exchange scopes `aud-<addon>`, service accounts,
#      `models.needs`; `keycloak`, `litellm`, `papaia-audit`, `papaia-egress` attachable
#      (ADR 0017, 0018)
#   8  `personal_settings:`; `me-web` and `prefs` attachable (ADR 0020)
#   12  add-on ingress `integration.nginx` (ADR 0023)
current=12
min=1
"""

COMPOSE = """\
services:
  app:
    image: ${DEMO_IMAGE:-docker.io/example/demo:1.2.3}
    volumes:
      - demo-data:/data
  app-mcp:
    image: ${DEMO_MCP_IMAGE:-docker.io/example/demo-mcp:0.1.0}
  queue:
    image: ${DEMO_QUEUE_IMAGE:-docker.io/example/queue:2.0.0}
    volumes:
      - demo-queue:/queue
volumes:
  demo-data:
  demo-queue:
"""

MANIFEST = """\
name: demo
version: 1.0.0
requires:
  # Generations, as in the core's table:
  #   7  `exposes:` (also `exposes.yaml` of core profiles), rendering of `services.json` and
  #      `groups.json`, declared roles, exchange scopes `aud-<addon>`, service accounts,
  #      `models.needs`; `keycloak`, `litellm`, `papaia-audit`, `papaia-egress` attachable
  #      (ADR 0017, 0018)
  #   12  add-on ingress `integration.nginx` (ADR 0023)
  addon_api: [12]
integration:
  keycloak:
    clients:
      - integration/mcp-demo.json
components:
  - id: demo
    name: Demo
    role: primary
    kind: upstream
    image_var: DEMO_IMAGE
    version: "1.2.3"
    source: https://example.com/demo
  - id: demo-mcp
    name: Demo MCP
    role: primary
    kind: fidonis
    image_var: DEMO_MCP_IMAGE
    version: "0.1.0"
    source: https://example.com/demo-mcp
  - id: queue
    name: Queue
    role: sidecar
    kind: upstream
    image_var: DEMO_QUEUE_IMAGE
    version: "2.0.0"
    source: https://example.com/queue
exposes:
  - id: demo.items
    kind: mcp
    title: {de: "Demo", en: "Demo"}
    address: {service: app-mcp, port: 8000, path: /mcp}
    audience: mcp-demo
    identity: [person]
    standard_operations: [describe_access, check_access]
    operations:
      - name: get_item
        version: 1
        title: {de: "Lesen", en: "Read"}
        via: tools
        effect: read
        retry: safe
        input_schema:
          type: object
          additionalProperties: false
          required: [id]
          properties:
            id: {type: integer, minimum: 1}
        output_schema: {type: object}
        data_class_max: internal
        resource: {type: item, id_field: id}
        rights_check: server
      - name: describe_access
        version: 1
        title: {de: "Rechte", en: "Access"}
        via: tools
        effect: read
        retry: safe
        input_schema: {type: object, additionalProperties: false}
        output_schema: {type: object}
        data_class_max: internal
        rights_check: server
      - name: check_access
        version: 1
        title: {de: "Prüfen", en: "Check"}
        via: tools
        effect: check
        retry: safe
        input_schema:
          type: object
          additionalProperties: false
          required: [resources, right]
          properties:
            resources: {type: array, maxItems: 100}
            right: {enum: [read, write, delete]}
        output_schema: {type: object}
        data_class_max: internal
        resource: {type: item, id_field: resources}
        rights_check: server
    requires:
      addon_api: [12]
    volumes:
      - name: demo-data
        backup: include
      - name: demo-queue
        backup: exclude
        reason: {de: "Warteschlange", en: "Queue"}
    export:
      exit: {format: demo.export/1, schema: schemas/demo-export-1.json}
    storage:
      personal_data: true
      retention: {until: deleted_by_person}
      categories:
        - id: items
          title: {de: "Einträge", en: "Items"}
          purpose: {de: "Ablage", en: "Filing"}
          legal_basis: {de: "Art. 6 Abs. 1 lit. f DSGVO", en: "Art. 6(1)(f) GDPR"}
          retention: {until: deleted_by_person}
          data_class: internal
          erasure: person_erase
      secrets:
        - {kind: database, location: demo-data}
"""

ENV = """\
# DEMO_IMAGE=docker.io/example/demo:1.2.3
# DEMO_MCP_IMAGE=docker.io/example/demo-mcp:0.1.0
# DEMO_QUEUE_IMAGE=docker.io/example/queue:2.0.0
"""

CHANGELOG = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- First release.\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def make_addon(root: Path, manifest: str = MANIFEST, compose: str = COMPOSE) -> Path:
    addon = root / "demo"
    write(addon / components.MANIFEST, manifest)
    write(addon / components.COMPOSE, compose)
    write(addon / components.ENV_EXAMPLE, ENV)
    write(addon / components.CHANGELOG, CHANGELOG)
    write(addon / "integration/mcp-demo.json", json.dumps({"clientId": "mcp-demo"}) + "\n")
    write(addon / "schemas/demo-export-1.json", json.dumps({"type": "object"}) + "\n")
    listed = components.load_manifest(addon).get("components") or []
    write(addon / components.README, "# demo\n\n" + components.render_block(listed) + "\n")
    return addon


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def problems(self, manifest: str = MANIFEST, compose: str = COMPOSE) -> list[str]:
        return components.check_addon(make_addon(self.root, manifest, compose))

    def assertReported(self, needle: str, manifest: str = MANIFEST, compose: str = COMPOSE) -> None:
        problems = self.problems(manifest, compose)
        self.assertTrue(any(needle in p for p in problems), problems)

    def test_consistent_addon_passes(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_manifest_without_exposes_is_left_to_its_own_migration(self) -> None:
        legacy = MANIFEST.split("exposes:\n", 1)[0].replace("  addon_api: [12]\n", "  addon_api: 2\n")
        self.assertEqual(self.problems(legacy), [])

    def test_undecided_marker_is_rejected_in_any_manifest(self) -> None:
        legacy = MANIFEST.split("exposes:\n", 1)[0]
        self.assertReported("@UNDECIDED@", legacy + "papaia_compat: \"@UNDECIDED@\"\n")

    def test_exposes_needs_a_list_of_generations_from_seven(self) -> None:
        self.assertReported(
            "requires.addon_api", MANIFEST.replace("  addon_api: [12]\n", "  addon_api: 2\n", 1)
        )
        self.assertReported(
            "requires.addon_api", MANIFEST.replace("  addon_api: [12]\n", "  addon_api: [6]\n", 1)
        )

    def test_entry_requires_must_equal_the_manifest_requires(self) -> None:
        self.assertReported(
            "demo.items: requires.addon_api",
            MANIFEST.replace("    requires:\n      addon_api: [12]\n", "    requires:\n      addon_api: [7]\n"),
        )

    def test_entry_without_requires_is_reported(self) -> None:
        manifest = MANIFEST.replace("    requires:\n      addon_api: [12]\n", "")
        self.assertNotEqual(manifest, MANIFEST)
        self.assertReported("demo.items: requires.addon_api", manifest)

    def test_every_declared_generation_needs_its_meaning_in_a_comment(self) -> None:
        self.assertReported(
            "generation 12",
            MANIFEST.replace("  #   12  add-on ingress `integration.nginx` (ADR 0023)\n", ""),
        )

    def test_address_service_must_be_a_compose_service(self) -> None:
        self.assertReported(
            "address.service `demo-mcp`", MANIFEST.replace("service: app-mcp", "service: demo-mcp")
        )

    def test_audience_must_be_a_shipped_keycloak_client(self) -> None:
        self.assertReported("audience `mcp-other`", MANIFEST.replace("mcp-demo\n", "mcp-other\n", 1))

    def test_operation_names_are_unique(self) -> None:
        self.assertReported(
            "operation `get_item` is declared twice", MANIFEST.replace("name: describe_access", "name: get_item")
        )

    def test_id_field_must_be_an_input_property(self) -> None:
        self.assertReported(
            "resource.id_field `item_id`", MANIFEST.replace("id_field: id}", "id_field: item_id}")
        )

    def test_every_named_volume_is_one_backup_item(self) -> None:
        missing = MANIFEST.replace(
            "      - name: demo-queue\n        backup: exclude\n        reason: {de: \"Warteschlange\", en: \"Queue\"}\n",
            "",
        )
        self.assertReported("volume `demo-queue` has no entry", missing)
        self.assertReported(
            "volumes[] names `demo-cache`", MANIFEST.replace("name: demo-queue", "name: demo-cache")
        )

    def test_secret_location_must_be_a_volume(self) -> None:
        self.assertReported(
            "storage.secrets[].location `demo-keys`",
            MANIFEST.replace("location: demo-data}", "location: demo-keys}"),
        )

    def test_exit_export_schema_must_exist(self) -> None:
        self.assertReported(
            "export.exit.schema `schemas/missing.json`",
            MANIFEST.replace("schemas/demo-export-1.json", "schemas/missing.json"),
        )

    def test_entry_ids_are_unique(self) -> None:
        entry = MANIFEST.split("exposes:\n", 1)[1]
        self.assertReported("`demo.items` is declared twice", MANIFEST + entry)


class GenerationTableTests(unittest.TestCase):
    def test_core_table_is_parsed_with_continuation_lines(self) -> None:
        table = exposes.core_generations(CORE_ADDON_API)
        self.assertEqual(sorted(table), [6, 7, 8, 12])
        self.assertTrue(table[7].startswith("`exposes:` (also"))
        self.assertTrue(table[7].endswith("attachable (ADR 0017, 0018)"))

    def test_manifest_comments_equal_the_core_table(self) -> None:
        self.assertEqual(exposes.compare_generations(MANIFEST, CORE_ADDON_API), [])

    def test_differing_meaning_is_reported(self) -> None:
        changed = MANIFEST.replace("add-on ingress", "ingress for add-ons")
        problems = exposes.compare_generations(changed, CORE_ADDON_API)
        self.assertTrue(any("generation 12" in p for p in problems), problems)

    def test_generation_unknown_to_the_core_is_reported(self) -> None:
        problems = exposes.compare_generations(MANIFEST, CORE_ADDON_API.replace("#   12", "#   13"))
        self.assertTrue(any("generation 12" in p for p in problems), problems)

    def test_cli_compares_against_a_core_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = Path(tmp) / "core"
            write(core / "ADDON_API", CORE_ADDON_API)
            addon = make_addon(Path(tmp))
            self.assertEqual(exposes.main(["--root", tmp, "generations", "--core", str(core)]), 0)
            write(core / "ADDON_API", CORE_ADDON_API.replace("(ADR 0023)", "(ADR 0099)"))
            self.assertEqual(exposes.main(["--root", tmp, "generations", "--core", str(core)]), 1)
            self.assertTrue(addon.is_dir())


class PaperlessTests(unittest.TestCase):
    """The paperless add-on is described once, in `exposes:`."""

    def setUp(self) -> None:
        self.path = REPO_ROOT / "paperless"
        self.manifest = components.load_manifest(self.path)
        self.text = (self.path / components.MANIFEST).read_text(encoding="utf-8")

    def entry(self) -> dict:
        entries = self.manifest.get("exposes")
        self.assertIsInstance(entries, list)
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_manifest_and_files_agree(self) -> None:
        self.assertEqual(components.check_addon(self.path), [])

    def test_requires_generation_twelve(self) -> None:
        self.assertEqual(self.manifest["requires"]["addon_api"], [12])
        self.assertEqual(self.entry()["requires"]["addon_api"], [12])
        self.assertEqual(self.manifest["papaia_compat"], ">=1.10.0")

    def test_generation_comments_equal_the_core_table(self) -> None:
        self.assertEqual(exposes.compare_generations(self.text, CORE_ADDON_API), [])
        self.assertEqual(sorted(exposes.generation_comments(self.text)), [7, 12])

    def test_tool_server_entry(self) -> None:
        entry = self.entry()
        self.assertEqual(entry["id"], "paperless.documents")
        self.assertEqual(entry["kind"], "mcp")
        self.assertEqual(entry["address"], {"service": "paperless-mcp", "port": 8000, "scheme": "http", "path": "/mcp"})
        self.assertEqual(entry["audience"], "mcp-paperless")
        self.assertEqual(entry["identity"], ["person", "service"])
        self.assertEqual(entry["standard_operations"], ["describe_access", "check_access"])

    def test_operations_cover_every_tool_of_the_server_with_its_effect(self) -> None:
        effects = {op["name"]: op["effect"] for op in self.entry()["operations"]}
        self.assertEqual(
            effects,
            {
                "search_documents": "read",
                "get_document": "read",
                "get_document_metadata": "read",
                "get_document_content": "read",
                "download_document": "read",
                "list_tags": "read",
                "list_correspondents": "read",
                "list_document_types": "read",
                "get_notes": "read",
                "upload_document": "write",
                "update_document": "write",
                "delete_document": "delete",
                "add_note": "write",
                "create_tag": "write",
                "create_correspondent": "write",
                "describe_access": "read",
                "check_access": "check",
            },
        )

    def test_every_operation_declares_its_highest_data_class(self) -> None:
        for op in self.entry()["operations"]:
            self.assertIsInstance(op.get("data_class_max"), str, op["name"])
            self.assertEqual(op["input_schema"].get("additionalProperties"), False, op["name"])

    def test_content_operations_carry_the_highest_class_of_the_archive(self) -> None:
        # A general archive can hold Art. 9 GDPR data and secrets under
        # Section 203 StGB; Paperless enforces no class limit.
        ops = {op["name"]: op for op in self.entry()["operations"]}
        for name in (
            "search_documents",
            "get_document",
            "get_document_metadata",
            "get_document_content",
            "download_document",
            "get_notes",
            "upload_document",
            "update_document",
            "add_note",
            "delete_document",
        ):
            self.assertEqual(ops[name]["data_class_max"], "professional_secret", name)
        classes = {c["id"]: c["data_class"] for c in self.entry()["storage"]["categories"]}
        self.assertEqual(classes["documents"], "professional_secret")
        self.assertEqual(classes["notes"], "professional_secret")

    def test_custom_field_items_reject_unknown_keys(self) -> None:
        ops = {op["name"]: op for op in self.entry()["operations"]}
        items = ops["update_document"]["input_schema"]["properties"]["custom_fields"]["items"]
        self.assertEqual(items.get("additionalProperties"), False)
        self.assertEqual(sorted(items.get("required", [])), ["field", "value"])
        self.assertEqual(sorted(items["properties"]), ["field", "value"])
        value = items["properties"]["value"]
        self.assertIsInstance(value.get("maxLength"), int)
        self.assertIsInstance(value.get("maxItems"), int)

    def test_ordering_is_a_field_name(self) -> None:
        ops = {op["name"]: op for op in self.entry()["operations"]}
        ordering = ops["search_documents"]["input_schema"]["properties"]["ordering"]
        self.assertEqual(ordering.get("pattern"), "^-?[a-z_]{1,40}$")

    def test_delete_is_confirmed_with_check_access(self) -> None:
        ops = {op["name"]: op for op in self.entry()["operations"]}
        self.assertEqual(ops["delete_document"]["rights_check"], "server_and_check_access")

    def test_backup_items_cover_every_named_volume(self) -> None:
        backup = {v["name"]: v["backup"] for v in self.entry()["volumes"]}
        self.assertEqual(
            backup,
            {
                "paperless-data": "include",
                "paperless-pgdata": "include",
                "paperless-valkeydata": "exclude",
                "paperless-gate-mcp": "exclude",
            },
        )

    def test_storage_declares_personal_data_and_an_exit_export(self) -> None:
        entry = self.entry()
        self.assertIs(entry["storage"]["personal_data"], True)
        self.assertEqual(
            {c["id"]: c["erasure"] for c in entry["storage"]["categories"]},
            {"accounts": "on_request", "documents": "on_request", "notes": "on_request"},
        )
        # No handler erases a Paperless account with the person; the
        # administrator deletes it, so the declared way is the manual one.
        accounts = next(c for c in entry["storage"]["categories"] if c["id"] == "accounts")
        self.assertEqual(accounts["on_request"]["action"], "delete-account")
        schema = self.path / entry["export"]["exit"]["schema"]
        self.assertTrue(schema.is_file())
        self.assertEqual(json.loads(schema.read_text(encoding="utf-8"))["type"], "array")

    def test_declares_no_roles_events_or_service_accounts(self) -> None:
        entry = self.entry()
        for key in ("roles", "events", "service_accounts", "delegation", "downstream"):
            self.assertNotIn(key, entry)


class RepositoryTests(unittest.TestCase):
    def test_no_manifest_carries_the_undecided_marker(self) -> None:
        for path in components.addon_dirs(REPO_ROOT):
            text = (path / components.MANIFEST).read_text(encoding="utf-8")
            self.assertNotIn(exposes.UNDECIDED, text, path.name)


if __name__ == "__main__":
    unittest.main()
