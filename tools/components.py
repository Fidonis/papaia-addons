#!/usr/bin/env python3
"""Keep the component versions an add-on bundles in one place.

An add-on's `version` in `papaia-app.yaml` identifies the bundle, not the
software inside it. The software is named by the image tags in the add-on's
`docker-compose.yml`, so that file stays the one source of truth: the
`${VAR:-image:tag}` defaults are what actually runs, and what an operator
overrides through `VAR`. Everything else that mentions a version is either
declared next to it or derived from it, and this script keeps the two honest:

* the `components:` list of the manifest, which consumers such as
  papaia-manager read because they scan catalogs by manifest only;
* the commented image lines of `.env.example`;
* the generated table between the `components` markers of each README.

Commands:
  check          fail on any disagreement (CI runs this)
  fix            rewrite the derived places from Compose
  report         Markdown matrix of what changed since a ref
  release-check  fail when an add-on changed since a ref without a version bump
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

MANIFEST = "papaia-app.yaml"
COMPOSE = "docker-compose.yml"
ENV_EXAMPLE = ".env.example"
README = "README.md"

README_START = "<!-- components:start -->"
README_END = "<!-- components:end -->"

ROLES = ("primary", "sidecar")
KINDS = ("upstream", "fidonis")
REQUIRED = ("id", "name", "role", "kind", "image_var", "version", "source")

# Compose files name every image as `${VAR:-image:tag}` so the operator can swap
# it from the add-on's .env. A bare `image:` would hide the version from here.
COMPOSE_IMAGE = re.compile(r"^\s+image:\s*(?P<value>.+?)\s*$")
COMPOSE_DEFAULT = re.compile(r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*):-(?P<ref>[^}\s]+)\}$")
ENV_IMAGE = re.compile(r"^(?P<prefix>#\s*)?(?P<var>[A-Z][A-Z0-9_]*)=(?P<ref>\S+)\s*$")
# `3.0.5`, `v1.19.0`, `1.43.11-1.0.0`, `3.2.3.0`: a patch level is named.
# `18`, `8.34`, `9-alpine`, `latest`: the deployed version depends on the pull date.
EXACT_TAG = re.compile(r"^v?\d+\.\d+\.\d+(?:[.+-].*)?$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def image_tag(ref: str) -> str:
    """Tag of an image reference (`registry/name:tag[@digest]`)."""
    last = ref.split("@", 1)[0].rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else "latest"


def is_exact(tag: str) -> bool:
    return EXACT_TAG.match(tag) is not None


def read_compose_images(text: str) -> tuple[dict[str, str], list[str]]:
    """Map each image variable to its default reference.

    Also returns the `image:` values that do not follow `${VAR:-ref}`.
    """
    images: dict[str, str] = {}
    unreadable: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        found = COMPOSE_IMAGE.match(line)
        if not found:
            continue
        default = COMPOSE_DEFAULT.match(found["value"])
        if default:
            images[default["var"]] = default["ref"]
        else:
            unreadable.append(found["value"])
    return images, unreadable


def render_table(components: list[dict[str, Any]]) -> str:
    rows = ["| Component | Version | Role | Image variable |", "|---|---|---|---|"]
    for component in components:
        version = f"`{component['version']}`"
        if component.get("pinned") is False:
            version += " (floating)"
        rows.append(
            f"| [{component['name']}]({component['source']}) | {version} "
            f"| {component['role']} | `{component['image_var']}` |"
        )
    return "\n".join(rows)


def render_block(components: list[dict[str, Any]]) -> str:
    return f"{README_START}\n{render_table(components)}\n{README_END}"


def readme_block(text: str) -> str | None:
    start, end = text.find(README_START), text.find(README_END)
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + len(README_END)]


def load_manifest(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load((path / MANIFEST).read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def addon_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / MANIFEST).is_file())


def check_addon(path: Path) -> list[str]:
    manifest = load_manifest(path)
    components = manifest.get("components")
    if not isinstance(components, list) or not components:
        return [f"{MANIFEST} declares no `components`"]

    images, unreadable = read_compose_images((path / COMPOSE).read_text(encoding="utf-8"))
    problems = [
        f"{COMPOSE}: image `{value}` does not follow `${{VAR:-image:tag}}`" for value in unreadable
    ]

    ids: set[str] = set()
    variables: set[str] = set()
    valid: list[dict[str, Any]] = []
    for index, component in enumerate(components, 1):
        where = f"components[{index}]"
        if not isinstance(component, dict):
            problems.append(f"{where} is not a mapping")
            continue
        label = str(component.get("id") or where)
        bad = [
            key for key in REQUIRED if not isinstance(component.get(key), str) or not component[key]
        ]
        if bad:
            problems.append(
                f"{label}: needs a non-empty string for {', '.join(bad)}"
                " (quote the version, YAML reads `18` as a number)"
            )
            continue
        if component["role"] not in ROLES:
            problems.append(f"{label}: role `{component['role']}` is not one of {', '.join(ROLES)}")
        if component["kind"] not in KINDS:
            problems.append(f"{label}: kind `{component['kind']}` is not one of {', '.join(KINDS)}")
        if not component["source"].startswith("https://"):
            problems.append(f"{label}: source must be an https:// URL")
        if "pinned" in component and not isinstance(component["pinned"], bool):
            problems.append(f"{label}: pinned must be true or false")
        if component["id"] in ids:
            problems.append(f"{label}: id is declared twice")
        if component["image_var"] in variables:
            problems.append(f"{label}: image_var {component['image_var']} is declared twice")
        ids.add(component["id"])
        variables.add(component["image_var"])
        valid.append(component)

        var = component["image_var"]
        if var not in images:
            problems.append(f"{label}: {var} is not an image variable in {COMPOSE}")
            continue
        tag = image_tag(images[var])
        if component["version"] != tag:
            problems.append(
                f"{label}: version is `{component['version']}`, {COMPOSE} pulls `{tag}`"
            )
        exact = is_exact(tag)
        if not exact and component.get("pinned") is not False:
            problems.append(
                f"{label}: tag `{tag}` floats, declare `pinned: false` or pin an exact tag"
            )
        if exact and component.get("pinned") is False:
            problems.append(f"{label}: `pinned: false` but tag `{tag}` is exact")
        if not exact and (component["role"] == "primary" or component["kind"] == "fidonis"):
            problems.append(
                f"{label}: a primary or Fidonis component needs an exact tag, not `{tag}`"
            )

    for var in images:
        if var not in variables:
            problems.append(f"{var}: image in {COMPOSE} has no entry in `components`")

    env_path = path / ENV_EXAMPLE
    if env_path.is_file():
        for number, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
            found = ENV_IMAGE.match(line)
            if found and found["var"] in images and found["ref"] != images[found["var"]]:
                problems.append(
                    f"{ENV_EXAMPLE}:{number}: {found['var']} is `{found['ref']}`, "
                    f"{COMPOSE} pulls `{images[found['var']]}`"
                )

    readme = path / README
    if readme.is_file() and not problems:
        block = readme_block(readme.read_text(encoding="utf-8"))
        if block is None:
            problems.append(f"{README}: missing the `{README_START}` / `{README_END}` markers")
        elif block != render_block(valid):
            problems.append(f"{README}: components table is out of date")
    return problems


def sync_manifest(text: str, images: dict[str, str]) -> str:
    """Rewrite the `version:` of each component from the Compose default.

    Line-based on purpose: the manifest carries long explanatory comments that a
    YAML round trip would drop. Relies on `image_var` coming before `version`
    inside an entry, which `check` reports when it does not.
    """
    lines = text.splitlines(keepends=True)
    in_block = False
    var: str | None = None
    for index, line in enumerate(lines):
        if re.match(r"^\S", line):
            in_block = line.startswith("components:")
            var = None
            continue
        if not in_block:
            continue
        if re.match(r"^\s*-\s+id:", line):
            var = None
        found = re.match(r"^\s+image_var:\s*(\S+)\s*$", line)
        if found:
            var = found[1]
            continue
        match = re.match(r"^(\s+)version:\s*.*$", line)
        if match and var in images:
            lines[index] = f'{match[1]}version: "{image_tag(images[var])}"\n'
    return "".join(lines)


def sync_env_example(text: str, images: dict[str, str]) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        found = ENV_IMAGE.match(line.rstrip("\n"))
        if found and found["var"] in images:
            line = f"{found['prefix'] or ''}{found['var']}={images[found['var']]}\n"
        out.append(line)
    return "".join(out)


def fix_addon(path: Path) -> list[str]:
    """Rewrite the derived places; returns the names of the files that changed."""
    images, _ = read_compose_images((path / COMPOSE).read_text(encoding="utf-8"))
    changed: list[str] = []

    def rewrite(name: str, convert: Any) -> None:
        target = path / name
        if not target.is_file():
            return
        before = target.read_text(encoding="utf-8")
        after = convert(before)
        if after != before:
            target.write_bytes(after.encode("utf-8"))
            changed.append(name)

    rewrite(MANIFEST, lambda text: sync_manifest(text, images))
    rewrite(ENV_EXAMPLE, lambda text: sync_env_example(text, images))
    # Read after the rewrite above, so the table carries the new versions.
    components = [c for c in load_manifest(path).get("components") or [] if isinstance(c, dict)]

    def readme(text: str) -> str:
        block = readme_block(text)
        return text if block is None else text.replace(block, render_block(components))

    rewrite(README, readme)
    return changed


def git_show(root: Path, ref: str, relpath: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{relpath}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def git_changed(root: Path, ref: str, addon: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", ref, "HEAD", "--", f"{addon}/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"git diff against {ref} failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def old_versions(root: Path, ref: str, addon: str, current: list[dict[str, Any]]) -> dict[str, str]:
    """Component id -> version at `ref`.

    A ref from before the manifests carried `components` is read from its
    Compose file instead, through the image variables of the current entries.
    """
    manifest_text = git_show(root, ref, f"{addon}/{MANIFEST}")
    if manifest_text is None:
        return {}
    declared = (yaml.safe_load(manifest_text) or {}).get("components")
    if isinstance(declared, list):
        return {c["id"]: str(c["version"]) for c in declared if isinstance(c, dict) and "id" in c}
    compose_text = git_show(root, ref, f"{addon}/{COMPOSE}") or ""
    images, _ = read_compose_images(compose_text)
    return {c["id"]: image_tag(images[c["image_var"]]) for c in current if c["image_var"] in images}


def old_addon_version(root: Path, ref: str, addon: str) -> str | None:
    text = git_show(root, ref, f"{addon}/{MANIFEST}")
    if text is None:
        return None
    return str((yaml.safe_load(text) or {}).get("version"))


def arrow(old: str | None, new: str) -> str:
    return new if old in (None, new) else f"{old} → {new}"


def report(root: Path, ref: str, everything: bool) -> str:
    rows = ["| Add-on | Version | Components |", "|---|---|---|"]
    for path in addon_dirs(root):
        manifest = load_manifest(path)
        components = [c for c in manifest.get("components") or [] if isinstance(c, dict)]
        before = old_versions(root, ref, path.name, components)
        old_version = old_addon_version(root, ref, path.name)
        new_version = str(manifest.get("version"))
        parts = [f"{c['name']} {arrow(before.get(c['id']), c['version'])}" for c in components]
        changed = old_version != new_version or any(
            before.get(c["id"]) != c["version"] for c in components
        )
        if changed or everything:
            label = arrow(old_version, new_version) if old_version else f"{new_version} (new)"
            rows.append(f"| {path.name} | {label} | {'; '.join(parts)} |")
    return "\n".join(rows)


def release_check(root: Path, ref: str) -> list[str]:
    problems = []
    for path in addon_dirs(root):
        name = path.name
        old_text = git_show(root, ref, f"{name}/{MANIFEST}")
        if old_text is None:
            continue  # new since the ref, nothing to compare against
        old = yaml.safe_load(old_text) or {}
        new = load_manifest(path)
        old_version, new_version = str(old.get("version")), str(new.get("version"))
        old_match, new_match = SEMVER.match(old_version), SEMVER.match(new_version)
        if not new_match:
            problems.append(f"{name}: version `{new_version}` is not X.Y.Z")
            continue
        if old_match and tuple(map(int, new_match.groups())) < tuple(map(int, old_match.groups())):
            problems.append(f"{name}: version went backwards, {old_version} → {new_version}")
            continue
        files = [f for f in git_changed(root, ref, name) if not f.endswith(".md")]
        others = [f for f in files if f != f"{name}/{MANIFEST}"]
        old_rest = {k: v for k, v in old.items() if k != "version"}
        new_rest = {k: v for k, v in new.items() if k != "version"}
        substantive = bool(others) or old_rest != new_rest
        if substantive and old_version == new_version:
            problems.append(f"{name}: changed since {ref} but the version is still {new_version}")
        if not substantive and old_version != new_version:
            problems.append(f"{name}: version moved {old_version} → {new_version} without a change")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="catalog checkout (default: this repo)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("fix")
    for name in ("report", "release-check"):
        command = sub.add_parser(name)
        command.add_argument(
            "--from", dest="ref", required=True, help="tag or commit to compare against"
        )
    sub.choices["report"].add_argument(
        "--all", action="store_true", help="list unchanged add-ons too"
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the report prints arrows

    root: Path = args.root
    if args.command == "check":
        failed = False
        for path in addon_dirs(root):
            for problem in check_addon(path):
                print(f"{path.name}: {problem}", file=sys.stderr)
                failed = True
        if failed:
            print(
                "Run `python tools/components.py fix` to rewrite what Compose decides.",
                file=sys.stderr,
            )
        return 1 if failed else 0
    if args.command == "fix":
        for path in addon_dirs(root):
            for name in fix_addon(path):
                print(f"{path.name}/{name}")
        return 0
    if args.command == "report":
        print(report(root, args.ref, args.all))
        return 0
    problems = release_check(root, args.ref)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
