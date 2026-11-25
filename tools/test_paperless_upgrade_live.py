"""Live upgrade test of the paperless add-on: Paperless-ngx 3.4.2 to 3.5.1 on the same volumes.

Runs only when both variables are set (and Docker is available):

    PAPERLESS_UPGRADE_LIVE_PROJECT  Compose project name, for example `itk-upgrade`
    PAPERLESS_UPGRADE_LIVE_SUBNET   a free IPv4 /24, for example `10.212.121.0/24`
    PAPERLESS_UPGRADE_LIVE_PROTOCOL optional: path of the JSON protocol to write

    PAPERLESS_UPGRADE_LIVE_PROJECT=itk-upgrade PAPERLESS_UPGRADE_LIVE_SUBNET=10.212.121.0/24 \\
      python3 -m unittest discover -s tools -p test_paperless_upgrade_live.py

The stack is `paperless/docker-compose.yml` plus an override written at run time, as in
tools/test_paperless_gate_live.py: no `.env`, Tika and Gotenberg left out, probe containers
for the proxy and the MCP server, secrets generated per run, no published port.

Phase 1 starts Paperless-ngx 3.4.2 and writes synthetic data through the API: an admin, two
users, a group with object permissions, a tag tree of three levels, a correspondent, a
document type, a custom field, a storage path, a saved view and six documents (PDF with a
text layer and plain text) with owners, tags, notes and custom field values. Phase 2 removes
the containers but not the volumes and starts the add-on's default image, 3.5.1. Phase 3
reads everything again and compares (tools/paperless_upgrade.py). Findings name document
ids and user names, never content. The project is removed with `down -v` at the end, also
when a phase fails.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

import paperless_upgrade as pu

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON = REPO_ROOT / "paperless"
PROJECT = os.environ.get("PAPERLESS_UPGRADE_LIVE_PROJECT", "")
SUBNET = os.environ.get("PAPERLESS_UPGRADE_LIVE_SUBNET", "")
PROTOCOL = os.environ.get("PAPERLESS_UPGRADE_LIVE_PROTOCOL", "")
FROM_IMAGE = ("ghcr.io/paperless-ngx/paperless-ngx:3.4.2"
              "@sha256:d704e84f88300eb6ab001ab15720a2330ff99169cf9f94d6b8fcc44b63a84c84")
FROM_VERSION = "3.4.2"
TO_VERSION = "3.5.1"
PROBE_IMAGE = "docker.io/library/python:3.12-alpine"
ADMIN = "upgrade-admin"
USER_A = "upgrade-a-" + secrets.token_hex(3)
USER_B = "upgrade-b-" + secrets.token_hex(3)
STRANGER = "mallory-" + secrets.token_hex(4)
# Words that occur only in the synthetic documents; one search per word.
WORDS = {"s1": "quillmarsh", "s2": "brindlecove", "s3": "fenwickstone"}
TASK_TIMEOUT = 600
START_TIMEOUT = 900

# Runs a batch of requests read from stdin as JSON and prints one JSON list of results:
# {"status", "json", "sha256" (of the body, for downloads), "version" (X-Version)}. The
# gate's secret is read inside the container, so it never shows up in a process list.
CLIENT = r"""
import base64, hashlib, json, sys, urllib.error, urllib.request
jobs = json.load(sys.stdin)
gate = open("/s/secret", encoding="ascii").read().strip()
out = []
for job in jobs:
    headers = dict(job.get("headers") or {})
    if job.get("gate", True):
        headers["X-Papaia-Gate"] = gate
    data = None
    if job.get("json") is not None:
        data = json.dumps(job["json"]).encode()
        headers["Content-Type"] = "application/json"
    elif job.get("body") is not None:
        data = base64.b64decode(job["body"])
        headers["Content-Type"] = job["content_type"]
    request = urllib.request.Request("http://paperless:8001" + job["path"], data=data,
                                     headers=headers, method=job.get("method", "GET"))
    res = {"status": None, "json": None, "sha256": None, "version": None}
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            res["status"] = response.status
            res["version"] = response.headers.get("X-Version")
            body = response.read()
    except urllib.error.HTTPError as error:
        res["status"] = error.code
        body = error.read()
    except OSError:
        res["status"] = "unreachable"
        body = b""
    if job.get("hash"):
        res["sha256"] = hashlib.sha256(body).hexdigest() if res["status"] == 200 else None
    else:
        try:
            res["json"] = json.loads(body)
        except ValueError:
            pass
    out.append(res)
print(json.dumps(out))
"""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class ApiError(AssertionError):
    pass


@unittest.skipUnless(
    PROJECT and SUBNET and docker_available(),
    "set PAPERLESS_UPGRADE_LIVE_PROJECT and PAPERLESS_UPGRADE_LIVE_SUBNET (needs Docker)",
)
class LiveUpgrade(unittest.TestCase):
    tmp: Path
    env: dict[str, str]
    files: list[str]
    passwords: dict[str, str]
    tokens: dict[str, str]
    ids: dict[str, int]
    expected_visible: dict[str, list[int]]
    expected_searches: dict[str, list[int]]
    before: dict
    after: dict
    versions: dict[str, str | None]
    migrations: list[str]
    images: dict[str, str]
    checks: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.images = {}
        cls.versions = {}
        cls.migrations = []
        cls.tmp = Path(tempfile.mkdtemp(prefix="paperless-upgrade-"))
        cls.addClassCleanup(cls.write_protocol)
        cls.write_stack()
        # Registered before `up`: unittest runs class cleanups also when setUpClass fails.
        cls.addClassCleanup(cls.remove_stack)
        # Phase 1: 3.4.2 with synthetic data.
        cls.env["PAPERLESS_IMAGE"] = FROM_IMAGE
        cls.start()
        cls.versions["before"] = cls.server_version()
        cls.images["before"] = cls.running_image()
        cls.seed()
        cls.before = cls.snapshot(cls.versions["before"])
        # Phase 2: same volumes, the add-on's default image.
        cls.compose("down")
        del cls.env["PAPERLESS_IMAGE"]
        cls.start()
        cls.versions["after"] = cls.server_version()
        cls.images["after"] = cls.running_image()
        logs = cls.compose("logs", "--no-log-prefix", "paperless", check=False).stdout
        cls.migrations = pu.migration_lines(logs)
        # Phase 3: read everything again.
        cls.after = cls.snapshot(cls.versions["after"])

    # --- stack ----------------------------------------------------------------------------

    @classmethod
    def write_stack(cls) -> None:
        net = ipaddress.ip_network(SUBNET)
        inner, front, _, _ = net.subnets(new_prefix=net.prefixlen + 2)
        config = cls.tmp / "config"
        (config / "certs").mkdir(parents=True)
        secret_dir = config / "secrets" / "paperless-gate" / "proxy"
        secret_dir.mkdir(parents=True, mode=0o700)
        secret = secret_dir / "secret"
        secret.write_text(secrets.token_urlsafe(32) + "\n", encoding="ascii")
        secret.chmod(0o600)
        users_dir = config / "security" / "paperless-gate"
        users_dir.mkdir(parents=True)
        users_dir.chmod(0o755)
        users = users_dir / "users.json"
        users.write_text(json.dumps({"format": "papaia.paperless-users/1",
                                     "users": sorted([USER_A])}) + "\n", encoding="utf-8")
        users.chmod(0o644)
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
networks:
  papaia-paperless-net:
    ipam:
      config: [{{subnet: "{inner}"}}]
  paperless-front:
    ipam:
      config: [{{subnet: "{front}"}}]
""",
            encoding="utf-8",
        )
        cls.files = ["-f", str(ADDON / "docker-compose.yml"), "-f", str(override)]
        cls.passwords = {name: secrets.token_urlsafe(24) for name in (ADMIN, USER_A, USER_B)}
        cls.env = {
            **os.environ,
            "PAPAIA_PROJECT": PROJECT,
            "PAPAIA_CONFIG_DIR": str(config),
            "OIDC_ISSUER": "http://127.0.0.1:9/realms/papaia",
            "PAPERLESS_PUBLIC_URL": "http://paperless.invalid",
            "PAPERLESS_DBPASS": secrets.token_urlsafe(24),
            "PAPERLESS_ADMIN_USER": ADMIN,
            "PAPERLESS_ADMIN_PASSWORD": cls.passwords[ADMIN],
            "KC_PAPERLESS_CLIENT_SECRET": secrets.token_urlsafe(24),
            "MEDIA_DIRECTORY": str(cls.tmp / "media"),
            "EXPORT_DIRECTORY": str(cls.tmp / "export"),
            "CONSUME_DIRECTORY": str(cls.tmp / "consume"),
        }
        cls.env.pop("PAPERLESS_IMAGE", None)

    @classmethod
    def remove_stack(cls) -> None:
        cls.compose("down", "-v", "--remove-orphans", check=False)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def compose(cls, *args: str, check: bool = True, stdin: str | None = None,
                ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["docker", "compose", "-p", PROJECT, "--project-directory", str(ADDON), *cls.files,
             *args],
            env=cls.env, capture_output=True, text=True, timeout=900, check=False, input=stdin,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"docker compose {args[0]} failed: {result.stderr[-2000:]}")
        return result

    @classmethod
    def start(cls) -> None:
        cls.compose("up", "-d", "paperless", "paperless-gate", "probe-proxy", "probe-mcp")
        deadline = time.monotonic() + START_TIMEOUT
        last = None
        while time.monotonic() < deadline:
            last = cls.call([{"path": "/api/", "headers": basic(ADMIN, cls.passwords[ADMIN])}])[0]
            if last["status"] == 200:
                break
            time.sleep(5)
        else:
            logs = cls.compose("logs", "--tail", "40", "paperless", check=False)
            raise AssertionError(f"Paperless did not come up: {last['status']}\n"
                                 f"{logs.stdout[-3000:]}")
        # The web server answers once the migrations ran; this confirms none is left.
        cls.compose("exec", "-T", "--user", "paperless", "paperless",
                    "python3", "manage.py", "migrate", "--check")

    @classmethod
    def running_image(cls) -> str:
        cid = cls.compose("ps", "-q", "paperless").stdout.strip()
        image = subprocess.run(["docker", "inspect", cid, "--format", "{{.Image}}"],
                               capture_output=True, text=True, check=True).stdout.strip()
        digests = subprocess.run(["docker", "image", "inspect", image, "--format",
                                  "{{json .RepoDigests}}"],
                                 capture_output=True, text=True, check=True).stdout
        found = [d.split("@", 1)[1] for d in json.loads(digests)
                 if d.startswith("ghcr.io/paperless-ngx/paperless-ngx@")]
        return found[0] if found else image

    # --- API ------------------------------------------------------------------------------

    @classmethod
    def call(cls, jobs: list[dict], service: str = "probe-proxy") -> list[dict]:
        result = cls.compose("exec", "-T", service, "python", "-c", CLIENT,
                             stdin=json.dumps(jobs))
        return json.loads(result.stdout.strip().splitlines()[-1])

    @classmethod
    def api(cls, method: str, path: str, *, user: str = ADMIN, body: dict | None = None,
            expect: tuple[int, ...] = (200, 201)) -> object:
        job = {"method": method, "path": path, "json": body,
               "headers": {"Authorization": f"Token {cls.tokens[user]}"}}
        got = cls.call([job])[0]
        if got["status"] not in expect:
            # The path holds ids only; the body is not repeated, it may hold content.
            raise ApiError(f"{method} {path.split('?')[0]} as {user}: {got['status']}")
        return got["json"]

    @classmethod
    def results(cls, path: str, user: str = ADMIN) -> list[dict]:
        sep = "&" if "?" in path else "?"
        page = cls.api("GET", f"{path}{sep}page_size=100000", user=user)
        if page["next"]:
            raise ApiError(f"{path.split('?')[0]}: more results than one page")
        return page["results"]

    @classmethod
    def server_version(cls) -> str | None:
        got = cls.call([{"path": "/api/documents/?page_size=1",
                         "headers": basic(ADMIN, cls.passwords[ADMIN])}])[0]
        return got["version"]

    @classmethod
    def login(cls, user: str) -> str:
        got = cls.call([{"method": "POST", "path": "/api/token/",
                         "json": {"username": user, "password": cls.passwords[user]}}])[0]
        if got["status"] != 200:
            raise ApiError(f"token for {user}: {got['status']}")
        return got["json"]["token"]

    @classmethod
    def consume(cls, filename: str, data: bytes, title: str) -> int:
        body, content_type = pu.multipart({"title": title}, filename, data)
        got = cls.call([{"method": "POST", "path": "/api/documents/post_document/",
                         "body": base64.b64encode(body).decode(), "content_type": content_type,
                         "headers": {"Authorization": f"Token {cls.tokens[ADMIN]}"}}])[0]
        if got["status"] != 200 or not isinstance(got["json"], str):
            raise ApiError(f"upload {filename}: {got['status']}")
        return cls.wait_task(got["json"], filename)

    @classmethod
    def wait_task(cls, task: str, filename: str) -> int:
        deadline = time.monotonic() + TASK_TIMEOUT
        while time.monotonic() < deadline:
            tasks = cls.api("GET", f"/api/tasks/?task_id={task}")
            # A list up to Paperless-ngx 2.x, a page from 3.x on.
            tasks = tasks["results"] if isinstance(tasks, dict) else tasks
            status = str(tasks[0]["status"]).lower() if tasks else ""
            if status == "success":
                ids = tasks[0].get("related_document_ids") or [tasks[0].get("related_document")]
                return int(ids[0])
            if status in ("failure", "revoked"):
                raise ApiError(f"consumption of {filename} failed")
            time.sleep(3)
        raise ApiError(f"consumption of {filename} did not finish")

    @classmethod
    def search(cls, word: str, user: str = ADMIN) -> list[int]:
        return sorted(d["id"] for d in cls.results(f"/api/documents/?query={word}&fields=id",
                                                   user))

    # --- phase 1 --------------------------------------------------------------------------

    @classmethod
    def seed(cls) -> None:
        cls.tokens = {ADMIN: cls.login(ADMIN)}
        # A PDF with a text layer gets an archive file only on request; the setting is
        # stored in the database, so it is carried through the upgrade as well.
        config = cls.api("GET", "/api/config/")
        cls.api("PATCH", f"/api/config/{config[0]['id']}/",
                body={"archive_file_generation": "always"})
        view = ["view_document", "view_tag", "view_note"]
        group = cls.api("POST", "/api/groups/", body={"name": "upgrade-group",
                                                      "permissions": view})
        ids = {"group": group["id"]}
        for user, groups, perms in ((USER_A, [group["id"]], []), (USER_B, [], view)):
            got = cls.api("POST", "/api/users/", body={
                "username": user, "password": cls.passwords[user], "is_active": True,
                "groups": groups, "user_permissions": perms})
            ids[user] = got["id"]
            cls.tokens[user] = cls.login(user)
        ids[ADMIN] = next(u["id"] for u in cls.results("/api/users/")
                          if u["username"] == ADMIN)
        parent = None
        for level in ("root", "middle", "leaf"):
            got = cls.api("POST", "/api/tags/", body={"name": f"upgrade-{level}",
                                                      "parent": parent})
            ids[level] = parent = got["id"]
        ids["flat"] = cls.api("POST", "/api/tags/", body={"name": "upgrade-flat"})["id"]
        ids["correspondent"] = cls.api("POST", "/api/correspondents/",
                                       body={"name": "upgrade-correspondent"})["id"]
        ids["document_type"] = cls.api("POST", "/api/document_types/",
                                       body={"name": "upgrade-type"})["id"]
        ids["field"] = cls.api("POST", "/api/custom_fields/",
                               body={"name": "upgrade-field", "data_type": "string"})["id"]
        ids["storage_path"] = cls.api("POST", "/api/storage_paths/", body={
            "name": "upgrade-path", "path": "upgrade/{{ title }}"})["id"]
        cls.api("POST", "/api/saved_views/", body={
            "name": "upgrade-view", "show_on_dashboard": False, "show_in_sidebar": False,
            "sort_field": "created", "sort_reverse": True,
            "filter_rules": [{"rule_type": 6, "value": str(ids["leaf"])}]})
        a, b, g = ids[USER_A], ids[USER_B], ids["group"]
        # (kind, words, owner, view users, view groups, change users, tags)
        plan = [
            ("pdf", ["s1", "s3"], a, [], [], [], ["leaf"]),
            ("pdf", ["s2"], b, [], [g], [], ["middle", "flat"]),
            ("pdf", ["s2"], ids[ADMIN], [b], [], [], ["root"]),
            ("txt", ["s1"], a, [b], [], [b], ["leaf", "flat"]),
            ("txt", ["s2"], None, [], [], [], []),
            ("txt", ["s3"], b, [], [], [], ["middle"]),
        ]
        docs = []
        searches: dict[str, list[int]] = {key: [] for key in WORDS}
        for number, (kind, words, owner, view_users, view_groups, change_users, tags) in \
                enumerate(plan, start=1):
            lines = [f"Upgrade smoke document {number} {secrets.token_hex(8)}",
                     *(f"Keyword {WORDS[key]} line {number}" for key in words)]
            data = (pu.text_pdf(lines) if kind == "pdf"
                    else ("\n".join(lines) + "\n").encode("ascii"))
            doc = cls.consume(f"upgrade-{number}.{kind}", data, f"upgrade document {number}")
            cls.api("PATCH", f"/api/documents/{doc}/", body={
                "owner": owner, "tags": [ids[t] for t in tags],
                "correspondent": ids["correspondent"] if number % 2 else None,
                "document_type": ids["document_type"] if number < 4 else None,
                "storage_path": ids["storage_path"] if number == 1 else None,
                "custom_fields": [{"field": ids["field"], "value": f"value {number}"}],
                "set_permissions": {"view": {"users": view_users, "groups": view_groups},
                                    "change": {"users": change_users, "groups": []}},
            })
            cls.api("POST", f"/api/documents/{doc}/notes/", body={"note": f"note {number}"})
            docs.append((doc, owner, view_users, view_groups))
            for key in words:
                searches[key].append(doc)
        cls.ids = ids
        everyone = sorted(d[0] for d in docs)
        cls.expected_visible = {
            ADMIN: everyone,
            USER_A: sorted(d for d, owner, vu, vg in docs
                           if owner in (a, None) or a in vu or g in vg),
            USER_B: sorted(d for d, owner, vu, vg in docs if owner in (b, None) or b in vu),
        }
        cls.expected_searches = {key: sorted(v) for key, v in searches.items()}
        # Indexing runs after the consumption task; wait until each search finds its set.
        deadline = time.monotonic() + TASK_TIMEOUT
        while time.monotonic() < deadline:
            if all(cls.search(WORDS[k]) == v for k, v in cls.expected_searches.items()):
                break
            time.sleep(3)

    # --- snapshot -------------------------------------------------------------------------

    @classmethod
    def snapshot(cls, version: str | None) -> dict:
        documents = []
        for doc in cls.results("/api/documents/?full_perms=true&ordering=id"):
            downloads = [{"path": f"/api/documents/{doc['id']}/download/?original=true",
                          "hash": True, "headers": {"Authorization": f"Token {cls.tokens[ADMIN]}"}}]
            if doc.get("archived_file_name"):
                downloads.append({"path": f"/api/documents/{doc['id']}/download/",
                                  "hash": True,
                                  "headers": {"Authorization": f"Token {cls.tokens[ADMIN]}"}})
            # A file that cannot be read is recorded without a checksum (null), so the
            # comparison names the document instead of the run ending here.
            files = cls.call(downloads)
            notes = cls.api("GET", f"/api/documents/{doc['id']}/notes/")
            perms = doc["permissions"]
            documents.append({
                "id": doc["id"], "title": doc["title"], "original": files[0]["sha256"],
                "archive": files[1]["sha256"] if len(files) > 1 else None,
                "tags": doc["tags"], "owner": doc["owner"],
                "correspondent": doc["correspondent"], "document_type": doc["document_type"],
                "storage_path": doc["storage_path"],
                "custom_fields": [[f["field"], f["value"]] for f in doc["custom_fields"]],
                "notes": [n["note"] for n in (notes["results"] if isinstance(notes, dict)
                                              else notes)],
                "view_users": perms["view"]["users"], "view_groups": perms["view"]["groups"],
                "change_users": perms["change"]["users"],
                "change_groups": perms["change"]["groups"],
            })
        tags = [{"id": t["id"], "name": t["name"], "parent": t.get("parent")}
                for t in cls.results("/api/tags/")]
        objects = {kind: [{"id": o["id"], "name": o["name"]}
                          for o in cls.results(f"/api/{kind}/")]
                   for kind in ("correspondents", "document_types", "custom_fields",
                                "storage_paths", "saved_views")}
        users = [{"id": u["id"], "username": u["username"], "groups": u["groups"],
                  "is_superuser": u["is_superuser"], "permissions": u["user_permissions"]}
                 for u in cls.results("/api/users/")
                 if u["username"] in (ADMIN, USER_A, USER_B)]
        groups = [{"id": g["id"], "name": g["name"], "permissions": g["permissions"]}
                  for g in cls.results("/api/groups/")]
        searches = {key: cls.search(word) for key, word in WORDS.items()}
        visible = {user: sorted(d["id"] for d in cls.results("/api/documents/?fields=id", user))
                   for user in (ADMIN, USER_A, USER_B)}
        return pu.parse_snapshot({
            "format": pu.SNAPSHOT_FORMAT, "version": version or "0.0.0",
            "documents": documents, "tags": tags, "objects": objects, "users": users,
            "groups": groups, "searches": searches, "visible": visible,
        })

    # --- protocol -------------------------------------------------------------------------

    @classmethod
    def write_protocol(cls) -> None:
        if not PROTOCOL:
            return
        commit = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=False).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain",
                                "--untracked-files=no"],
                               capture_output=True, text=True, check=False).stdout.strip()
        before = getattr(cls, "before", None)
        after = getattr(cls, "after", None)
        protocol = {
            "format": "papaia.paperless-upgrade-protocol/1",
            "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "commit": commit, "worktree_clean": not dirty,
            "project": PROJECT, "subnet": SUBNET,
            "from": {"version": cls.versions.get("before"), "image": FROM_IMAGE,
                     "running_digest": cls.images.get("before")},
            "to": {"version": cls.versions.get("after"), "running_digest":
                   cls.images.get("after")},
            "migrations_applied": cls.migrations,
            "counts_before": pu.counts(before) if before else None,
            "counts_after": pu.counts(after) if after else None,
            "visible_counts_before": {u: len(v) for u, v in before["visible"].items()}
            if before else None,
            "checks": dict(sorted(cls.checks.items())),
        }
        Path(PROTOCOL).write_text(json.dumps(protocol, indent=2) + "\n", encoding="utf-8")

    def record(self, name: str, findings: list[str]) -> None:
        type(self).checks[name] = "pass" if not findings else f"fail ({len(findings)})"
        self.assertEqual(findings, [])

    # --- phase 1 sanity: the data the comparison rests on ------------------------------

    def test_a_phase_one_data_has_teeth(self) -> None:
        findings = []
        if self.before["visible"] != self.expected_visible:
            findings.append("visible documents before the upgrade differ from the plan")
        if self.before["searches"] != self.expected_searches:
            findings.append("searches before the upgrade differ from the plan")
        if any(d["original"] is None for d in self.before["documents"]):
            findings.append("an original file could not be read before the upgrade")
        counts = pu.counts(self.before)
        if counts["documents"] < 5 or counts["archive_files"] < 3 or counts["tag_children"] < 2:
            findings.append(f"too little data: {counts}")
        self.record("phase_one_data_as_planned", findings)

    # --- phase 2 ------------------------------------------------------------------------

    def test_b_versions_and_images(self) -> None:
        findings = []
        if self.versions.get("before") != FROM_VERSION:
            findings.append(f"version before: {self.versions.get('before')}")
        if self.versions.get("after") != TO_VERSION:
            findings.append(f"version after: {self.versions.get('after')}")
        if self.images.get("before") != FROM_IMAGE.split("@", 1)[1]:
            findings.append("image before is not the 3.4.2 digest")
        compose = (ADDON / "docker-compose.yml").read_text(encoding="utf-8")
        if f"@{self.images.get('after')}" not in compose:
            findings.append("image after is not the add-on default")
        self.record("versions_and_digests", findings)

    def test_c_migrations_were_applied(self) -> None:
        self.record("migrations_applied",
                    [] if self.migrations else ["no migration was applied at the upgrade"])

    # --- phase 3 ------------------------------------------------------------------------

    def findings(self, prefixes: tuple[str, ...]) -> list[str]:
        return [f for f in pu.compare(self.before, self.after) if f.startswith(prefixes)]

    def test_d_documents_and_files_are_unchanged(self) -> None:
        self.record("documents_files_owners_permissions_notes",
                    self.findings(("document ",)))

    def test_e_tag_tree_is_unchanged(self) -> None:
        self.record("tag_tree", self.findings(("tag ",)))

    def test_f_searches_return_the_same_documents(self) -> None:
        self.record("searches", self.findings(("search ",)))

    def test_g_users_groups_and_visibility_are_unchanged(self) -> None:
        self.record("users_groups_visibility", self.findings(("user ", "group ")))

    def test_h_other_objects_are_unchanged(self) -> None:
        self.record("correspondent_type_field_path_view", self.findings(
            ("correspondent ", "document_type ", "custom_field ", "storage_path ",
             "saved_view ")))

    def test_i_gate_admits_the_listed_person_and_refuses_a_stranger(self) -> None:
        mcp = "probe-mcp"
        got = self.call([
            {"path": "/api/documents/?page_size=100&fields=id",
             "headers": {"X-Papaia-Remote-User": USER_A}},
            {"path": "/api/documents/", "headers": {"X-Papaia-Remote-User": STRANGER}},
            {"path": "/api/documents/", "headers": {"X-Papaia-Remote-User": USER_A},
             "gate": False},
        ], service=mcp)
        findings = []
        listed = sorted(d["id"] for d in (got[0]["json"] or {}).get("results", []))
        if got[0]["status"] != 200:
            findings.append(f"user {USER_A}: through the gate {got[0]['status']}")
        elif listed != self.after["visible"][USER_A]:
            findings.append(f"user {USER_A}: documents through the gate differ")
        error = (got[1]["json"] or {}).get("error") if isinstance(got[1]["json"], dict) else None
        if got[1]["status"] != 403 or error != "paperless_unknown_user":
            findings.append(f"stranger: {got[1]['status']} {error}")
        if got[2]["status"] != 403:
            findings.append(f"no secret: {got[2]['status']}")
        self.record("gate_listed_and_stranger", findings)

    def test_z_new_document_after_upgrade_is_found(self) -> None:
        word = "glimmerfold" + secrets.token_hex(3)
        findings = []
        try:
            doc = self.consume("upgrade-new.pdf",
                               pu.text_pdf([f"After the upgrade {word}"]), "upgrade new")
            deadline = time.monotonic() + TASK_TIMEOUT
            found: list[int] = []
            while time.monotonic() < deadline:
                found = self.search(word)
                if found:
                    break
                time.sleep(3)
            if found != [doc]:
                findings.append(f"document {doc}: search after the upgrade gives {found}")
        except ApiError as error:
            findings.append(str(error))
        self.record("new_document_consumed_and_found", findings)


if __name__ == "__main__":
    unittest.main()
