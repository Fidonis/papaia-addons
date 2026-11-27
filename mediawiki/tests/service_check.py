#!/usr/bin/env python3
"""Service account checks for the mediawiki add-on.

Runs against a wiki started by run.sh. The service account signs in with
`action=login` and its bot password, creates, edits and deletes a page in the
Management tier, and its edits carry the bot flag. Its grants keep it from
moving or protecting pages. Special:BotPasswords is gone for everyone, the
break-glass admin included. Each --rejected LOGIN PASSWORD is a sign-in the
wiki must no longer accept (the password before a rotation, a former
MW_SERVICE_LOGIN). With an empty --password, only those are tried.

Standard library only. Exit status 0 when every check passes.
"""

import argparse
import secrets
import sys

from tier_check import TIER, Checks, Client


def bot_login(base: str, login: str, password: str) -> tuple[Client, dict]:
    client = Client(base)
    _, _, data = client.api(
        {"action": "login", "lgname": login, "lgpassword": password, "lgtoken": client.token("login")},
        post=True,
    )
    return client, data.get("login", {})


def finish(checks: Checks) -> int:
    print(f"\n{checks.count - len(checks.failures)}/{checks.count} checks passed")
    if checks.failures:
        print("Failed: " + "; ".join(checks.failures))
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="wiki URL, e.g. https://127.0.0.1:18020")
    ap.add_argument("--login", required=True, help="MW_SERVICE_LOGIN")
    ap.add_argument("--password", required=True, help="MW_SERVICE_PASSWORD; empty: only --rejected is tried")
    ap.add_argument("--admin", default="Admin")
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--rejected", nargs=2, action="append", default=[], metavar=("LOGIN", "PASSWORD"),
                    help="a sign-in that must be refused; repeatable")
    args = ap.parse_args()
    checks = Checks()
    print("service account")

    for login, password in args.rejected:
        _, result = bot_login(args.base, login, password)
        checks.ok(f"{login} with a former password is refused", result.get("result") == "Failed", f"({result})")
    if not args.password:
        return finish(checks)

    svc, result = bot_login(args.base, args.login, args.password)
    checks.ok("signs in with the bot password", result.get("result") == "Success", f"({result})")
    if result.get("result") != "Success":
        return finish(checks)

    title = f"{TIER}:Service check {secrets.token_hex(4)}"
    marker = secrets.token_hex(8)
    revids = []
    for text in (f"created {marker}", f"changed {marker}"):
        # bot=1, as an importer sets it; the flag needs the bot right.
        _, _, data = svc.api({"action": "edit", "title": title, "text": text, "summary": "service check",
                              "bot": "1", "token": svc.token()}, post=True)
        revids.append(data.get("edit", {}).get("newrevid"))
    _, _, data = svc.api(
        {"action": "query", "prop": "revisions", "titles": title, "rvprop": "content|user|flags",
         "rvslots": "main", "rvlimit": 1}
    )
    rev = data["query"]["pages"][0]["revisions"][0]
    checks.ok("creates and edits a page in a tier", rev["slots"]["main"]["content"] == f"changed {marker}")
    checks.ok("edits show the service account", rev["user"] == args.login.split("@")[0], f"({rev['user']})")
    _, _, data = svc.api({"action": "query", "list": "recentchanges", "rcprop": "ids|flags",
                          "rcnamespace": 3002, "rclimit": 50})
    flags = [rc for rc in data["query"]["recentchanges"] if rc["revid"] in revids]
    checks.ok("edits carry the bot flag", len(flags) == 2 and all(rc.get("bot") for rc in flags), f"({flags})")

    _, _, data = svc.api(
        {"action": "move", "from": title, "to": title + " moved", "token": svc.token()}, post=True)
    checks.ok("cannot move pages (no movepage grant)", "error" in data, f"({data})")
    _, _, data = svc.api(
        {"action": "protect", "title": title, "protections": "edit=sysop", "token": svc.token()}, post=True)
    checks.ok("cannot protect pages (no protect grant)", "error" in data, f"({data})")

    _, _, data = svc.api({"action": "delete", "title": title, "token": svc.token()}, post=True)
    checks.ok("deletes a page in a tier", "delete" in data, f"({data})")

    admin = Client(args.base)
    admin.login(args.admin, args.admin_password)
    for path in ("Special:BotPasswords", "Special:BotPasswords/other"):
        status, body, _ = admin.request("/index.php", {"title": path})
        checks.ok(f"{path} does not exist, even for the admin",
                  status == 404 and "wpappId" not in body, f"(HTTP {status})")

    return finish(checks)


if __name__ == "__main__":
    sys.exit(main())
