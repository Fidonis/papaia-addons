"""Static checks of the document converters of the paperless add-on (Gotenberg, Tika).

Run with `python -m unittest discover -s tools`. The live proof of the same settings
is `tools/test_paperless_conversion_live.py`.

Both converters read untrusted documents from uploads and mail. Three layers keep a
document from making them send requests elsewhere, and each is checked here:

- Gotenberg's start flags (8.40.0, public 8.x documentation): only files Gotenberg
  writes below /tmp may be loaded by Chromium or LibreOffice, private and public
  addresses are refused, webhook and `downloadFrom` are off, JavaScript is off.
- Networks: each converter sits alone with Paperless in its own `internal: true`
  network, so it has no egress and no path to the database, Valkey or the other one.
- Containers: no capabilities, `no-new-privileges`, the image's unprivileged user,
  process and memory limits, read-only root file system with tmpfs for what it writes.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON = REPO_ROOT / "paperless"
GOTENBERG = "paperless-gotenberg"
TIKA = "paperless-tika"

# The flag table of the README, as the service must start. Each name exists in
# 8.40.0 (proven by the start of the pinned digest in the live test). No Chromium
# or LibreOffice deny list: the anchored allow lists already admit nothing but
# file:///tmp/, and Chromium keeps its built-in deny list for other file:// URLs.
REQUIRED_FLAGS = {
    "--chromium-disable-javascript": "true",
    "--chromium-allow-list": "^file:///tmp/.*",
    "--chromium-deny-private-ips": "true",
    "--chromium-deny-public-ips": "true",
    "--libreoffice-allow-list": "^file:///tmp/.*",
    "--libreoffice-deny-private-ips": "true",
    "--libreoffice-deny-public-ips": "true",
    "--webhook-disable": "true",
    "--api-disable-download-from": "true",
}

# Network key in the compose file -> service that has it besides Paperless.
CONVERTER_NETWORKS = {
    "paperless-gotenberg-net": GOTENBERG,
    "paperless-tika-net": TIKA,
}

# The unprivileged user each image declares (`docker image inspect`): Gotenberg
# `gotenberg` (1001), Tika 35002.
CONVERTER_USERS = {
    GOTENBERG: "1001:1001",
    TIKA: "35002:35002",
}


def compose() -> dict:
    return yaml.safe_load((ADDON / "docker-compose.yml").read_text(encoding="utf-8"))


def flags(command: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in command[1:]:
        name, sep, value = item.partition("=")
        if not sep or not name.startswith("--"):
            raise AssertionError(f"start flag without `--name=value` form: {item!r}")
        if name in out:
            raise AssertionError(f"start flag given twice: {name}")
        out[name] = value
    return out


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)([kmg])", str(value).lower())
    if not match:
        raise AssertionError(f"mem_limit not of the form <n>k|m|g: {value!r}")
    return int(match.group(1)) * {"k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2)]


class GotenbergFlags(unittest.TestCase):
    def test_start_flags_are_exactly_the_flag_table(self) -> None:
        command = compose()["services"][GOTENBERG]["command"]
        self.assertEqual(command[0], "gotenberg")
        self.assertEqual(flags(command), REQUIRED_FLAGS)

    def test_allow_lists_are_anchored(self) -> None:
        got = flags(compose()["services"][GOTENBERG]["command"])
        for name in ("--chromium-allow-list", "--libreoffice-allow-list"):
            with self.subTest(flag=name):
                pattern = re.compile(got[name])
                self.assertTrue(got[name].startswith("^"))
                self.assertTrue(pattern.match("file:///tmp/0f3c/index.html"))
                for url in (
                    "http://paperless-db:5432/file:///tmp/x",
                    "https://example.org/?file:///tmp/x",
                    "file:///etc/passwd",
                    "http://169.254.169.254/latest/meta-data/",
                ):
                    self.assertIsNone(pattern.search(url), url)

    def test_image_and_digest_are_unchanged(self) -> None:
        image = compose()["services"][GOTENBERG]["image"]
        self.assertIn(
            "docker.io/gotenberg/gotenberg:8.40.0@sha256:"
            "53073e798c8ef39ea2187e44cb67bab77894ea2509100b94c4fe3b461f7b2996",
            image,
        )

    def test_readme_flag_table_names_every_start_flag(self) -> None:
        readme = (ADDON / "README.md").read_text(encoding="utf-8")
        self.assertIn("\n## Document converters\n", readme)
        for name, value in REQUIRED_FLAGS.items():
            with self.subTest(flag=name):
                self.assertIn(f"`{name}={value}`", readme)
        for left_out in ("--libreoffice-deny-list", "--chromium-deny-list"):
            self.assertIn(f"`{left_out}`", readme)


class ConverterNetworks(unittest.TestCase):
    def setUp(self) -> None:
        self.data = compose()
        self.services = self.data["services"]

    def test_each_converter_has_only_its_own_internal_network(self) -> None:
        for network, service in CONVERTER_NETWORKS.items():
            with self.subTest(service=service):
                self.assertEqual(self.services[service]["networks"], [network])
                declared = self.data["networks"][network]
                self.assertIs(declared.get("internal"), True)
                self.assertEqual(declared.get("driver"), "bridge")
                self.assertEqual(declared.get("name"), "${PAPAIA_PROJECT:-papaia}-" + network)

    def test_converter_networks_are_shared_only_with_paperless(self) -> None:
        for network, service in CONVERTER_NETWORKS.items():
            with self.subTest(network=network):
                members = {
                    name for name, body in self.services.items()
                    if network in (body.get("networks") or [])
                }
                self.assertEqual(members, {"paperless", service})

    def test_paperless_keeps_its_network_and_joins_both_converter_networks(self) -> None:
        self.assertEqual(
            self.services["paperless"]["networks"],
            ["papaia-paperless-net", *CONVERTER_NETWORKS],
        )

    def test_no_converter_publishes_a_port_or_borrows_a_namespace(self) -> None:
        for service in CONVERTER_NETWORKS.values():
            with self.subTest(service=service):
                body = self.services[service]
                for key in ("ports", "expose", "network_mode", "volumes", "extra_hosts", "privileged"):
                    self.assertNotIn(key, body)

    def test_paperless_calls_the_converters_by_service_name(self) -> None:
        env = self.services["paperless"]["environment"]
        self.assertEqual(env["PAPERLESS_TIKA_GOTENBERG_ENDPOINT"], f"http://{GOTENBERG}:3000")
        self.assertEqual(env["PAPERLESS_TIKA_ENDPOINT"], f"http://{TIKA}:9998")

    def test_only_the_paperless_addon_runs_gotenberg(self) -> None:
        users = []
        for path in sorted(REPO_ROOT.glob("*/docker-compose.yml")):
            if path.parent.name == "n8n":
                continue
            if re.search(r"gotenberg/gotenberg", path.read_text(encoding="utf-8")):
                users.append(path.parent.name)
        self.assertEqual(users, ["paperless"])


class ConverterContainers(unittest.TestCase):
    def setUp(self) -> None:
        self.services = compose()["services"]

    def test_capabilities_and_privileges(self) -> None:
        for service in CONVERTER_USERS:
            with self.subTest(service=service):
                body = self.services[service]
                self.assertEqual(body.get("cap_drop"), ["ALL"])
                self.assertNotIn("cap_add", body)
                self.assertEqual(body.get("security_opt"), ["no-new-privileges:true"])

    def test_unprivileged_numeric_user_of_the_image(self) -> None:
        for service, user in CONVERTER_USERS.items():
            with self.subTest(service=service):
                self.assertEqual(self.services[service].get("user"), user)
                uid, _, gid = user.partition(":")
                self.assertNotEqual(int(uid), 0)
                self.assertNotEqual(int(gid), 0)

    def test_process_and_memory_limits(self) -> None:
        for service in CONVERTER_USERS:
            with self.subTest(service=service):
                body = self.services[service]
                self.assertIsInstance(body.get("pids_limit"), int)
                self.assertGreater(body["pids_limit"], 0)
                self.assertGreater(memory_bytes(body.get("mem_limit")), 256 * 1024**2)

    def test_read_only_root_with_tmpfs_for_tmp(self) -> None:
        for service in CONVERTER_USERS:
            with self.subTest(service=service):
                body = self.services[service]
                self.assertIs(body.get("read_only"), True)
                mounts = [entry.split(":", 1)[0] for entry in body.get("tmpfs") or []]
                self.assertIn("/tmp", mounts)

    def test_gotenberg_reports_its_health(self) -> None:
        check = self.services[GOTENBERG].get("healthcheck") or {}
        self.assertIn("http://127.0.0.1:3000/health", " ".join(check.get("test") or []))


if __name__ == "__main__":
    unittest.main()
