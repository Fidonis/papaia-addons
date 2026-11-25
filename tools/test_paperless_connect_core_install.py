"""Install the paperless-connect add-on on a core checkout and read what the core rendered.

paperless-connect runs the same tool server as `paperless` in front of an existing
Paperless-ngx that the customer runs behind their own proxy. It is not a module of the
suite: the catalog entry keeps `trust: addon`, and the chat reaches it only after the
operator allows it with the core's opt-in, through the core's chat relay. After
`addon install` and before that opt-in, the rendered LibreChat configuration has no
entry for `mcp-paperless`, the `librechat` client carries no `mcp-paperless` audience
mapper, the add-on ships no integration file and puts nothing of the core on its
network, and `addon check` reports the add-on compatible.

`PaperlessConnectChatOptInTests` then runs the operator's opt-in as the README
describes it: `addon chat allow paperless-connect` by one host account waits (the add-on
writes and deletes), a second host account approves it, and only then the chat entry
`mcp-paperless` exists and points to the core's chat relay, the relay's list names the
add-on's catalog entry and tools, and the relay alone joins the add-on's network; the
`librechat` client still carries no `mcp-paperless` audience. `addon chat revoke` removes
all of it again. Every decision is one audit row.

It needs a checkout of the core (papaia 1.10.0 or newer, with its tool environment) and
runs only when the variable names one:

    PAPAIA_CORE_CHECKOUT=/path/to/papaia python3 -m unittest discover -s tools \
        -p test_paperless_connect_core_install.py

Like tools/test_paperless_core_install.py, everything happens in a temporary directory:
no container is started, no port is published, no secret is printed.
"""

from __future__ import annotations

import json
import unittest

import yaml
from test_paperless_core_install import (
    AUDIENCE,
    CORE,
    REPO_ROOT,
    SERVER_URL,
    install_on_core,
)

ADDON = REPO_ROOT / "paperless-connect"
RELAY = "papaia-chat-relay"
RELAY_ORIGIN = f"http://{RELAY}:8080"
RELAY_URL = f"{RELAY_ORIGIN}/servers/{AUDIENCE}/mcp"
RELAY_OVERRIDE = "docker-compose.chat-relay-paperless-connect.override.yml"
NETWORK = "papaia-paperless-connect-net"
CAP = "professional_secret"


def declared_tools() -> dict[str, str]:
    """{operation: effect} of the add-on's catalog entry that the chat may call."""
    manifest = yaml.safe_load((ADDON / "papaia-app.yaml").read_text(encoding="utf-8"))
    [entry] = manifest["exposes"]
    return {op["name"]: op["effect"] for op in entry["operations"] if op.get("via") == "tools"}


def mapped_audiences(snapshot: dict) -> list[str]:
    return [
        m["config"].get("included.client.audience")
        for m in snapshot["mappers"].values()
        if m.get("protocolMapper") == "oidc-audience-mapper"
    ]


@unittest.skipUnless(CORE, "set PAPAIA_CORE_CHECKOUT to a core checkout to run this test")
class PaperlessConnectOnTheCoreTests(unittest.TestCase):
    result: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = install_on_core(cls, ADDON)

    def test_setup_and_install_succeed(self) -> None:
        self.assertEqual(self.result["setup"], 0)
        self.assertEqual(self.result["install"], 0)

    def test_catalog_lists_the_entry_outside_the_suite(self) -> None:
        # The core sets `trust`, never the add-on: paperless-connect is not in the
        # core's compatibility matrix, so it stays an add-on outside the suite.
        self.assertEqual(self.result["address"], SERVER_URL)
        self.assertEqual(self.result["trust"], "addon")

    def test_the_chat_has_no_entry_before_the_operator_opt_in(self) -> None:
        servers = self.result["servers"]
        self.assertNotIn(AUDIENCE, servers)
        # Nor under the name of the old fragment, nor under any other name.
        self.assertNotIn("Paperless", servers)
        self.assertEqual([k for k, v in servers.items() if v.get("url") == SERVER_URL], [])
        self.assertNotIn("http://paperless-mcp:8000", self.result["allowed"])

    def test_no_audience_mapper_on_the_librechat_client(self) -> None:
        # The shared chat token carries the audiences of the suite servers only; this
        # server gets a token for its own audience from the chat relay after the opt-in.
        self.assertNotIn(f"{AUDIENCE}-audience", self.result["mappers"])
        audiences = [
            m["config"].get("included.client.audience")
            for m in self.result["mappers"].values()
            if m.get("protocolMapper") == "oidc-audience-mapper"
        ]
        self.assertNotIn(AUDIENCE, audiences)

    def test_the_addon_puts_no_chat_on_its_network(self) -> None:
        # A direct network path from LibreChat would bypass the opt-in.
        self.assertEqual(self.result["chat_override"], [])

    def test_no_file_of_the_old_format_remains(self) -> None:
        self.assertEqual(self.result["addon_manifest_dirs"], [])
        self.assertEqual(self.result["addon_integration_files"], [])
        # The core creates the resource-server client in the realm sync.
        self.assertNotIn(AUDIENCE, self.result["realm_clients"])
        self.assertFalse(
            [line for line in self.result["install_out"] if "add protocol mapper" in line.lower()],
            self.result["install_out"],
        )

    def test_addon_check_reports_the_addon_compatible(self) -> None:
        self.assertEqual(self.result["check"], 0, self.result["check_out"])
        self.assertTrue(
            any("OK" in line for line in self.result["check_out"]), self.result["check_out"]
        )



@unittest.skipUnless(CORE, "set PAPAIA_CORE_CHECKOUT to a core checkout to run this test")
class PaperlessConnectChatOptInTests(unittest.TestCase):
    """The README's steps for chat access, run with the core's own commands."""

    result: dict
    steps: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = install_on_core(cls, ADDON, opt_in=True)
        cls.steps = cls.result["optin"]

    def assert_no_chat_path(self, snapshot: dict) -> None:
        self.assertNotIn(AUDIENCE, snapshot["servers"])
        self.assertEqual(
            [k for k, v in snapshot["servers"].items()
             if v.get("url") in (SERVER_URL, RELAY_URL)], [])
        self.assertIsNone(snapshot["relay_list"])
        self.assertEqual(snapshot["relay_overrides"], {})
        self.assertNotIn(AUDIENCE, mapped_audiences(snapshot))
        self.assertEqual(snapshot["chat_override"], [])

    def test_the_addon_runs_before_the_opt_in(self) -> None:
        self.assertEqual(self.steps["running"], 0)

    def test_show_names_the_audience_the_tools_the_cap_and_the_second_person(self) -> None:
        code, out = self.steps["show"]
        self.assertEqual(code, 0, out)
        self.assertIn(f"{AUDIENCE} (the relay's token carries this audience only)", out)
        self.assertIn("trust: addon", out)
        self.assertIn(f"up to {CAP}", out)
        for tool, effect in declared_tools().items():
            self.assertRegex(out, rf"\n  {tool} +{effect} ", tool)
        self.assertIn("A second host account must approve this opt-in (it writes or deletes)", out)

    def test_an_approval_for_another_audience_allows_nothing(self) -> None:
        code, out = self.steps["allow_wrong_audience"]
        self.assertEqual(code, 2, out)
        self.assertIn(f"serves '{AUDIENCE}'; nothing was allowed", out)

    def test_the_opt_in_waits_for_a_second_host_account(self) -> None:
        code, out = self.steps["allow"]
        self.assertEqual(code, 0, out)
        self.assertIn("waiting for approval: paperless-connect in the chat", out)
        self.assert_no_chat_path(self.steps["waiting"])
        code, out = self.steps["approve_same_person"]
        self.assertEqual(code, 2, out)
        self.assertIn("a second host account must approve it", out)

    def test_after_the_approval_the_chat_entry_points_to_the_relay(self) -> None:
        code, out = self.steps["approve"]
        self.assertEqual(code, 0, out)
        self.assertIn("allowed: paperless-connect in the chat, through the chat relay", out)
        allowed = self.steps["allowed"]
        entry = allowed["servers"][AUDIENCE]
        self.assertEqual(entry["url"], RELAY_URL)
        self.assertEqual(entry["type"], "streamable-http")
        self.assertEqual(entry["title"], "Document archive")
        self.assertTrue(entry["description"].endswith(f"Data class up to: {CAP}."), entry)
        self.assertEqual(entry["headers"], {"Authorization": "Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}"})
        self.assertIn(RELAY_ORIGIN, allowed["allowed"])
        # The chat never gets the add-on's own address, under any name.
        self.assertEqual(
            [k for k, v in allowed["servers"].items() if v.get("url") == SERVER_URL], [])
        self.assertNotIn("http://paperless-mcp:8000", allowed["allowed"])

    def test_after_the_approval_the_shared_chat_token_still_lacks_the_audience(self) -> None:
        allowed = self.steps["allowed"]
        self.assertNotIn(f"{AUDIENCE}-audience", allowed["mappers"])
        self.assertNotIn(AUDIENCE, mapped_audiences(allowed))
        # The core creates no client of its own for the add-on's audience in the import.
        self.assertNotIn(AUDIENCE, allowed["realm_clients"])

    def test_the_relay_lists_the_catalog_entry_with_its_tools(self) -> None:
        listed = self.steps["allowed"]["relay_list"]
        self.assertIsNotNone(listed)
        [row] = listed["servers"]
        self.assertEqual(row["audience"], AUDIENCE)
        self.assertEqual(row["scope"], f"aud-{AUDIENCE}")
        self.assertEqual(row["addon"], "paperless-connect")
        self.assertEqual(row["catalog_id"], "paperless.documents")
        self.assertEqual(row["address"], SERVER_URL)
        self.assertEqual(row["data_class_max"], CAP)
        self.assertEqual({t["name"]: t["effect"] for t in row["tools"]}, declared_tools())

    def test_only_the_relay_joins_the_addon_network(self) -> None:
        allowed = self.steps["allowed"]
        self.assertEqual(
            allowed["relay_overrides"],
            {RELAY_OVERRIDE: {
                "services": {RELAY: {"networks": [NETWORK]}},
                "networks": {NETWORK: {"external": True}},
            }},
        )
        self.assertEqual(allowed["chat_override"], [])

    def test_the_list_shows_who_requested_and_who_approved(self) -> None:
        code, out = self.steps["list"]
        self.assertEqual(code, 0, out)
        [row] = json.loads(out)["addons"]
        self.assertEqual(
            {k: row[k] for k in ("addon", "audience", "catalog_id", "state",
                                 "requested_by", "confirmed_by")},
            {"addon": "paperless-connect", "audience": AUDIENCE,
             "catalog_id": "paperless.documents", "state": "allowed",
             "requested_by": "host:anna", "confirmed_by": "host:ben"},
        )
        self.assertEqual(set(row["effects"]), {"r", "w", "d"})

    def test_revoke_removes_the_chat_entry_the_list_and_the_network(self) -> None:
        code, out = self.steps["revoke"]
        self.assertEqual(code, 0, out)
        self.assertIn("revoked: paperless-connect in the chat", out)
        self.assert_no_chat_path(self.steps["revoked"])

    def test_every_decision_is_one_audit_row(self) -> None:
        decisions = [r for r in self.steps["audit"]
                     if r["operation"] in ("chat_relay.allow", "chat_relay.approve",
                                           "chat_relay.revoke")]
        self.assertEqual(
            [(r["operation"], r["result"], r["error_code"], r["sub"]) for r in decisions],
            [
                ("chat_relay.allow", "denied", "approval_mismatch", "host:anna"),
                ("chat_relay.allow", "allowed", None, "host:anna"),
                ("chat_relay.approve", "denied", "same_person", "host:anna"),
                ("chat_relay.approve", "allowed", None, "host:ben"),
                ("chat_relay.revoke", "allowed", None, "host:ben"),
            ],
        )
        for row in decisions:
            self.assertEqual(row["object_ref"], {"type": "addon", "id": "paperless-connect"})
            self.assertEqual(row["data_class"], CAP)
        ids = [r["correlation_id"] for r in decisions]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(ids))


if __name__ == "__main__":
    unittest.main()
