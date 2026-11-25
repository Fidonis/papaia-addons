"""Install the paperless add-on on a core checkout and read what the core rendered.

The core generates the chat entry, the audience mapper on the `librechat` client and the
resource-server client of a tool server from its `exposes:` entry (contract generation
7). This test holds the add-on to that: after `addon install` the rendered LibreChat
configuration lists `mcp-paperless` from the core's generated layer, the rendered realm
maps the audience, the add-on ships no file of the old format, and `addon check`
reports the add-on compatible.

It needs a checkout of the core (papaia 1.10.0 or newer, with its tool environment) and
runs only when the variable names one:

    PAPAIA_CORE_CHECKOUT=/path/to/papaia python3 -m unittest tools/test_paperless_core_install.py

The installation lives in a temporary directory: `setup` and `addon install` run with the
core's own command line against a copy of the checkout, exactly as the core's tests do.
No container is started, no port is published and nothing outside the directory is
written. Generated secrets stay in the temporary directory and are never printed.
`install_on_core` is shared with tools/test_paperless_connect_core_install.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON = REPO_ROOT / "paperless"
CORE = os.environ.get("PAPAIA_CORE_CHECKOUT", "")
AUDIENCE = "mcp-paperless"
SERVER_URL = "http://paperless-mcp:8000/mcp"

# Runs in the core's tool environment (`tools/` on sys.path, the core's tests beside
# it). Prints one JSON object with what the core rendered; never a secret.
DRIVER = r"""
import contextlib, io, json, shutil, sys
from pathlib import Path

work, addon_source, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[4]
sys.path[:0] = [sys.argv[3], sys.argv[3] + "/tests"]
import yaml
from install_helpers import Installation
from lib import cli

inst = Installation(work, ["--app-host=https://papaia.example.org"])
# The checkout's compatibility matrix, unchanged: it decides whether the add-on is a
# module of the suite (`trust: platform`), which the chat entry needs.
shutil.copytree(Path(sys.argv[3]).parent / "compat", inst.repo / "compat")
addon = work / "addons" / name
shutil.copytree(addon_source, addon, ignore=shutil.ignore_patterns(".env", "data"))

def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main(["--repo-root", str(inst.repo), "--config-dir", str(inst.config), *args])
    return code, out.getvalue()

setup_code, _ = run("setup", "--env=papaia", *inst.flags)
install_code, install_out = run("addon-install", "--name", name, "--path", str(addon))
check_code, check_out = run("addon-check")

librechat = yaml.safe_load(
    (inst.config / "ai/librechat/librechat.yaml").read_text(encoding="utf-8")) or {}
realm = json.loads((inst.config / "infra/keycloak/realm-import/papaia-realm.json")
                   .read_text(encoding="utf-8"))
librechat_client = next(c for c in realm["clients"] if c["clientId"] == "librechat")
catalog = json.loads((inst.config / "platform/services.json").read_text(encoding="utf-8"))
entry = next((e for e in catalog["services"] if e["id"] == "paperless.documents"), {})

def chat_networks(path):
    services = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services") or {}
    return (services.get("librechat") or {}).get("networks") or []

# Generated compose overrides that put librechat on a network of this add-on.
overrides = inst.config / "overrides"
chat_override = sorted(
    str(p.relative_to(inst.config)) for p in overrides.rglob("*.yml")
    if any(name in str(n) for n in chat_networks(p))) if overrides.is_dir() else []
print(json.dumps({
    "setup": setup_code,
    "install": install_code,
    "install_out": [line for line in install_out.splitlines()
                    if "mapper" in line.lower() or "mcp-paperless" in line],
    "check": check_code,
    "check_out": [line for line in check_out.splitlines() if line.startswith(name + " ")],
    "servers": librechat.get("mcpServers") or {},
    "allowed": (librechat.get("mcpSettings") or {}).get("allowedDomains") or [],
    "mappers": {m["name"]: m for m in librechat_client.get("protocolMappers") or []},
    "realm_clients": sorted(c["clientId"] for c in realm["clients"]),
    "trust": entry.get("trust"),
    "address": entry.get("address"),
    "chat_override": chat_override,
    "addon_manifest_dirs": [str(p.relative_to(inst.config))
                            for p in inst.config.rglob("addon-manifests")],
    "addon_integration_files": sorted(
        str(p.relative_to(addon)) for p in (addon / "integration").rglob("*") if p.is_file()),
}))
"""


def core_tools() -> Path:
    return Path(CORE).resolve() / "tools"


def install_on_core(test: type[unittest.TestCase], addon: Path) -> dict:
    """Install the add-on in `addon` under its directory name on a temporary copy of the
    core checkout and return what the core rendered. Skips the class when the checkout
    does not generate chat entries."""
    tools = core_tools()
    if not (tools / "lib" / "chat_servers.py").is_file():
        raise unittest.SkipTest(f"{CORE} is not a core checkout that generates chat entries")
    work = Path(tempfile.mkdtemp(prefix=f"{addon.name}-core-"))
    test.addClassCleanup(shutil.rmtree, work, True)
    proc = subprocess.run(
        ["uv", "run", "--locked", "--extra", "dev", "python", "-c", DRIVER,
         str(work), str(addon), str(tools), addon.name],
        cwd=tools, capture_output=True, text=True, timeout=600, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"driver failed ({proc.returncode}): {proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(CORE, "set PAPAIA_CORE_CHECKOUT to a core checkout to run this test")
class PaperlessOnTheCoreTests(unittest.TestCase):
    result: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = install_on_core(cls, ADDON)

    def test_setup_and_install_succeed(self) -> None:
        self.assertEqual(self.result["setup"], 0)
        self.assertEqual(self.result["install"], 0)

    def test_chat_lists_the_server_from_the_core_rendered_layer(self) -> None:
        self.assertEqual(self.result["trust"], "platform")
        servers = self.result["servers"]
        self.assertIn(AUDIENCE, servers)
        self.assertEqual(
            servers[AUDIENCE],
            {
                "title": "Document archive",
                "description": "Search, read, file and annotate documents in Paperless"
                " with the rights of the person.",
                "type": "streamable-http",
                "url": SERVER_URL,
                "headers": {"Authorization": "Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}"},
                "startup": False,
            },
        )
        self.assertEqual(self.result["address"], SERVER_URL)
        self.assertIn("http://paperless-mcp:8000", self.result["allowed"])
        # The entry of the old fragment is gone: one server per audience, no second name.
        self.assertNotIn("Paperless", servers)
        self.assertEqual([k for k, v in servers.items() if v.get("url") == SERVER_URL], [AUDIENCE])

    def test_the_audience_mapper_exists_on_the_librechat_client(self) -> None:
        mapper = self.result["mappers"].get(f"{AUDIENCE}-audience")
        self.assertIsNotNone(mapper)
        self.assertEqual(mapper["protocolMapper"], "oidc-audience-mapper")
        self.assertEqual(mapper["config"]["included.client.audience"], AUDIENCE)
        self.assertEqual(mapper["config"]["access.token.claim"], "true")
        self.assertEqual(mapper["config"]["id.token.claim"], "false")

    def test_no_file_of_the_old_format_remains(self) -> None:
        self.assertEqual(self.result["addon_manifest_dirs"], [])
        # Only the web login client and the proxy host stay with the add-on.
        self.assertEqual(
            self.result["addon_integration_files"],
            ["integration/infra/keycloak/paperless.json", "integration/infra/nginx/paperless.conf"],
        )
        # The core creates the resource-server client in the realm sync; the add-on
        # does not import one of its own into the rendered realm.
        self.assertNotIn(AUDIENCE, self.result["realm_clients"])
        # The operator is not asked to add the mapper by hand.
        self.assertFalse(
            [line for line in self.result["install_out"] if "add protocol mapper" in line.lower()],
            self.result["install_out"],
        )

    def test_addon_check_reports_the_addon_compatible(self) -> None:
        self.assertEqual(self.result["check"], 0, self.result["check_out"])
        self.assertTrue(
            any("OK" in line for line in self.result["check_out"]), self.result["check_out"]
        )


if __name__ == "__main__":
    unittest.main()
