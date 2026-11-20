#!/usr/bin/env python3
"""Hold the `exposes:` section of a manifest to the files next to it.

`exposes:` (contract generation 7) describes each surface of an add-on once:
its tool server with operations, data classes, backup items and storage. The
schema, the naming register and the data class scale belong to papaia-kit; its
manifest conformance check validates them. This script checks what the kit
cannot see from the section alone, because it needs the add-on directory:

* no manifest carries the `@UNDECIDED@` marker;
* a manifest with `exposes:` declares `requires.addon_api` as a list of
  generations from 7 on, every entry repeats that list, and every declared
  generation has its meaning in a comment of the `requires:` block;
* entry ids and operation names are unique, `address.service` is a Compose
  service, `audience` is the client id of a shipped Keycloak client;
* `resource.id_field` names a property of the operation's `input_schema`;
* every named volume of `docker-compose.yml` is exactly one backup item in
  `volumes[]`, and `storage.secrets[].location` names one of them;
* `export.exit.schema` exists as a JSON file in the add-on directory.

`components.py check` runs these checks for every add-on.

Commands:
  generations --core PATH  compare the generation comments of every manifest
                           with the table in the core's ADDON_API file
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

MANIFEST = "papaia-app.yaml"
COMPOSE = "docker-compose.yml"
CORE_ADDON_API = "ADDON_API"

UNDECIDED = "@UNDECIDED@"
# The generation that introduced `exposes:`; a manifest using it needs a core
# serving at least this one.
EXPOSES_GENERATION = 7

# `#   7  meaning`, as in the core's ADDON_API table; a continuation line is a
# comment indented by at least five spaces.
GENERATION_ROW = re.compile(r"^#\s+(?P<number>\d{1,3})\s{2,}(?P<text>\S.*)$")
CONTINUATION = re.compile(r"^#\s{5,}(?P<text>\S.*)$")
TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][\w-]*:")


def _table(lines: list[str]) -> dict[int, str]:
    rows: dict[int, list[str]] = {}
    current: int | None = None
    for raw in lines:
        line = raw.strip()
        row = GENERATION_ROW.match(line)
        if row:
            current = int(row["number"])
            rows[current] = [row["text"]]
            continue
        more = CONTINUATION.match(line)
        if more and current is not None:
            rows[current].append(more["text"])
            continue
        current = None
    return {number: " ".join(" ".join(parts).split()) for number, parts in rows.items()}


def core_generations(text: str) -> dict[int, str]:
    """Generation -> meaning, from the comment table of the core's ADDON_API file."""
    return _table(text.splitlines())


def generation_comments(text: str) -> dict[int, str]:
    """Generation -> meaning, from the comments of a manifest's `requires:` block."""
    block: list[str] = []
    inside = False
    for line in text.splitlines():
        if TOP_LEVEL_KEY.match(line):
            inside = line.startswith("requires:")
            continue
        if inside:
            block.append(line)
    return _table(block)


def compare_generations(manifest_text: str, core_text: str) -> list[str]:
    """Every generation a manifest comment names must mean what the core's table says."""
    table = core_generations(core_text)
    problems = []
    for number, meaning in sorted(generation_comments(manifest_text).items()):
        if number not in table:
            problems.append(f"{MANIFEST}: generation {number} is not in the core's table")
        elif meaning != table[number]:
            problems.append(
                f"{MANIFEST}: generation {number} reads `{meaning}`, the core's table `{table[number]}`"
            )
    return problems


def compose_file(path: Path) -> dict[str, Any]:
    compose = path / COMPOSE
    if not compose.is_file():
        return {}
    try:
        loaded = yaml.safe_load(compose.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _keys(value: object) -> set[str]:
    return {str(key) for key in value} if isinstance(value, dict) else set()


def keycloak_client_ids(path: Path, manifest: dict[str, Any]) -> set[str] | None:
    """Client ids of the Keycloak clients the add-on ships; None when it ships none."""
    keycloak = (manifest.get("integration") or {}).get("keycloak")
    clients = keycloak.get("clients") if isinstance(keycloak, dict) else None
    if not isinstance(clients, list) or not clients:
        return None
    ids: set[str] = set()
    for relative in clients:
        target = path / str(relative)
        if not target.is_file():
            continue  # components.py reports the missing file
        try:
            client = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(client, dict) and isinstance(client.get("clientId"), str):
            ids.add(client["clientId"])
    return ids


def _generations(value: object) -> list[int] | None:
    if not isinstance(value, list) or not value:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return None
    return sorted(set(value))


def check_operations(label: str, operations: object) -> list[str]:
    if not isinstance(operations, list):
        return []
    problems = []
    names: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            problems.append(f"{label}: every operation must be a mapping")
            continue
        name = operation.get("name")
        if name in names:
            problems.append(f"{label}: operation `{name}` is declared twice")
        names.add(name)
        resource = operation.get("resource")
        id_field = resource.get("id_field") if isinstance(resource, dict) else None
        if id_field is None:
            continue
        schema = operation.get("input_schema")
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if id_field not in _keys(properties):
            problems.append(
                f"{label}: operation `{name}` resource.id_field `{id_field}` is not a property"
                " of its input_schema"
            )
    return problems


def check_exposes(path: Path, manifest: dict[str, Any]) -> list[str]:
    """What the `exposes:` section claims about the add-on directory."""
    if "exposes" not in manifest:
        return []
    entries = manifest["exposes"]
    if not isinstance(entries, list) or not entries:
        return [f"{MANIFEST}: exposes must be a non-empty list"]

    problems = []
    requires = manifest.get("requires")
    declared = _generations(requires.get("addon_api") if isinstance(requires, dict) else None)
    if declared is None or min(declared) < EXPOSES_GENERATION:
        problems.append(
            f"{MANIFEST}: requires.addon_api must be a list of generations from"
            f" {EXPOSES_GENERATION} on, since `exposes:` needs generation {EXPOSES_GENERATION}"
        )
    else:
        text = (path / MANIFEST).read_text(encoding="utf-8")
        commented = generation_comments(text)
        for number in declared:
            if number not in commented:
                problems.append(
                    f"{MANIFEST}: generation {number} of requires.addon_api has no comment"
                    " with its meaning in the `requires:` block"
                )

    compose = compose_file(path)
    services = _keys(compose.get("services"))
    volumes = _keys(compose.get("volumes"))
    clients = keycloak_client_ids(path, manifest)

    ids: set[str] = set()
    backup_items: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"{MANIFEST}: exposes[{index}] must be a mapping")
            continue
        label = str(entry.get("id") or f"exposes[{index}]")
        if entry.get("id") in ids:
            problems.append(f"{MANIFEST}: exposes id `{label}` is declared twice")
        ids.add(entry.get("id"))

        entry_requires = entry.get("requires")
        entry_generations = (
            entry_requires.get("addon_api") if isinstance(entry_requires, dict) else None
        )
        if entry_generations is None:
            problems.append(
                f"{label}: requires.addon_api is missing; every entry repeats the manifest's"
                " requires.addon_api"
            )
        elif declared is not None and _generations(entry_generations) != declared:
            problems.append(
                f"{label}: requires.addon_api must equal the manifest's requires.addon_api"
            )

        address = entry.get("address")
        service = address.get("service") if isinstance(address, dict) else None
        if service is not None and service not in services:
            problems.append(f"{label}: address.service `{service}` is not a service in {COMPOSE}")

        audience = entry.get("audience")
        if audience is not None and clients is not None and audience not in clients:
            problems.append(
                f"{label}: audience `{audience}` is not the clientId of a Keycloak client the"
                " add-on ships"
            )

        problems.extend(check_operations(label, entry.get("operations")))

        for volume in entry.get("volumes") or []:
            name = volume.get("name") if isinstance(volume, dict) else None
            if name not in volumes:
                problems.append(f"{label}: volumes[] names `{name}`, which is not a volume in {COMPOSE}")
            backup_items[str(name)] = backup_items.get(str(name), 0) + 1

        storage = entry.get("storage")
        secrets = storage.get("secrets") if isinstance(storage, dict) else None
        for secret in secrets if isinstance(secrets, list) else []:
            location = secret.get("location") if isinstance(secret, dict) else None
            if location not in volumes:
                problems.append(
                    f"{label}: storage.secrets[].location `{location}` is not a volume in {COMPOSE}"
                )

        export = entry.get("export")
        exit_export = export.get("exit") if isinstance(export, dict) else None
        schema = exit_export.get("schema") if isinstance(exit_export, dict) else None
        if isinstance(schema, str) and not schema.startswith("https://"):
            target = path / schema
            try:
                json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                problems.append(
                    f"{label}: export.exit.schema `{schema}` is not a JSON file in the add-on directory"
                )

    for name in sorted(volumes):
        if name not in backup_items:
            problems.append(f"{COMPOSE}: volume `{name}` has no entry in exposes[].volumes")
        elif backup_items[name] > 1:
            problems.append(f"{COMPOSE}: volume `{name}` is declared in exposes[].volumes more than once")
    return problems


def check_manifest_text(text: str) -> list[str]:
    if UNDECIDED in text:
        return [f"{MANIFEST}: carries the {UNDECIDED} marker; decide the value"]
    return []


def check(path: Path, manifest: dict[str, Any]) -> list[str]:
    text = (path / MANIFEST).read_text(encoding="utf-8")
    return check_manifest_text(text) + check_exposes(path, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="catalog checkout (default: this repo)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    generations = sub.add_parser("generations")
    generations.add_argument(
        "--core", type=Path, required=True, help="checkout of the core that carries ADDON_API"
    )
    args = parser.parse_args(argv)

    core_file = args.core / CORE_ADDON_API
    if not core_file.is_file():
        print(f"{core_file} not found", file=sys.stderr)
        return 2
    core_text = core_file.read_text(encoding="utf-8")
    failed = False
    manifests = sorted(p for p in args.root.iterdir() if (p / MANIFEST).is_file())
    for path in manifests:
        for problem in compare_generations((path / MANIFEST).read_text(encoding="utf-8"), core_text):
            print(f"{path.name}: {problem}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
