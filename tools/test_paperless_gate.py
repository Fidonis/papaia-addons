"""Tests for the Paperless gate; run with `python -m unittest discover -s tools`.

Paperless takes the person from the header `X-Papaia-Remote-User`. These tests hold the
add-on to the hardening around it: Paperless listens on the loopback of its container
only, no container publishes a port, and `paperless-gate` in Paperless' network
namespace passes a request only with the secret of one of its callers (the proxy, the
MCP server) and a person only by a name on the list of the realm sync. The live
counterpart is tools/test_paperless_gate_live.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import components
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON = REPO_ROOT / "paperless"
GATE_DIR = ADDON / "gate"
GATE_SCRIPT = GATE_DIR / "gate.sh"
GATE_TEMPLATE = GATE_DIR / "nginx.conf"
SECRET_RE = re.compile(r"\A[A-Za-z0-9._~+/=-]{32,512}\Z")
# Paperless, its data services and the MCP server: nothing else joins its network.
PAPERLESS_NET_MEMBERS = {
    "paperless",
    "paperless-mcp",
    "paperless-broker",
    "paperless-db",
}


def load_compose() -> dict:
    return yaml.safe_load((ADDON / "docker-compose.yml").read_text(encoding="utf-8"))


def load_manifest() -> dict:
    return yaml.safe_load((ADDON / "papaia-app.yaml").read_text(encoding="utf-8"))


def env_example() -> dict[str, str]:
    values = {}
    for line in (ADDON / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def default_of(value: str) -> str:
    """`${VAR:-default}` -> `default`; a literal stays as it is."""
    found = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-(.*)\}", str(value))
    return found.group(1) if found else str(value)


class ComposeHardening(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = load_compose()
        self.services = self.compose["services"]

    def test_no_service_publishes_a_port(self) -> None:
        for name, body in self.services.items():
            with self.subTest(service=name):
                self.assertNotIn("ports", body)

    def test_paperless_listens_on_its_loopback_only(self) -> None:
        env = self.services["paperless"]["environment"]
        self.assertEqual(env.get("PAPERLESS_BIND_ADDR"), "127.0.0.1")

    def test_gate_shares_the_network_namespace_of_paperless(self) -> None:
        gate = self.services["paperless-gate"]
        self.assertEqual(gate.get("network_mode"), "service:paperless")
        self.assertNotIn("networks", gate)
        self.assertNotIn("ports", gate)
        self.assertIn("paperless", gate.get("depends_on", []))

    def test_gate_runs_with_least_privilege(self) -> None:
        gate = self.services["paperless-gate"]
        self.assertIs(gate.get("read_only"), True)
        self.assertEqual(gate.get("cap_drop"), ["ALL"])
        # Reading the proxy's secret (0600 of the account running papaia-ctl) and
        # nginx' switch to an unprivileged worker; nothing else.
        self.assertLessEqual(
            set(gate.get("cap_add", [])), {"DAC_READ_SEARCH", "CHOWN", "SETUID", "SETGID"}
        )
        self.assertIn("no-new-privileges:true", gate.get("security_opt", []))
        self.assertNotIn("env_file", gate)
        self.assertNotIn("environment", gate)

    def test_gate_image_is_pinned_by_tag_and_digest(self) -> None:
        image = default_of(self.services["paperless-gate"]["image"])
        self.assertRegex(image, r"^docker\.io/library/nginx:\d+\.\d+\.\d+-alpine@sha256:[0-9a-f]{64}$")

    def test_gate_mounts_its_files_and_the_proxy_secret_read_only(self) -> None:
        mounts = self.services["paperless-gate"]["volumes"]
        by_target = {m["target"]: m for m in mounts if isinstance(m, dict)}
        self.assertEqual(len(by_target), len(mounts), "use the long volume syntax only")
        files = by_target["/gate"]
        self.assertEqual(files["source"], "./gate")
        self.assertIs(files.get("read_only"), True)
        proxy = by_target["/run/paperless-gate/callers/proxy"]
        self.assertEqual(proxy["type"], "bind")
        self.assertEqual(proxy["source"], "${PAPAIA_CONFIG_DIR}/secrets/paperless-gate/proxy")
        self.assertIs(proxy.get("read_only"), True)
        # Docker must not create the directory as root when the core has not written it.
        self.assertIs(proxy.get("bind", {}).get("create_host_path"), False)
        mcp = by_target["/run/paperless-gate/callers/mcp"]
        self.assertEqual(mcp["type"], "volume")
        self.assertEqual(mcp["source"], "paperless-gate-mcp")
        self.assertFalse(mcp.get("read_only", False))

    def test_gate_mounts_the_directory_of_the_name_list_read_only(self) -> None:
        # The realm sync replaces users.json by renaming a new file into place; a bind
        # mount of the single file would keep showing the replaced one.
        mounts = self.services["paperless-gate"]["volumes"]
        by_target = {m["target"]: m for m in mounts if isinstance(m, dict)}
        names = by_target["/run/paperless-gate/users"]
        self.assertEqual(names["type"], "bind")
        self.assertEqual(names["source"], "${PAPAIA_CONFIG_DIR}/security/paperless-gate")
        self.assertIs(names.get("read_only"), True)
        self.assertIs(names.get("bind", {}).get("create_host_path"), False)
        self.assertEqual(len(mounts), 4, "the gate mounts nothing beyond its files, two callers and the list")

    def test_gate_writes_only_to_memory(self) -> None:
        tmpfs = self.services["paperless-gate"].get("tmpfs", [])
        self.assertTrue(any(str(t).startswith("/tmp") for t in tmpfs))

    def test_paperless_accepts_the_host_name_the_mcp_server_calls(self) -> None:
        # From Paperless-ngx 3.5 on, with PAPERLESS_URL set, Paperless accepts
        # only that host name and localhost unless PAPERLESS_ALLOWED_HOSTS adds
        # more. paperless-mcp calls the gate as `paperless`, so that name (and
        # no wildcard) must be on the list, or every tool call is refused.
        env = self.services["paperless"]["environment"]
        mcp_url = default_of(self.services["paperless-mcp"]["environment"]["PAPERLESS_URL"])
        mcp_host = re.match(r"https?://([^:/]+)", mcp_url)
        assert mcp_host is not None
        hosts = [h.strip() for h in default_of(env.get("PAPERLESS_ALLOWED_HOSTS", "")).split(",") if h.strip()]
        self.assertIn(mcp_host.group(1), hosts)
        self.assertNotIn("*", hosts)

    def test_mcp_server_talks_to_the_gate_with_its_own_secret(self) -> None:
        mcp = self.services["paperless-mcp"]
        env = mcp["environment"]
        self.assertEqual(default_of(env["PAPERLESS_URL"]), "http://paperless:8001")
        self.assertEqual(env["PAPERLESS_GATE_SECRET_FILE"], "/run/papaia/secrets/paperless-gate/secret")
        mounts = {m["target"]: m for m in mcp["volumes"] if isinstance(m, dict)}
        secret = mounts["/run/papaia/secrets/paperless-gate"]
        self.assertEqual(secret["source"], "paperless-gate-mcp")
        self.assertIs(secret.get("read_only"), True)
        self.assertIn("paperless-gate", mcp.get("depends_on", []))

    def test_mcp_image_sends_the_gate_secret(self) -> None:
        # 0.1.1 sends no secret, so the gate would refuse every call.
        image = default_of(self.services["paperless-mcp"]["image"])
        tag = components.image_tag(image)
        major, minor = (int(part) for part in tag.split("-", 1)[0].split(".")[:2])
        self.assertGreaterEqual((major, minor), (0, 2))

    def test_only_paperless_its_data_services_and_the_mcp_server_share_its_network(self) -> None:
        members = {
            name
            for name, body in self.services.items()
            if "papaia-paperless-net" in (body.get("networks") or [])
        }
        self.assertEqual(members, PAPERLESS_NET_MEMBERS)

    def test_front_network_carries_only_the_mcp_server(self) -> None:
        networks = self.compose["networks"]
        self.assertEqual(
            networks["paperless-front"]["name"], "${PAPAIA_PROJECT:-papaia}-paperless-front"
        )
        members = {
            name
            for name, body in self.services.items()
            if "paperless-front" in (body.get("networks") or [])
        }
        self.assertEqual(members, {"paperless-mcp"})

    def test_gate_secret_volume_is_declared(self) -> None:
        self.assertIn("paperless-gate-mcp", self.compose["volumes"])


class ManifestAndEnv(unittest.TestCase):
    def test_no_host_port_variable_is_left(self) -> None:
        self.assertNotIn("PAPERLESS_EXT_PORT", load_manifest()["env_prompts"])
        self.assertNotIn("PAPERLESS_EXT_PORT", env_example())
        self.assertNotIn("PAPERLESS_EXT_PORT", (ADDON / "docker-compose.yml").read_text())

    def test_public_url_default_is_not_the_host_port(self) -> None:
        prompt = load_manifest()["env_prompts"]["PAPERLESS_PUBLIC_URL"]
        default = str(prompt.get("default", ""))
        self.assertNotIn("host.docker.internal", default)
        self.assertNotIn(":8010", default)

    def test_env_example_points_the_mcp_server_at_the_gate(self) -> None:
        self.assertEqual(env_example()["PAPERLESS_MCP_PAPERLESS_URL"], "http://paperless:8001")

    def test_proxy_joins_the_paperless_network(self) -> None:
        # The core attaches nginx-proxy-manager to the add-on network and every other
        # listed core service (the chat service) to the front network only.
        self.assertIn("nginx-proxy-manager", load_manifest()["networks"]["attach"])

    def test_gate_secret_volume_is_a_declared_secret_store(self) -> None:
        entry = load_manifest()["exposes"][0]
        volumes = {v["name"]: v for v in entry["volumes"]}
        self.assertEqual(volumes["paperless-gate-mcp"]["backup"], "exclude")
        self.assertIn({"kind": "file", "location": "paperless-gate-mcp"}, entry["storage"]["secrets"])


class ProxyReference(unittest.TestCase):
    def test_reference_routes_through_the_gate_with_the_proxy_secret(self) -> None:
        text = (ADDON / "integration" / "infra" / "nginx" / "paperless.conf").read_text()
        active = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn("http://paperless:8001", active)
        self.assertNotIn("paperless:8000", active)
        self.assertIn('more_clear_input_headers "X-Papaia-Remote-User" "X-Papaia-Gate";', active)
        self.assertIn("include /run/papaia/paperless-gate/nginx.conf;", active)


class GateTemplate(unittest.TestCase):
    def setUp(self) -> None:
        self.text = GATE_TEMPLATE.read_text(encoding="utf-8")

    def test_listens_on_the_gate_port_and_forwards_to_the_loopback(self) -> None:
        self.assertRegex(self.text, r"(?m)^\s*listen 8001;$")
        self.assertRegex(self.text, r"(?m)^\s*proxy_pass http://127\.0\.0\.1:8000;$")

    def test_refuses_requests_without_a_caller(self) -> None:
        self.assertIn('if ($gate_refuse) {', self.text)
        self.assertRegex(self.text, r"return 403;")

    def test_never_forwards_the_gate_header(self) -> None:
        self.assertRegex(self.text, r'(?m)^\s*proxy_set_header X-Papaia-Gate "";$')

    def test_identity_header_is_forwarded_only_for_the_mcp_server(self) -> None:
        self.assertRegex(self.text, r"(?m)^\s*proxy_set_header X-Papaia-Remote-User \$gate_remote_user;$")
        block = re.search(r"map \$gate_caller \$gate_remote_user \{(.*?)\}", self.text, re.DOTALL)
        self.assertIsNotNone(block)
        lines = [line.strip() for line in block.group(1).strip().splitlines()]
        self.assertEqual(sorted(lines), sorted(['default "";', "mcp $http_x_papaia_remote_user;"]))

    def test_header_names_with_underscores_are_dropped(self) -> None:
        self.assertRegex(self.text, r"(?m)^\s*underscores_in_headers off;$")
        self.assertRegex(self.text, r"(?m)^\s*ignore_invalid_headers on;$")

    def test_access_log_names_the_caller_and_nothing_secret(self) -> None:
        fmt = re.search(r"log_format gate escape=json '(.*?)';", self.text, re.DOTALL)
        self.assertIsNotNone(fmt)
        self.assertIn("$gate_caller", fmt.group(1))
        for leak in ("http_", "request", "request_uri", "args", "arg_", "query_string", "uri",
                     "cookie_", "remote_user"):
            self.assertNotRegex(fmt.group(1), r"\$" + leak + r"(?![a-z_])" if not leak.endswith("_")
                                else r"\$" + leak)

    def test_callers_are_filled_in_by_the_script(self) -> None:
        self.assertEqual(self.text.count("# @callers@"), 1)

    def test_listed_names_are_filled_in_by_the_script(self) -> None:
        self.assertEqual(self.text.count("# @users@"), 1)
        block = re.search(r"map \$gate_remote_user \$gate_listed \{(.*?)\}", self.text, re.DOTALL)
        self.assertIsNotNone(block)
        self.assertIn('default "";', block.group(1))
        self.assertIn("# @users@", block.group(1))

    def test_the_map_hash_holds_a_full_list(self) -> None:
        # Default buckets (64 bytes) refuse a name of 255 characters at load time.
        self.assertRegex(self.text, r"(?m)^\s*map_hash_bucket_size 512;$")
        self.assertRegex(self.text, r"(?m)^\s*map_hash_max_size 131072;$")

    def test_a_name_must_equal_its_listed_spelling_byte_for_byte(self) -> None:
        # nginx compares map strings without regard to case, Paperless does not: "Anna"
        # would match the listed "anna" and Paperless would create an account "Anna".
        block = re.search(
            r'map "\$gate_remote_user:\$gate_listed" \$gate_name_known \{(.*?)\}', self.text, re.DOTALL
        )
        self.assertIsNotNone(block)
        lines = sorted(line.strip() for line in block.group(1).strip().splitlines())
        self.assertEqual(lines, sorted(['default 0;', '":" 1;', r'"~^([^:]+):\1$" 1;']))

    def test_an_unknown_name_is_refused_before_paperless(self) -> None:
        server = self.text[self.text.index("server {"):]
        before = server[: server.index("location / {")]
        self.assertIn("if ($gate_name_known = 0) {", before)
        self.assertIn("""return 403 '{"error":"paperless_unknown_user"}\\n';""", before)
        # The syntax check stays first: a malformed name is a 400, not a lookup.
        self.assertLess(before.index("$gate_name_ok = 0"), before.index("$gate_name_known = 0"))
        self.assertRegex(server, r"(?m)^\s*default_type application/json;$")

    def test_an_unknown_name_writes_one_audit_row_without_the_name(self) -> None:
        fmt = re.search(r"log_format gate_audit escape=json '(.*?)';", self.text, re.DOTALL)
        self.assertIsNotNone(fmt)
        row = fmt.group(1)
        for field in ('"format":"papaia.audit/1"', '"writer":"paperless/paperless-gate"',
                      '"event":"paperless_unknown_user"', '"result":"denied"',
                      '"error_code":"paperless_unknown_user"', '"correlation_id":"$gate_correlation_id"'):
            self.assertIn(field, row)
        self.assertNotRegex(row, r"\$http_|\$gate_remote_user|\$gate_listed|\$request|\$uri|\$args")
        self.assertRegex(self.text, r"(?m)^\s*access_log /dev/stdout gate_audit if=\$gate_unknown_row;$")
        block = re.search(r'map "\$gate_name_ok\$gate_name_known" \$gate_unknown_row \{(.*?)\}',
                          self.text, re.DOTALL)
        self.assertIsNotNone(block)
        lines = sorted(line.strip() for line in block.group(1).strip().splitlines())
        self.assertEqual(lines, sorted(['default "";', '10 1;']))

    def test_correlation_id_is_checked_before_it_is_logged(self) -> None:
        # The block ends at the first closing brace on a line of its own.
        block = re.search(r"map \$http_x_papaia_correlation_id \$gate_correlation_id \{\n(.*?)\n\s*\}\n",
                          self.text, re.DOTALL)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("default $request_id;", body)
        self.assertIn('"~^[A-Za-z0-9-]{1,64}$" $http_x_papaia_correlation_id;', body)
        # Values shaped like a prefixed key are replaced before the general rule.
        self.assertLess(body.index("AKIA"), body.index("{1,64}"))


def run_gate(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(
        ["sh", str(GATE_SCRIPT), *args], env=full, capture_output=True, text=True, timeout=30,
        check=False,
    )


def token(length: int = 43) -> str:
    import secrets

    return secrets.token_urlsafe(64)[:length]


class GateScript(unittest.TestCase):
    """`gate.sh render` checks the caller secrets and writes the nginx configuration."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.callers = self.tmp / "callers"
        (self.callers / "proxy").mkdir(parents=True)
        (self.callers / "mcp").mkdir()
        self.run_dir = self.tmp / "run"
        self.users = self.tmp / "users"
        self.users.mkdir()
        self.env = {
            "GATE_CALLERS_DIR": str(self.callers),
            "GATE_RUN_DIR": str(self.run_dir),
            "GATE_TEMPLATE": str(GATE_TEMPLATE),
            "GATE_USERS_FILE": str(self.users / "users.json"),
        }
        self.proxy_secret = token()
        self.write_proxy(self.proxy_secret + "\n")
        self.write_users(["anna"])

    def write_users(self, users: object, raw: str | None = None) -> None:
        """Writes the list as the realm sync does: beside the old one, then renamed."""
        text = raw if raw is not None else json.dumps(
            {"format": "papaia.paperless-users/1", "users": users}, indent=2
        ) + "\n"
        part = self.users / ".users.json.new"
        part.write_text(text, encoding="utf-8")
        part.chmod(0o644)
        part.replace(self.users / "users.json")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def write_proxy(self, content: str) -> Path:
        path = self.callers / "proxy" / "secret"
        if path.is_symlink() or path.exists():
            path.unlink()
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return path

    def rendered(self) -> str:
        return (self.run_dir / "nginx.conf").read_text(encoding="utf-8")

    def mcp_secret(self) -> str:
        return (self.callers / "mcp" / "secret").read_text(encoding="utf-8").strip()

    def assert_no_secret_in(self, result: subprocess.CompletedProcess, *values: str) -> None:
        for value in values:
            self.assertNotIn(value, result.stdout)
            self.assertNotIn(value, result.stderr)

    def test_render_maps_each_callers_secret_to_its_name(self) -> None:
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.rendered()
        self.assertIn(f'"{self.proxy_secret}" proxy;', text)
        self.assertIn(f'"{self.mcp_secret()}" mcp;', text)
        self.assertNotIn("# @callers@", text)
        self.assertEqual(stat.S_IMODE((self.run_dir / "nginx.conf").stat().st_mode), 0o600)
        self.assert_no_secret_in(result, self.proxy_secret, self.mcp_secret())

    def test_mcp_secret_is_generated_once_and_kept(self) -> None:
        first = run_gate("render", env=self.env)
        self.assertEqual(first.returncode, 0, first.stderr)
        value = self.mcp_secret()
        self.assertRegex(value, SECRET_RE)
        self.assertNotEqual(value, self.proxy_secret)
        mode = stat.S_IMODE((self.callers / "mcp" / "secret").stat().st_mode)
        self.assertEqual(mode, 0o444)
        self.assertIn('"format":"papaia.audit/1"', first.stdout)
        self.assertIn('"event":"config_changed"', first.stdout)
        self.assertIn('"operation":"gate.secret_created"', first.stdout)
        self.assertIn('"object_ref":{"type":"secret","id":"paperless-gate/mcp"}', first.stdout)
        second = run_gate("render", env=self.env)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.mcp_secret(), value)
        self.assertNotIn("papaia.audit/1", second.stdout)

    def test_rotate_replaces_the_mcp_secret_and_writes_one_audit_row(self) -> None:
        self.assertEqual(run_gate("render", env=self.env).returncode, 0)
        old = self.mcp_secret()
        result = run_gate("rotate", "mcp", "rot-1234", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        new = self.mcp_secret()
        self.assertNotEqual(new, old)
        self.assertIn(f'"{new}" mcp;', self.rendered())
        self.assertNotIn(old, self.rendered())
        rows = [line for line in result.stdout.splitlines() if "papaia.audit/1" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn('"correlation_id":"rot-1234"', rows[0])
        self.assertIn('"operation":"gate.secret_rotated"', rows[0])
        self.assert_no_secret_in(result, old, new, self.proxy_secret)

    def test_rotate_refuses_an_unknown_caller(self) -> None:
        result = run_gate("rotate", "proxy", env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("papaia-ctl secrets rotate paperless-gate/proxy", result.stderr)

    def test_a_malformed_correlation_id_is_replaced(self) -> None:
        self.assertEqual(run_gate("render", env=self.env).returncode, 0)
        result = run_gate("rotate", "mcp", 'bad"id', env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('bad"id', result.stdout)
        self.assertRegex(result.stdout, r'"correlation_id":"[A-Za-z0-9-]{1,64}"')

    def test_unknown_command_is_refused(self) -> None:
        self.assertEqual(run_gate("serve", env=self.env).returncode, 2)

    def refused(self, why: str) -> subprocess.CompletedProcess:
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 1, why)
        self.assertFalse((self.run_dir / "nginx.conf").exists(), why)
        self.assertIn("proxy/secret", result.stderr, why)
        return result

    def test_missing_proxy_secret_stops_the_gate(self) -> None:
        (self.callers / "proxy" / "secret").unlink()
        self.refused("missing")

    def test_malformed_proxy_secrets_stop_the_gate(self) -> None:
        cases = {
            "empty": "",
            "too short": "a" * 31,
            "too long": "a" * 513,
            "space": token(20) + " " + token(20),
            "quote": token(40) + '"',
            "dollar": token(40) + "$x",
            "semicolon": token(40) + ";",
            "two lines": token(40) + "\n" + token(40) + "\n",
            "control character": token(40) + "\x01",
            "non-ascii": token(40) + "ä",
        }
        for why, content in cases.items():
            with self.subTest(why):
                self.write_proxy(content)
                result = self.refused(why)
                if content.strip():
                    self.assertNotIn(content.strip(), result.stderr)

    def test_oversized_proxy_secret_is_refused(self) -> None:
        self.write_proxy("a" * 5000)
        self.refused("oversized")

    def test_symlinked_proxy_secret_is_refused(self) -> None:
        target = self.tmp / "elsewhere"
        target.write_text(token() + "\n", encoding="utf-8")
        path = self.callers / "proxy" / "secret"
        path.unlink()
        path.symlink_to(target)
        self.refused("symlink")

    def test_a_tampered_mcp_secret_stops_the_gate(self) -> None:
        (self.callers / "mcp" / "secret").write_text("short\n", encoding="utf-8")
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("mcp/secret", result.stderr)

    def test_a_secret_without_a_line_break_is_accepted(self) -> None:
        self.write_proxy(self.proxy_secret)
        self.assertEqual(run_gate("render", env=self.env).returncode, 0)

    def test_one_secret_for_two_callers_stops_the_gate(self) -> None:
        mcp = self.callers / "mcp" / "secret"
        mcp.write_text(self.proxy_secret + "\n", encoding="utf-8")
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("own secret", result.stderr)

    # --- the name list of the realm sync ------------------------------------------------

    def listed(self) -> list[str]:
        """Names in the rendered map, in the order written."""
        text = self.rendered()
        block = re.search(r"map \$gate_remote_user \$gate_listed \{(.*?)\}", text, re.DOTALL)
        self.assertIsNotNone(block)
        names = []
        for line in block.group(1).strip().splitlines():
            line = line.strip()
            if line == 'default "";':
                continue
            found = re.fullmatch(r'"(\\?)([^"\\]+)" "([^"]+)";', line)
            self.assertIsNotNone(found, line)
            self.assertEqual(found.group(2), found.group(3), "a name maps to its own spelling")
            # map reads a leading backslash as "not one of my parameters".
            self.assertEqual(bool(found.group(1)),
                             found.group(2) in ("default", "hostnames", "include", "volatile"), line)
            names.append(found.group(2))
        self.assertNotIn("# @users@", text)
        return names

    def test_render_maps_each_listed_name_to_itself(self) -> None:
        names = ["anna", "ben.miller", "c+d@example.org", "e_f-g", "x" * 150]
        self.write_users(names)
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(self.listed()), sorted(names))
        self.assertIn("name list: 5 names", result.stderr)
        for name in names:
            self.assertNotIn(name, result.stdout + result.stderr)

    def test_names_equal_to_map_parameters_stay_names(self) -> None:
        names = ["default", "hostnames", "include", "volatile"]
        self.write_users(names)
        self.assertEqual(run_gate("render", env=self.env).returncode, 0)
        self.assertIn('        "\\include" "include";\n', self.rendered())
        self.assertEqual(sorted(self.listed()), names)

    def test_an_empty_list_admits_no_name(self) -> None:
        self.write_users([])
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.listed(), [])
        self.assertIn("name list: 0 names", result.stderr)

    def test_a_missing_list_admits_no_name_and_keeps_the_gate_up(self) -> None:
        # The realm sync removes the list when no module serves mcp-paperless. The proxy
        # path carries no identity and stays open.
        (self.users / "users.json").unlink()
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.listed(), [])
        self.assertIn("users.json is missing; no name is admitted", result.stderr)

    def test_a_malformed_list_admits_no_name(self) -> None:
        good = '{"format": "papaia.paperless-users/1", "users": ["anna"]}'
        cases = {
            "not json": "anna\nben\n",
            "empty file": "",
            "top level array": '["anna"]',
            "wrong format": '{"format": "papaia.paperless-users/2", "users": ["anna"]}',
            "format missing": '{"users": ["anna"]}',
            "users missing": '{"format": "papaia.paperless-users/1"}',
            "unknown field": '{"format": "papaia.paperless-users/1", "users": ["anna"], "extra": 1}',
            "repeated field": '{"format": "papaia.paperless-users/1", "users": [], "users": ["anna"]}',
            "users not a list": '{"format": "papaia.paperless-users/1", "users": "anna"}',
            "number in list": '{"format": "papaia.paperless-users/1", "users": ["anna", 1]}',
            "nested list": '{"format": "papaia.paperless-users/1", "users": [["anna"]]}',
            "null in list": '{"format": "papaia.paperless-users/1", "users": ["anna", null]}',
            "space in name": '{"format": "papaia.paperless-users/1", "users": ["an na"]}',
            "quote in name": '{"format": "papaia.paperless-users/1", "users": ["an\\"na"]}',
            "escape in name": '{"format": "papaia.paperless-users/1", "users": ["\\u0061nna"]}',
            "semicolon in name": '{"format": "papaia.paperless-users/1", "users": ["anna;"]}',
            "brace in name": '{"format": "papaia.paperless-users/1", "users": ["an{na"]}',
            "dollar in name": '{"format": "papaia.paperless-users/1", "users": ["$anna"]}',
            "colon in name": '{"format": "papaia.paperless-users/1", "users": ["an:na"]}',
            "tilde in name": '{"format": "papaia.paperless-users/1", "users": ["~anna"]}',
            "leading separator": '{"format": "papaia.paperless-users/1", "users": [".anna"]}',
            "empty name": '{"format": "papaia.paperless-users/1", "users": [""]}',
            "non-ascii name": '{"format": "papaia.paperless-users/1", "users": ["änna"]}',
            "control character": '{"format": "papaia.paperless-users/1", "users": ["an\x01na"]}',
            "name too long": '{"format": "papaia.paperless-users/1", "users": ["' + "a" * 256 + '"]}',
            "duplicate name": '{"format": "papaia.paperless-users/1", "users": ["anna", "anna"]}',
            "names differing in case": '{"format": "papaia.paperless-users/1", "users": ["anna", "Anna"]}',
            "trailing comma": '{"format": "papaia.paperless-users/1", "users": ["anna",]}',
            "trailing text": good + " x",
            "two documents": good + good,
            "unterminated": '{"format": "papaia.paperless-users/1", "users": ["anna"',
        }
        for why, raw in cases.items():
            with self.subTest(why):
                self.write_users(None, raw=raw)
                result = run_gate("render", env=self.env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.listed(), [], why)
                self.assertIn("users.json is not a valid name list; no name is admitted", result.stderr)
                self.assertNotIn("anna", result.stdout + result.stderr)

    def test_the_valid_form_of_each_malformed_case_is_accepted(self) -> None:
        # Guards the malformed cases above: they fail for their own reason only.
        self.write_users(None, raw='{\n\t"users" : [ "anna" , "Ben" ] ,\r\n "format":"papaia.paperless-users/1"}')
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(self.listed()), ["Ben", "anna"])

    def test_a_symlinked_list_admits_no_name(self) -> None:
        target = self.tmp / "elsewhere.json"
        target.write_text('{"format": "papaia.paperless-users/1", "users": ["anna"]}', encoding="utf-8")
        (self.users / "users.json").unlink()
        (self.users / "users.json").symlink_to(target)
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.listed(), [])
        self.assertIn("users.json is a symbolic link", result.stderr)

    def test_an_oversized_list_admits_no_name(self) -> None:
        names = [f"user{i:07d}" for i in range(70000)]
        self.write_users(names)
        self.assertGreater((self.users / "users.json").stat().st_size, 1024 * 1024)
        result = run_gate("render", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.listed(), [])
        self.assertIn("users.json is larger than", result.stderr)

    def test_names_are_kept_out_of_the_callers_map(self) -> None:
        self.write_users(["proxy", "mcp"])
        self.assertEqual(run_gate("render", env=self.env).returncode, 0)
        text = self.rendered()
        callers = re.search(r"map \$http_x_papaia_gate \$gate_caller \{(.*?)\}", text, re.DOTALL)
        self.assertNotIn('"proxy"', callers.group(1))
        self.assertNotIn('"mcp"', callers.group(1))
        self.assertEqual(sorted(self.listed()), ["mcp", "proxy"])


FAKE_NGINX = """#!/bin/sh
# Stands in for nginx: records reloads and the stop signal, runs like `daemon off`.
log="$GATE_RUN_DIR/nginx.calls"
case "$*" in
    *"-s reload"*) echo reload >> "$log"; exit 0 ;;
    *"-t"*) exit 0 ;;
esac
echo $$ > "$GATE_RUN_DIR/nginx.pid"
echo start >> "$log"
trap 'echo stop >> "$log"; exit 0' TERM
while :; do sleep 1; done
"""


class GateWatchesTheList(unittest.TestCase):
    """`gate.sh run` reloads the gate when the realm sync replaces the name list."""

    def setUp(self) -> None:
        self.script = GateScript("test_render_maps_each_callers_secret_to_its_name")
        self.script.setUp()
        self.addCleanup(self.script.tearDown)
        bin_dir = self.script.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "nginx"
        fake.write_text(FAKE_NGINX, encoding="utf-8")
        fake.chmod(0o755)
        self.run_dir = self.script.run_dir
        env = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            **self.script.env,
            "GATE_WATCH_SECONDS": "1",
        }
        self.proc = subprocess.Popen(
            ["sh", str(GATE_SCRIPT), "run"], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.addCleanup(self.stop)
        self.wait_for(lambda: "start" in self.calls(), "nginx started")

    def stop(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        self.proc.communicate(timeout=10)

    def calls(self) -> list[str]:
        path = self.run_dir / "nginx.calls"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def wait_for(self, check, what: str, seconds: float = 10) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for: {what}")

    def test_a_removed_name_is_refused_after_the_next_check(self) -> None:
        self.assertEqual(self.script.listed(), ["anna"])
        self.script.write_users(["ben"])
        self.wait_for(lambda: self.calls().count("reload") == 1, "reload after a new list")
        self.assertEqual(self.script.listed(), ["ben"])
        self.script.write_users([])
        self.wait_for(lambda: self.calls().count("reload") == 2, "reload after an empty list")
        self.assertEqual(self.script.listed(), [])

    def test_an_unchanged_list_causes_no_reload(self) -> None:
        self.script.write_users(["anna"])
        time.sleep(3)
        self.assertEqual(self.calls(), ["start"])

    def test_a_removed_list_closes_the_names(self) -> None:
        (self.script.users / "users.json").unlink()
        self.wait_for(lambda: "reload" in self.calls(), "reload after the list was removed")
        self.assertEqual(self.script.listed(), [])

    def test_a_render_failure_stops_the_gate(self) -> None:
        # A secret broken on disk must not leave the old names admitted.
        self.script.write_proxy("short\n")
        self.script.write_users(["ben"])
        self.wait_for(lambda: "stop" in self.calls(), "stop after a failed render")
        self.assertNotIn("reload", self.calls())
        # The container exits; its restart policy runs `gate.sh run` again, which
        # stops at the broken secret (fail closed).
        self.proc.wait(timeout=10)
        self.assertIn("proxy/secret", self.proc.stderr.read())


if __name__ == "__main__":
    unittest.main()
