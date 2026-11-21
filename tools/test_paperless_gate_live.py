"""Live test of the Paperless gate with the add-on's own compose file and real Paperless.

Runs only when both variables are set (and Docker is available):

    PAPERLESS_GATE_LIVE_PROJECT  Compose project name, for example `itk-gate`
    PAPERLESS_GATE_LIVE_SUBNET   a free IPv4 /24, for example `10.210.49.0/24`

    PAPERLESS_GATE_LIVE_PROJECT=itk-gate PAPERLESS_GATE_LIVE_SUBNET=10.210.49.0/24 \\
      python -m unittest tools/test_paperless_gate_live.py

The stack is `paperless/docker-compose.yml` plus an override written at run time: no
`.env`, Tika and Gotenberg left out, and probe containers that stand in for the proxy
(with the proxy's secret), the MCP server (with the gate's MCP secret volume), a stranger
on the Paperless network, a container in the host network and one on a foreign Compose
network. The name list of the realm sync is written by the test the way the core writes
it. All secrets are generated per run. No port is published; the project is removed with
`down -v` at the end.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON = REPO_ROOT / "paperless"
PROJECT = os.environ.get("PAPERLESS_GATE_LIVE_PROJECT", "")
SUBNET = os.environ.get("PAPERLESS_GATE_LIVE_SUBNET", "")
PROBE_IMAGE = "docker.io/library/python:3.12-alpine"
ADMIN = "gate-admin"
# Listed person; Paperless creates the account on the first call through the gate.
ANNA = "anna-" + secrets.token_hex(3)
# Names equal to parameters of nginx' map must stay plain names.
LISTED = [ANNA, "default", "include"]
# The gate rereads the list every 5 seconds; a reload takes a moment more.
LIST_DELAY = 12
PROFILE = "http://paperless:8001/api/profile/"

# Sends one request and prints a JSON line: {"status": <int>|"refused"|"unreachable",
# "count": <documents or users>|null, "error": <error code of a refusal>|null}. A header value "@<path>" is read from that file
# inside the container, so a secret never shows up in a process list.
PROBE = r"""
import json, socket, sys, urllib.error, urllib.request
url, raw = sys.argv[1], json.loads(sys.argv[2])
headers = {}
for name, value in raw.items():
    if value.startswith("@"):
        value = open(value[1:], encoding="ascii").read().strip()
    headers[name] = value
request = urllib.request.Request(url, headers=headers)
out = {"status": None, "count": None, "error": None}
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        out["status"] = response.status
        body = response.read()
except urllib.error.HTTPError as error:
    out["status"] = error.code
    body = error.read()
except (ConnectionRefusedError, ConnectionResetError):
    out["status"] = "refused"
    body = b""
except urllib.error.URLError as error:
    reason = error.reason
    out["status"] = "refused" if isinstance(reason, ConnectionRefusedError) else "unreachable"
    body = b""
except (socket.timeout, TimeoutError, OSError):
    out["status"] = "unreachable"
    body = b""
try:
    parsed = json.loads(body)
    out["count"] = parsed.get("count")
    if isinstance(parsed.get("error"), str):
        out["error"] = parsed["error"]
except Exception:
    pass
print(json.dumps(out))
"""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


@unittest.skipUnless(
    PROJECT and SUBNET and docker_available(),
    "set PAPERLESS_GATE_LIVE_PROJECT and PAPERLESS_GATE_LIVE_SUBNET (needs Docker)",
)
class LiveGate(unittest.TestCase):
    tmp: Path
    env: dict[str, str]
    proxy_secret: str
    files: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        net = ipaddress.ip_network(SUBNET)
        inner, front, foreign, _ = net.subnets(new_prefix=net.prefixlen + 2)
        cls.tmp = Path(tempfile.mkdtemp(prefix="paperless-gate-"))
        config = cls.tmp / "config"
        (config / "certs").mkdir(parents=True)
        secret_dir = config / "secrets" / "paperless-gate" / "proxy"
        secret_dir.mkdir(parents=True, mode=0o700)
        cls.proxy_secret = secrets.token_urlsafe(32)
        cls.write_proxy_secret(cls.proxy_secret)
        (config / "security" / "paperless-gate").mkdir(parents=True)
        (config / "security" / "paperless-gate").chmod(0o755)
        cls.write_users(LISTED)
        for name in ("media", "export", "consume"):
            (cls.tmp / name).mkdir()
        override = cls.tmp / "override.yml"
        override.write_text(
            f"""\
services:
  paperless:
    env_file: !reset []
    depends_on: !override [paperless-db, paperless-broker]
    environment:
      PAPERLESS_SECRET_KEY: "{secrets.token_urlsafe(48)}"
      PAPERLESS_TIKA_ENABLED: "0"
      # Without the bundle's .env, the database password comes from here.
      PAPERLESS_DBPASS: "${{PAPERLESS_DBPASS}}"
      REQUESTS_CA_BUNDLE: ""
  paperless-db:
    environment:
      POSTGRES_PASSWORD: "${{PAPERLESS_DBPASS}}"
  probe-proxy:
    image: {PROBE_IMAGE}
    command: ["sleep", "infinity"]
    networks: [papaia-paperless-net]
    volumes:
      - {secret_dir}:/s:ro
  probe-mcp:
    image: {PROBE_IMAGE}
    command: ["sleep", "infinity"]
    networks: [papaia-paperless-net, paperless-front]
    volumes:
      - paperless-gate-mcp:/s:ro
  probe-stranger:
    image: {PROBE_IMAGE}
    command: ["sleep", "infinity"]
    networks: [papaia-paperless-net]
  probe-host:
    image: {PROBE_IMAGE}
    command: ["sleep", "infinity"]
    network_mode: host
  probe-foreign:
    image: {PROBE_IMAGE}
    command: ["sleep", "infinity"]
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks: [foreign]
networks:
  papaia-paperless-net:
    ipam:
      config: [{{subnet: "{inner}"}}]
  paperless-front:
    ipam:
      config: [{{subnet: "{front}"}}]
  foreign:
    name: {PROJECT}-foreign
    ipam:
      config: [{{subnet: "{foreign}"}}]
""",
            encoding="utf-8",
        )
        cls.files = ["-f", str(ADDON / "docker-compose.yml"), "-f", str(override)]
        cls.env = {
            **os.environ,
            "PAPAIA_PROJECT": PROJECT,
            "PAPAIA_CONFIG_DIR": str(config),
            "OIDC_ISSUER": "http://127.0.0.1:9/realms/papaia",
            "PAPERLESS_PUBLIC_URL": "http://paperless.invalid",
            "PAPERLESS_DBPASS": secrets.token_urlsafe(24),
            "PAPERLESS_ADMIN_USER": ADMIN,
            "PAPERLESS_ADMIN_PASSWORD": secrets.token_urlsafe(24),
            "KC_PAPERLESS_CLIENT_SECRET": secrets.token_urlsafe(24),
            "MEDIA_DIRECTORY": str(cls.tmp / "media"),
            "EXPORT_DIRECTORY": str(cls.tmp / "export"),
            "CONSUME_DIRECTORY": str(cls.tmp / "consume"),
        }
        # Registered before `up`: unittest skips tearDownClass when setUpClass fails,
        # but runs class cleanups, so a stack that did not come up is removed too.
        cls.addClassCleanup(cls.remove_stack)
        cls.compose(
            "up", "-d", "--wait-timeout", "300", "paperless", "paperless-gate",
            "probe-proxy", "probe-mcp", "probe-stranger", "probe-host", "probe-foreign",
        )
        cls.wait_for_paperless()

    @classmethod
    def remove_stack(cls) -> None:
        cls.compose("down", "-v", "--remove-orphans", check=False)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def write_proxy_secret(cls, value: str) -> None:
        path = cls.tmp / "config" / "secrets" / "paperless-gate" / "proxy" / "secret"
        part = path.with_name(".secret.new")
        part.write_text(value + "\n", encoding="ascii")
        part.chmod(0o600)
        part.replace(path)

    @classmethod
    def write_users(cls, users: list[str]) -> None:
        """As the realm sync writes it: beside the old list, then renamed into place."""
        path = cls.tmp / "config" / "security" / "paperless-gate" / "users.json"
        part = path.with_name(".users.json.new")
        doc = {"format": "papaia.paperless-users/1", "users": sorted(users)}
        part.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        part.chmod(0o644)
        part.replace(path)

    @classmethod
    def accounts(cls, name: str) -> int:
        """Number of Paperless accounts with exactly this user name, read in its database."""
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name):
            raise ValueError("test names are letters, digits and hyphens only")
        result = cls.compose(
            "exec", "-T", "paperless-db", "psql", "-U", "paperless", "-d", "paperless", "-tAc",
            f"SELECT count(*) FROM auth_user WHERE username = '{name}'",
        )
        return int(result.stdout.strip())

    @classmethod
    def compose(cls, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["docker", "compose", "-p", PROJECT, "--project-directory", str(ADDON), *cls.files,
             *args],
            env=cls.env, capture_output=True, text=True, timeout=600, check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"docker compose {args[0]} failed: {result.stderr[-2000:]}")
        return result

    @classmethod
    def probe(cls, service: str, url: str, headers: dict[str, str] | None = None) -> dict:
        result = cls.compose(
            "exec", "-T", service, "python", "-c", PROBE, url, json.dumps(headers or {})
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    @classmethod
    def wait_for_paperless(cls) -> None:
        deadline = time.monotonic() + 420
        last = None
        while time.monotonic() < deadline:
            last = cls.probe("probe-proxy", "http://paperless:8001/api/", {"X-Papaia-Gate": "@/s/secret"})
            if last["status"] in (200, 401, 403):
                return
            time.sleep(5)
        logs = cls.compose("logs", "--tail", "40", "paperless", "paperless-gate", check=False)
        raise AssertionError(f"Paperless did not come up: {last}\n{logs.stdout[-3000:]}")

    def container_ip(self, service: str) -> str:
        cid = self.compose("ps", "-q", service).stdout.strip()
        out = subprocess.run(
            ["docker", "inspect", cid, "--format", "{{json .NetworkSettings.Networks}}"],
            capture_output=True, text=True, check=True,
        ).stdout
        networks = json.loads(out)
        return networks[f"{PROJECT}-paperless-net"]["IPAddress"]

    # --- requests that must not reach Paperless with an identity --------------------

    def test_paperless_port_is_closed_on_its_own_network(self) -> None:
        got = self.probe("probe-stranger", "http://paperless:8000/api/documents/",
                         {"X-Papaia-Remote-User": ADMIN})
        self.assertEqual(got["status"], "refused")

    def test_gate_refuses_a_request_without_a_secret(self) -> None:
        for headers in (
            {"X-Papaia-Remote-User": ADMIN},
            {"X-Papaia-Remote-User": ADMIN, "X-Papaia-Gate": secrets.token_urlsafe(32)},
            {"X_Papaia_Remote_User": ADMIN},
            {},
        ):
            with self.subTest(headers=sorted(headers)):
                got = self.probe("probe-stranger", "http://paperless:8001/api/documents/", headers)
                self.assertEqual(got["status"], 403)
                self.assertIsNone(got["count"])

    def test_host_network_container_gets_no_document(self) -> None:
        ip = self.container_ip("paperless")
        got = self.probe("probe-host", f"http://{ip}:8000/api/documents/",
                         {"X-Papaia-Remote-User": ADMIN})
        self.assertIn(got["status"], ("refused", "unreachable"))
        got = self.probe("probe-host", f"http://{ip}:8001/api/documents/",
                         {"X-Papaia-Remote-User": ADMIN})
        self.assertIn(got["status"], (403, "unreachable"))
        self.assertIsNone(got["count"])

    def test_foreign_network_cannot_reach_paperless(self) -> None:
        ip = self.container_ip("paperless")
        for url in (
            "http://paperless:8001/api/documents/",
            f"http://{ip}:8001/api/documents/",
            f"http://{ip}:8000/api/documents/",
            # The former host port; on Docker Desktop a 127.0.0.1 binding would not
            # protect it from other containers.
            "http://host.docker.internal:8010/api/documents/",
        ):
            with self.subTest(url=url):
                got = self.probe("probe-foreign", url, {"X-Papaia-Remote-User": ADMIN})
                self.assertIn(got["status"], ("refused", "unreachable"))

    def test_identity_header_from_the_proxy_has_no_effect(self) -> None:
        # The proxy's secret is not the MCP server's: even a listed name is dropped.
        for name in (ADMIN, ANNA):
            with self.subTest(name=name):
                got = self.probe("probe-proxy", "http://paperless:8001/api/documents/",
                                 {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": name})
                self.assertIn(got["status"], (401, 403))
                self.assertIsNone(got["count"])
                self.assertIsNone(got["error"])

    def test_unknown_name_from_the_proxy_creates_no_account(self) -> None:
        name = "mallory" + secrets.token_hex(4)
        self.probe("probe-proxy", "http://paperless:8001/api/documents/",
                   {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": name})
        self.assertEqual(self.accounts(name), 0)

    # --- the name list (ISB-T73) ---------------------------------------------------------

    def test_unknown_name_is_refused_with_a_valid_mcp_secret(self) -> None:
        name = "mallory" + secrets.token_hex(4)
        got = self.probe("probe-mcp", "http://paperless:8001/api/documents/",
                         {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": name})
        self.assertEqual(got, {"status": 403, "count": None, "error": "paperless_unknown_user"})
        self.assertEqual(self.accounts(name), 0)

    def test_superuser_name_not_on_the_list_is_refused(self) -> None:
        got = self.probe("probe-mcp", "http://paperless:8001/api/documents/",
                         {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": ADMIN})
        self.assertEqual(got, {"status": 403, "count": None, "error": "paperless_unknown_user"})

    def test_a_listed_name_in_other_case_is_refused(self) -> None:
        # nginx compares map strings without regard to case, Paperless does not.
        for name in (ANNA.upper(), ANNA.capitalize()):
            with self.subTest(name=name):
                got = self.probe("probe-mcp", "http://paperless:8001/api/documents/",
                                 {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": name})
                self.assertEqual(got, {"status": 403, "count": None, "error": "paperless_unknown_user"})
                self.assertEqual(self.accounts(name), 0)

    def test_unknown_name_writes_one_audit_row_with_the_correlation_id(self) -> None:
        name = "mallory" + secrets.token_hex(4)
        cid = "add005-" + secrets.token_hex(8)
        self.probe("probe-mcp", "http://paperless:8001/api/documents/?query=secret-text",
                   {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": name,
                    "X-Papaia-Correlation-Id": cid})
        logs = self.compose("logs", "--no-log-prefix", "paperless-gate").stdout
        rows = [json.loads(line) for line in logs.splitlines()
                if line.startswith("{") and '"papaia.audit/1"' in line and cid in line]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["event"], "paperless_unknown_user")
        self.assertEqual(row["result"], "denied")
        self.assertEqual(row["error_code"], "paperless_unknown_user")
        self.assertEqual(row["writer"], "paperless/paperless-gate")
        self.assertRegex(row["time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertNotIn(name, logs)
        self.assertNotIn("secret-text", logs)

    def test_a_key_shaped_correlation_id_is_replaced(self) -> None:
        name = "mallory" + secrets.token_hex(4)
        cid = "req-AKIA" + secrets.token_hex(8).upper()
        self.probe("probe-mcp", "http://paperless:8001/api/documents/",
                   {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": name,
                    "X-Papaia-Correlation-Id": cid})
        logs = self.compose("logs", "--no-log-prefix", "paperless-gate").stdout
        self.assertNotIn(cid, logs)
        self.assertRegex(logs, r'"correlation_id":"[0-9a-f]{32}"')

    def test_no_container_publishes_a_port(self) -> None:
        ids = self.compose("ps", "-q").stdout.split()
        self.assertTrue(ids)
        for cid in ids:
            ports = subprocess.run(["docker", "port", cid], capture_output=True, text=True, check=False).stdout
            self.assertEqual(ports.strip(), "", cid)

    # --- the two callers ---------------------------------------------------------------

    def test_browser_path_through_the_proxy_works(self) -> None:
        got = self.probe("probe-proxy", "http://paperless:8001/accounts/login/",
                         {"X-Papaia-Gate": "@/s/secret"})
        self.assertEqual(got["status"], 200)

    def test_mcp_server_acts_as_the_listed_person(self) -> None:
        # A new account holds no Paperless permission yet; its own profile needs none.
        got = self.probe("probe-mcp", PROFILE, {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": ANNA})
        self.assertEqual(got["status"], 200)
        self.assertEqual(self.accounts(ANNA), 1)

    def test_malformed_name_from_the_mcp_server_is_refused(self) -> None:
        got = self.probe("probe-mcp", "http://paperless:8001/api/documents/",
                         {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": "a b"})
        self.assertEqual(got["status"], 400)

    def test_gate_log_names_callers_and_holds_no_secret(self) -> None:
        self.probe("probe-proxy", "http://paperless:8001/accounts/login/", {"X-Papaia-Gate": "@/s/secret"})
        logs = self.compose("logs", "--no-log-prefix", "paperless-gate").stdout
        mcp_secret = self.compose("exec", "-T", "probe-mcp", "cat", "/s/secret").stdout.strip()
        self.assertIn('"caller":"proxy"', logs)
        self.assertNotIn(self.proxy_secret, logs)
        self.assertNotIn(mcp_secret, logs)
        self.assertNotIn(ADMIN, logs)
        self.assertNotIn(ANNA, logs)

    # --- list changes (late: they change the list) ------------------------------------

    def test_y_list_changes_take_effect_without_a_restart(self) -> None:
        ben = "ben-" + secrets.token_hex(3)
        headers = {"X-Papaia-Gate": "@/s/secret"}
        self.write_users([*LISTED, ben])
        deadline = time.monotonic() + LIST_DELAY
        while time.monotonic() < deadline:
            got = self.probe("probe-mcp", PROFILE, {**headers, "X-Papaia-Remote-User": ben})
            if got["status"] == 200:
                break
            time.sleep(1)
        self.assertEqual(got["status"], 200, "a name added by the realm sync is admitted")
        # Removed from the realm: refused after the next check, account untouched.
        self.write_users(LISTED)
        deadline = time.monotonic() + LIST_DELAY
        while time.monotonic() < deadline:
            got = self.probe("probe-mcp", PROFILE, {**headers, "X-Papaia-Remote-User": ben})
            if got["status"] == 403:
                break
            time.sleep(1)
        self.assertEqual(got, {"status": 403, "count": None, "error": "paperless_unknown_user"})
        got = self.probe("probe-mcp", PROFILE, {**headers, "X-Papaia-Remote-User": ANNA})
        self.assertEqual(got["status"], 200)

    # --- rotation (last: changes the secrets) -----------------------------------------

    def test_z_rotation(self) -> None:
        old_mcp = self.compose("exec", "-T", "probe-mcp", "cat", "/s/secret").stdout.strip()
        out = self.compose("exec", "-T", "paperless-gate", "sh", "/gate/gate.sh", "rotate", "mcp")
        self.assertIn('"operation":"gate.secret_rotated"', out.stdout)
        got = self.probe("probe-stranger", "http://paperless:8001/api/documents/",
                         {"X-Papaia-Gate": old_mcp, "X-Papaia-Remote-User": ANNA})
        self.assertEqual(got["status"], 403)
        got = self.probe("probe-mcp", PROFILE, {"X-Papaia-Gate": "@/s/secret", "X-Papaia-Remote-User": ANNA})
        self.assertEqual(got["status"], 200)

        old_proxy = type(self).proxy_secret
        type(self).proxy_secret = secrets.token_urlsafe(32)
        self.write_proxy_secret(type(self).proxy_secret)
        self.compose("exec", "-T", "paperless-gate", "sh", "/gate/gate.sh", "reload")
        got = self.probe("probe-stranger", "http://paperless:8001/accounts/login/",
                         {"X-Papaia-Gate": old_proxy})
        self.assertEqual(got["status"], 403)
        got = self.probe("probe-proxy", "http://paperless:8001/accounts/login/",
                         {"X-Papaia-Gate": "@/s/secret"})
        self.assertEqual(got["status"], 200)


if __name__ == "__main__":
    unittest.main()
