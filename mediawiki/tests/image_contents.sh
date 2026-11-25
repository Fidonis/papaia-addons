#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# papaia-addon-mediawiki — image contents check
# by Fidonis GmbH · https://fidonis.de
#
# Checks a built wiki image against image/core-vendor.lock, without starting
# the stack:
#
# - the vendor directory of MediaWiki core lists every pinned package at the
#   pinned version and dist reference (vendor/composer/installed.json and
#   Composer's runtime API agree);
# - compared with the base image of image/Dockerfile, installed.json differs
#   only in the pinned packages (the diff is printed);
# - the core autoloader parses a YAML sample with
#   Symfony\Component\Yaml\Yaml::parse;
# - MediaWiki's own check of the vendor directory against core's
#   composer.json (`maintenance/run.php checkComposerLockUpToDate`, which
#   update.php runs on every start) passes;
# - `php maintenance/run.php Version` (the `version` script) succeeds.
#
#   tests/image_contents.sh <image reference>
#
# The base image is run for the platform of the checked image, so the check
# works for a linux/arm64 build under emulation too.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
    echo "usage: $0 <image reference>" >&2
    exit 2
fi
image="$1"
addon_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock="$addon_dir/image/core-vendor.lock"
base="$(sed -n 's#^FROM \(docker\.io/library/mediawiki:[0-9.]*@sha256:[0-9a-f]\{64\}\)$#\1#p' "$addon_dir/image/Dockerfile")"
[[ -n "$base" ]] || { echo "cannot read the MediaWiki base image from image/Dockerfile" >&2; exit 1; }
platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

installed=/var/www/html/vendor/composer/installed.json
docker run --rm --platform "$platform" --network none --entrypoint cat "$image" "$installed" > "$work/after.json"
docker run --rm --platform "$platform" --network none --entrypoint cat "$base" "$installed" > "$work/before.json"

echo "== vendor/composer/installed.json: base image -> $image ($platform)"
python3 - "$lock" "$work/before.json" "$work/after.json" <<'EOF'
import json
import sys

lock, before, after = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:])
pins = {p["name"]: p for p in lock["packages"]}
old = {p["name"]: p for p in before["packages"]}
new = {p["name"]: p for p in after["packages"]}
errors = []
if set(old) != set(new):
    errors.append(f"package set changed: added {sorted(set(new) - set(old))}, removed {sorted(set(old) - set(new))}")
if {k: v for k, v in before.items() if k != "packages"} != {k: v for k, v in after.items() if k != "packages"}:
    errors.append("installed.json changed outside its package list")
for name in sorted(set(old) & set(new)):
    if old[name] == new[name]:
        if name in pins:
            errors.append(f"{name} is pinned but unchanged ({old[name]['version']})")
        continue
    if name not in pins:
        errors.append(f"{name} changed although it is not pinned ({old[name]['version']} -> {new[name]['version']})")
        continue
    fields = sorted(k for k in set(old[name]) | set(new[name]) if old[name].get(k) != new[name].get(k))
    print(f"{name}: {old[name]['version']} -> {new[name]['version']} (changed: {', '.join(fields)})")
for name, pin in pins.items():
    got = new.get(name)
    if got is None:
        errors.append(f"{name} is not installed")
    elif got["version"] != pin["version"] or got["dist"]["reference"] != pin["dist"]["reference"]:
        errors.append(f"{name} is {got['version']} ({got['dist']['reference']}), pinned {pin['version']} ({pin['dist']['reference']})")
for error in errors:
    print(f"FAIL {error}")
sys.exit(1 if errors else 0)
EOF

pins="$(python3 -c 'import json, sys; print(" ".join(p["name"] + "=" + p["version"] for p in json.load(open(sys.argv[1]))["packages"]))' "$lock")"

echo "== runtime checks in $image"
# Throw-away settings with an SQLite database in a temporary directory; no
# network, nothing written outside the container.
docker run --rm --platform "$platform" --network none --entrypoint bash -e PINS="$pins" "$image" -c '
set -euo pipefail
cd /var/www/html
php -r '"'"'
require "vendor/autoload.php";
foreach (explode(" ", getenv("PINS")) as $pin) {
    [$name, $version] = explode("=", $pin, 2);
    $got = Composer\InstalledVersions::getPrettyVersion($name);
    if ($got !== $version) { fwrite(STDERR, "FAIL $name is $got at runtime, pinned $version\n"); exit(1); }
    echo "$name $got (runtime)\n";
}
$parsed = Symfony\Component\Yaml\Yaml::parse("wiki:\n  name: papaia\n  tiers: [internal, management]\n  public: false\n");
$expected = ["wiki" => ["name" => "papaia", "tiers" => ["internal", "management"], "public" => false]];
if ($parsed !== $expected) { fwrite(STDERR, "FAIL Yaml::parse returned " . var_export($parsed, true) . "\n"); exit(1); }
echo "Symfony\\Component\\Yaml\\Yaml::parse ok\n";
'"'"'
php maintenance/run.php checkComposerLockUpToDate
data="$(mktemp -d)"
chown www-data: "$data"
pass="$(od -An -N16 -tx1 /dev/urandom | tr -d " \n")"
su -s /bin/bash www-data -c "php maintenance/run.php install --dbtype=sqlite --dbpath=$data --dbname=check --server=http://localhost --scriptpath= --pass=$pass --confpath=$data --quiet Check Admin >/dev/null"
su -s /bin/bash www-data -c "php maintenance/run.php Version --conf=$data/LocalSettings.php"
'
echo "image contents check passed: $image"
