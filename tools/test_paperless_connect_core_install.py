"""Install the paperless-connect add-on on a core checkout and read what the core rendered.

paperless-connect runs the same tool server as `paperless` in front of an existing
Paperless-ngx that the customer runs behind their own proxy. It is not a module of the
suite: the catalog entry keeps `trust: addon`, and the chat reaches it only after the
operator allows it with the core's opt-in, through the core's chat relay. After
`addon install` and before that opt-in, the rendered LibreChat configuration has no
entry for `mcp-paperless`, the `librechat` client carries no `mcp-paperless` audience
mapper, the add-on ships no integration file and puts nothing of the core on its
network, and `addon check` reports the add-on compatible.

It needs a checkout of the core (papaia 1.10.0 or newer, with its tool environment) and
runs only when the variable names one:

    PAPAIA_CORE_CHECKOUT=/path/to/papaia python3 -m unittest discover -s tools \
        -p test_paperless_connect_core_install.py

Like tools/test_paperless_core_install.py, everything happens in a temporary directory:
no container is started, no port is published, no secret is printed.
"""

from __future__ import annotations

import unittest

from test_paperless_core_install import (
    AUDIENCE,
    CORE,
    REPO_ROOT,
    SERVER_URL,
    install_on_core,
)

ADDON = REPO_ROOT / "paperless-connect"


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


if __name__ == "__main__":
    unittest.main()
