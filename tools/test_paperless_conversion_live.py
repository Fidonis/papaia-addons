"""Live test of the document converters of the paperless add-on (Gotenberg and Tika).

Runs only when both variables are set (and Docker is available):

    PAPERLESS_CONVERSION_LIVE_PROJECT  Compose project name, for example `itk-conversion`
    PAPERLESS_CONVERSION_LIVE_SUBNET   a free IPv4 /24, for example `10.210.50.0/24`

    PAPERLESS_CONVERSION_LIVE_PROJECT=itk-conversion \\
      PAPERLESS_CONVERSION_LIVE_SUBNET=10.210.50.0/24 \\
      python -m unittest tools/test_paperless_conversion_live.py

Optional: `PAPERLESS_CONVERSION_LIVE_PUBLIC_SUBNET` (default `198.51.100.208/28`), a
range outside the loopback, RFC 1918, link-local and unique-local ranges. It stands in
for a public address: Gotenberg counts every other address as public. The metadata
probe always listens on `169.254.169.254` (network `169.254.169.248/29`).

The stack is `paperless/docker-compose.yml` plus an override written at run time: no
`.env`, Paperless with Tika and Gotenberg as the add-on ships them, the gate and the MCP
server left out. The /24 is split: the add-on network gets the first /26, the Gotenberg
and the Tika network a /27 each. Test containers:

- three probe servers that log every connection: one in the add-on network, one on the
  public stand-in address and one on `169.254.169.254`;
- `flags`: a second Gotenberg with the image, start flags, user and limits of
  `paperless-gotenberg`, but placed in the add-on network and both probe networks, so
  the start flags alone have to stop every request (the network would hide a missing
  flag); a control request from inside it shows that each probe is reachable;
- `tika-reach`: the same for Tika, which has no such flags;
- `client`: sends the documents to `flags` and `tika-reach`.

Paperless-ngx 3.5.1 takes no stand-alone `.html` from the consume directory; HTML
reaches the converters through Paperless as the body of a mail (the `.eml` case).

Fixtures are synthetic and written by the test: HTML, `.docx`, `.odt` and `.eml` that
reference each probe (image, style sheet, frame, linked object, remote attachment) and
an `.xlsx`, each with its own marker word. They are sent to the Chromium and LibreOffice
routes and to Tika, both on the reachable copies and on the shipped services (from
inside Paperless, the only container that reaches them), and consumed through
Paperless: no probe may see a request. `webhookUrl` and `downloadFrom` naming a probe
are refused. The shipped converters reach neither the database, Valkey, the other
converter, a probe nor an outside address. All secrets are generated per run; no port
is published; the project is removed with `down -v` at the end, also on failure. The
run prints a CI-L record: commit, image digests, cases, probe hit counts, measured peak
memory and process counts of the converters; never document content.
"""

from __future__ import annotations

import base64
import email.message
import io
import ipaddress
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON = REPO_ROOT / "paperless"
PROJECT = os.environ.get("PAPERLESS_CONVERSION_LIVE_PROJECT", "")
SUBNET = os.environ.get("PAPERLESS_CONVERSION_LIVE_SUBNET", "")
PUBLIC_SUBNET = os.environ.get("PAPERLESS_CONVERSION_LIVE_PUBLIC_SUBNET", "198.51.100.208/28")
METADATA_SUBNET = "169.254.169.248/29"
METADATA_IP = "169.254.169.254"
PROBE_IMAGE = "docker.io/library/python:3.12-alpine"
ADMIN = "conversion-admin"
CONSUME_TIMEOUT = 420
ADDON_PORT, PUBLIC_PORT, METADATA_PORT = 52660, 52661, 80
OUTSIDE = "1.1.1.1:443"

# Gotenberg's own classes of non-public addresses (8.x documentation of
# `--*-deny-private-ips`): the public stand-in must lie outside all of them.
NON_PUBLIC = [ipaddress.ip_network(n) for n in
              ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")]


def word(prefix: str) -> str:
    """A marker the full-text search keeps as one token: letters only."""
    return prefix + "".join(secrets.choice(string.ascii_lowercase) for _ in range(10))


MARKERS = {name: word(f"zq{name}") for name in ("docx", "xlsx", "odt", "eml", "html")}

# A TCP server that logs one line per connection (`HIT <first request line>`) and
# answers 200 with an empty style sheet: a fetch that happens is seen, whatever it asks.
PROBE_SERVER = r"""
import socketserver, sys
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(2048).decode("latin-1").strip()
        except OSError:
            line = ""
        print("HIT", line, flush=True)
        try:
            self.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Type: text/css\r\nContent-Length: 2\r\n\r\n{}")
        except OSError:
            pass
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
Server(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
"""

# Runs inside the Paperless container against its own loopback port (the gate is not
# started), as `localhost`: from 3.5 on Paperless accepts no other loopback name.
# Credentials come from the container's environment, never from argv.
API = r"""
import base64, json, os, sys, urllib.error, urllib.request
path, mode = sys.argv[1], sys.argv[2]
user, password = os.environ["PAPERLESS_ADMIN_USER"], os.environ["PAPERLESS_ADMIN_PASSWORD"]
auth = base64.b64encode(f"{user}:{password}".encode()).decode()
request = urllib.request.Request("http://127.0.0.1:8000" + path,
                                 headers={"Authorization": "Basic " + auth, "Accept": "application/json",
                                          "Host": "localhost"})
out = {"status": None}
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        out["status"], body, ctype = response.status, response.read(), response.headers.get("Content-Type", "")
except urllib.error.HTTPError as error:
    out["status"], body, ctype = error.code, b"", ""
except OSError:
    out["status"], body, ctype = "unreachable", b"", ""
if mode == "json" and out["status"] == 200:
    out["body"] = json.loads(body)
elif mode == "file":
    out["type"], out["head"], out["size"] = ctype, body[:5].decode("latin-1"), len(body)
print(json.dumps(out))
"""

# Sends one request described by a JSON object on stdin and prints one JSON line with
# the status and the body (base64). `files` (name -> base64) and `fields` make a
# multipart form; `put` sends one body as is (Tika).
SEND = r"""
import base64, json, sys, urllib.error, urllib.request, uuid
spec = json.load(sys.stdin)
headers = dict(spec.get("headers") or {})
if "put" in spec:
    body, method = base64.b64decode(spec["put"]), "PUT"
else:
    boundary, parts, method = uuid.uuid4().hex, [], "POST"
    for name, value in (spec.get("fields") or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f'{value}\r\n'.encode())
    for name, data in (spec.get("files") or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + base64.b64decode(data) + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
request = urllib.request.Request(spec["url"], data=body, method=method, headers=headers)
try:
    with urllib.request.urlopen(request, timeout=150) as response:
        status, data = response.status, response.read()
except urllib.error.HTTPError as error:
    status, data = error.code, error.read()
except OSError as error:
    status, data = "unreachable", str(error).encode()
print(json.dumps({"status": status, "body": base64.b64encode(data).decode()}))
"""

# Tries a TCP connection to each host:port argument; prints `OPEN` or `CLOSED` per target.
CONNECT = r"""
for target in "$@"; do
  host=${target%:*}; port=${target##*:}
  if timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$host" "$port" 2>/dev/null; then
    echo "OPEN $target"
  else
    echo "CLOSED $target"
  fi
done
"""


# --- fixtures (synthetic content, written by the test) -----------------------------


def _zip(members: list[tuple[str, str]], stored_first: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, (name, text) in enumerate(members):
            kind = zipfile.ZIP_STORED if stored_first and index == 0 else zipfile.ZIP_DEFLATED
            archive.writestr(zipfile.ZipInfo(name), text, compress_type=kind)
    return buffer.getvalue()


def html(marker: str, urls: list[str]) -> bytes:
    refs = "".join(
        f"<link rel='stylesheet' href='{u}.css'><img src='{u}.png'><iframe src='{u}.html'></iframe>"
        f"<object data='{u}.svg'></object>"
        for u in urls
    )
    head = "".join(f"<link rel='stylesheet' href='{u}-head.css'>" for u in urls)
    return (f"<html><head><meta charset='utf-8'>{head}</head><body><h1>Circular</h1>"
            f"<p>Notice {marker} for the archive.</p>{refs}</body></html>").encode()


def docx(marker: str, urls: list[str]) -> bytes:
    rels = "".join(
        f'<Relationship Id="rImg{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/image" Target="{u}.png" TargetMode="External"/>'
        for i, u in enumerate(urls)
    )
    pictures = "".join(
        '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="952500"/><wp:docPr id="'
        f'{i + 1}" name="Picture {i + 1}"/><a:graphic><a:graphicData '
        'uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr>'
        f'<pic:cNvPr id="{i + 1}" name="remote{i}.png"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill>'
        f'<a:blip r:link="rImg{i}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr>'
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="952500" cy="952500"/></a:xfrm>'
        '<a:prstGeom prst="rect"/></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline>'
        '</w:drawing></w:r></w:p>'
        f'<w:p><w:fldSimple w:instr=\' INCLUDEPICTURE "{u}-field.png" \\d \'><w:r><w:t>picture</w:t>'
        '</w:r></w:fldSimple></w:p>'
        for i, u in enumerate(urls)
    )
    return _zip([
        ("[Content_Types].xml",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/'
          'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')),
        ("_rels/.rels",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
          'relationships/officeDocument" Target="word/document.xml"/></Relationships>')),
        ("word/_rels/document.xml.rels",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}'
          '</Relationships>')),
        ("word/document.xml",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
          f'<w:body><w:p><w:r><w:t>Quarterly memo {marker} for the archive.</w:t></w:r></w:p>'
          f'{pictures}</w:body></w:document>')),
    ])


def xlsx(marker: str, urls: list[str]) -> bytes:
    del urls
    return _zip([
        ("[Content_Types].xml",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/'
          'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
          'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')),
        ("_rels/.rels",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
          'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')),
        ("xl/workbook.xml",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')),
        ("xl/_rels/workbook.xml.rels",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
          'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')),
        ("xl/worksheets/sheet1.xml",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
          f'<row r="1"><c r="A1" t="inlineStr"><is><t>Budget {marker}</t></is></c>'
          '<c r="B1"><v>42</v></c></row></sheetData></worksheet>')),
    ])


def odt(marker: str, urls: list[str]) -> bytes:
    linked = "".join(
        f'<text:p><draw:frame draw:name="image{i}" svg:width="2cm" svg:height="2cm" '
        f'text:anchor-type="as-char"><draw:image xlink:href="{u}.png" xlink:type="simple" '
        'xlink:show="embed" xlink:actuate="onLoad"/></draw:frame></text:p>'
        f'<text:p><draw:frame draw:name="object{i}" svg:width="2cm" svg:height="2cm" '
        f'text:anchor-type="as-char"><draw:object xlink:href="{u}-object" xlink:type="simple" '
        'xlink:show="embed" xlink:actuate="onLoad"/></draw:frame></text:p>'
        f'<text:section text:name="linked{i}"><text:section-source xlink:href="{u}-section.odt" '
        'xlink:type="simple"/></text:section>'
        for i, u in enumerate(urls)
    )
    return _zip([
        ("mimetype", "application/vnd.oasis.opendocument.text"),
        ("META-INF/manifest.xml",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
          'manifest:version="1.2">'
          '<manifest:file-entry manifest:full-path="/" '
          'manifest:media-type="application/vnd.oasis.opendocument.text"/>'
          '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
          '</manifest:manifest>')),
        ("content.xml",
         ('<?xml version="1.0" encoding="UTF-8"?>'
          '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
          'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
          'xmlns:xlink="http://www.w3.org/1999/xlink" office:version="1.2">'
          f'<office:body><office:text><text:p>Meeting notes {marker} of the board.</text:p>{linked}'
          '</office:text></office:body></office:document-content>')),
    ], stored_first=True)


def eml(marker: str, urls: list[str]) -> bytes:
    message = email.message.EmailMessage()
    message["From"] = "Sender Example <sender@example.org>"
    message["To"] = "Archive Example <archive@example.org>"
    message["Subject"] = "Delivery note"
    message["Date"] = "Tue, 24 Nov 2026 10:00:00 +0000"
    message["Message-ID"] = f"<{secrets.token_hex(8)}@example.org>"
    message.set_content("Please find the delivery note attached.")
    message.add_alternative(html(marker, urls).decode(), subtype="html")
    message.add_attachment(
        b"Item list: three boxes of paper.\n", maintype="text", subtype="plain", filename="items.txt"
    )
    message.add_attachment(html("attachment", urls), maintype="text", subtype="html", filename="page.html")
    # A remote attachment: the body lives at a URL (RFC 2046 message/external-body).
    for index, url in enumerate(urls):
        remote = email.message.EmailMessage()
        remote["Content-Type"] = "application/pdf"
        remote.set_payload("")
        message.attach(remote)
        remote.replace_header(
            "Content-Type", f'message/external-body; access-type=URL; URL="{url}-remote{index}.pdf"'
        )
    return message.as_bytes()


FIXTURES = {
    "docx": ("memo.docx", docx),
    "xlsx": ("budget.xlsx", xlsx),
    "odt": ("notes.odt", odt),
    "eml": ("delivery.eml", eml),
    "html": ("circular.html", html),
}


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@unittest.skipUnless(
    PROJECT and SUBNET and docker_available(),
    "set PAPERLESS_CONVERSION_LIVE_PROJECT and PAPERLESS_CONVERSION_LIVE_SUBNET (needs Docker)",
)
class LiveConversion(unittest.TestCase):
    tmp: Path
    env: dict[str, str]
    files: list[str]
    ips: dict[str, str]
    record: dict[str, object]

    # --- stack ------------------------------------------------------------------------

    @classmethod
    def setUpClass(cls) -> None:
        net = ipaddress.ip_network(SUBNET)
        if net.prefixlen != 24 or net.version != 4:
            raise AssertionError(f"PAPERLESS_CONVERSION_LIVE_SUBNET must be an IPv4 /24: {SUBNET}")
        public = ipaddress.ip_network(PUBLIC_SUBNET)
        if any(public.overlaps(n) for n in NON_PUBLIC) or public.num_addresses < 8:
            raise AssertionError(f"public stand-in {PUBLIC_SUBNET} must be a /29 or larger outside "
                                 "the non-public ranges")
        addon = next(iter(net.subnets(new_prefix=26)))
        gotenberg_net, tika_net = list(net.subnets(new_prefix=27))[2:4]
        cls.ips = {
            # The last host address of each range, and the probes start first: Docker
            # hands out the low addresses to the containers without a fixed one.
            "addon": str(addon.broadcast_address - 1),
            "public": str(public.broadcast_address - 1),
            "metadata": METADATA_IP,
        }
        cls.record = {"cases": {}}
        cls.tmp = Path(tempfile.mkdtemp(prefix="paperless-conversion-"))
        config = cls.tmp / "config"
        (config / "certs").mkdir(parents=True)
        for name in ("media", "export", "consume"):
            (cls.tmp / name).mkdir()
        (cls.tmp / "probe.py").write_text(PROBE_SERVER, encoding="utf-8")

        shipped = yaml.safe_load((ADDON / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
        copied = ("image", "command", "user", "read_only", "tmpfs", "cap_drop", "security_opt",
                  "pids_limit", "mem_limit")
        reach = ["papaia-paperless-net", "probe-public-net", "probe-metadata-net"]
        flags = {k: v for k, v in shipped["paperless-gotenberg"].items() if k in copied}
        tika_reach = {k: v for k, v in shipped["paperless-tika"].items() if k in copied}
        probe = ["python", "-u", "/probe/probe.py"]
        mount = [f"{cls.tmp / 'probe.py'}:/probe/probe.py:ro"]
        dump = json.dumps
        override = cls.tmp / "override.yml"
        override.write_text(
            f"""\
services:
  paperless:
    env_file: !reset []
    depends_on: !override [paperless-db, paperless-broker, paperless-gotenberg, paperless-tika]
    environment:
      PAPERLESS_SECRET_KEY: "{secrets.token_urlsafe(48)}"
      PAPERLESS_DBPASS: "${{PAPERLESS_DBPASS}}"
      PAPERLESS_CONSUMER_POLLING: "2"
      REQUESTS_CA_BUNDLE: ""
  paperless-db:
    environment:
      POSTGRES_PASSWORD: "${{PAPERLESS_DBPASS}}"
  probe-addon:
    image: {PROBE_IMAGE}
    command: {dump(probe + [str(ADDON_PORT)])}
    volumes: {dump(mount)}
    networks:
      papaia-paperless-net: {{ipv4_address: "{cls.ips['addon']}"}}
  probe-public:
    image: {PROBE_IMAGE}
    command: {dump(probe + [str(PUBLIC_PORT)])}
    volumes: {dump(mount)}
    networks:
      probe-public-net: {{ipv4_address: "{cls.ips['public']}"}}
  probe-metadata:
    image: {PROBE_IMAGE}
    command: {dump(probe + [str(METADATA_PORT)])}
    volumes: {dump(mount)}
    networks:
      probe-metadata-net: {{ipv4_address: "{METADATA_IP}"}}
  client:
    image: {PROBE_IMAGE}
    command: ["sleep", "infinity"]
    networks: {dump(reach)}
  flags: {dump({**flags, "networks": reach})}
  tika-reach: {dump({**tika_reach, "networks": reach})}
networks:
  papaia-paperless-net:
    ipam:
      config: [{{subnet: "{addon}"}}]
  paperless-gotenberg-net:
    ipam:
      config: [{{subnet: "{gotenberg_net}"}}]
  paperless-tika-net:
    ipam:
      config: [{{subnet: "{tika_net}"}}]
  probe-public-net:
    driver: bridge
    name: {PROJECT}-probe-public
    ipam:
      config: [{{subnet: "{public}"}}]
  probe-metadata-net:
    driver: bridge
    name: {PROJECT}-probe-metadata
    ipam:
      config: [{{subnet: "{METADATA_SUBNET}"}}]
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
        # Registered before `up`: class cleanups run even when setUpClass fails.
        cls.addClassCleanup(cls.remove_stack)
        cls.compose("up", "-d", "--wait", "--wait-timeout", "120", "probe-addon", "probe-public",
                    "probe-metadata")
        cls.compose("up", "-d", "--wait", "--wait-timeout", "300", "paperless-gotenberg", "paperless-tika",
                    "paperless-db", "paperless-broker", "client", "flags", "tika-reach")
        cls.compose("up", "-d", "paperless")
        cls.wait_for_paperless()
        for key, (name, build) in FIXTURES.items():
            part = cls.tmp / "consume" / f".{name}.part"
            part.write_bytes(build(MARKERS[key], cls.urls(f"consume-{key}")))
            part.replace(cls.tmp / "consume" / name)

    @classmethod
    def remove_stack(cls) -> None:
        try:
            cls.print_record()
        finally:
            cls.compose("down", "-v", "--remove-orphans", check=False)
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def print_record(cls) -> None:
        commit = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=False).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"], capture_output=True,
                               text=True, check=False).stdout.strip()
        digests = {}
        for service in ("paperless", "paperless-gotenberg", "paperless-tika", "flags", "tika-reach"):
            cid = cls.compose("ps", "-q", service, check=False).stdout.strip()
            if cid:
                digests[service] = subprocess.run(
                    ["docker", "inspect", "-f", "{{.Config.Image}}", cid], capture_output=True, text=True,
                    check=False).stdout.strip()
        peaks = {}
        for service in ("paperless-gotenberg", "paperless-tika", "flags", "tika-reach"):
            got = cls.compose("exec", "-T", service, "cat", "/sys/fs/cgroup/memory.peak",
                              "/sys/fs/cgroup/pids.peak", check=False).stdout.split()
            if len(got) == 2:
                peaks[service] = {"memory_peak_mib": round(int(got[0]) / 1024**2), "pids_peak": int(got[1])}
        hits = {name: len(cls.hits(name)) for name in ("addon", "public", "metadata")}
        print("\nCI-L record (tools/test_paperless_conversion_live.py)\n" + json.dumps({
            "commit": commit + (" (dirty)" if dirty else ""),
            "images": digests,
            "probe_hits_total_including_controls": hits,
            "converter_peaks": peaks,
            "cases": cls.record["cases"],
        }, indent=2), file=sys.stderr)

    @classmethod
    def compose(cls, *args: str, check: bool = True, stdin: bytes | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["docker", "compose", "-p", PROJECT, "--project-directory", str(ADDON), *cls.files, *args],
            env=cls.env, input=stdin, capture_output=True, timeout=600, check=False,
        )
        result.stdout = result.stdout.decode("utf-8", "replace")
        result.stderr = result.stderr.decode("utf-8", "replace")
        if check and result.returncode != 0:
            raise AssertionError(f"docker compose {args[0]} failed: {result.stderr[-2000:]}")
        return result

    @classmethod
    def urls(cls, case: str) -> list[str]:
        """One URL per probe (and the add-on probe by name too), tagged with the case."""
        return [
            f"http://{cls.ips['addon']}:{ADDON_PORT}/{case}-addon",
            f"http://probe-addon:{ADDON_PORT}/{case}-name",
            f"http://{cls.ips['public']}:{PUBLIC_PORT}/{case}-public",
            f"http://{METADATA_IP}/{case}-metadata/latest/meta-data",
        ]

    @classmethod
    def hits(cls, probe: str) -> list[str]:
        logs = cls.compose("logs", "--no-log-prefix", f"probe-{probe}", check=False).stdout
        return [line for line in logs.splitlines() if line.startswith("HIT")]

    def assert_no_probe_hit(self, case: str) -> None:
        time.sleep(3)
        seen = {probe: [h for h in self.hits(probe) if f"/{case}-" in h]
                for probe in ("addon", "public", "metadata")}
        self.record["cases"][case] = {probe: len(lines) for probe, lines in seen.items()}
        self.assertEqual(seen, {"addon": [], "public": [], "metadata": []}, case)

    @classmethod
    def api(cls, path: str, mode: str = "json") -> dict:
        result = cls.compose("exec", "-T", "paperless", "python3", "-c", API, path, mode)
        return json.loads(result.stdout.strip().splitlines()[-1])

    @classmethod
    def wait_for_paperless(cls) -> None:
        deadline = time.monotonic() + 420
        last: dict = {}
        while time.monotonic() < deadline:
            result = cls.compose("exec", "-T", "paperless", "python3", "-c", API, "/api/documents/", "json",
                                 check=False)
            if result.returncode == 0:
                last = json.loads(result.stdout.strip().splitlines()[-1])
                if last["status"] == 200:
                    return
            time.sleep(5)
        logs = cls.compose("logs", "--tail", "40", "paperless", check=False)
        raise AssertionError(f"Paperless did not come up: {last}\n{logs.stdout[-3000:]}")

    def send(self, origin: str, spec: dict) -> tuple[object, bytes]:
        """Sends a request from `origin` (`client` or `paperless`)."""
        python = "python" if origin == "client" else "python3"
        result = self.compose("exec", "-T", origin, python, "-c", SEND, stdin=json.dumps(spec).encode())
        out = json.loads(result.stdout.strip().splitlines()[-1])
        return out["status"], base64.b64decode(out["body"])

    def connect(self, service: str, *targets: str) -> dict[str, str]:
        result = self.compose("exec", "-T", service, "bash", "-c", CONNECT, "connect", *targets)
        out = {}
        for line in result.stdout.splitlines():
            state, _, target = line.partition(" ")
            out[target] = state
        return out

    def pdf_text(self, pdf: bytes) -> str:
        self.assertTrue(pdf.startswith(b"%PDF"), pdf[:200])
        return self.compose("exec", "-T", "paperless", "pdftotext", "-", "-", stdin=pdf).stdout

    # --- start check --------------------------------------------------------------------

    def test_pinned_digest_starts_healthy_with_the_full_command(self) -> None:
        cid = self.compose("ps", "-q", "paperless-gotenberg").stdout.strip()
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}} {{.Config.Image}}", cid],
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(state[0], "healthy")
        self.assertIn("gotenberg/gotenberg:8.40.0@sha256:", state[1])

    def test_an_unknown_flag_makes_the_start_fail(self) -> None:
        command = yaml.safe_load((ADDON / "docker-compose.yml").read_text(encoding="utf-8"))[
            "services"]["paperless-gotenberg"]["command"]
        result = self.compose("run", "--rm", "--no-deps", "-T", "paperless-gotenberg", *command,
                              "--no-such-flag-in-gotenberg=true", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no-such-flag-in-gotenberg", result.stdout + result.stderr)

    # --- start flags alone: the reachable copy -------------------------------------------

    def test_control_every_probe_is_reachable_from_the_flag_copy(self) -> None:
        targets = {
            "addon": f"{self.ips['addon']}:{ADDON_PORT}",
            "public": f"{self.ips['public']}:{PUBLIC_PORT}",
            "metadata": f"{METADATA_IP}:{METADATA_PORT}",
        }
        for service in ("flags", "tika-reach"):
            with self.subTest(service=service):
                got = self.connect(service, *targets.values())
                self.assertEqual(set(got.values()), {"OPEN"}, got)
        time.sleep(1)
        for probe in targets:
            self.assertTrue(self.hits(probe), f"control: probe {probe} records connections")

    def flag_copy_case(self, case: str, route: str, name: str, build) -> None:
        data = build(word("zqflag"), self.urls(case))
        status, body = self.send("client", {"url": f"http://flags:3000{route}", "files": {name: b64(data)}})
        self.assertIn(status, (200, 400, 403, 422), body[:300])
        self.assert_no_probe_hit(case)

    def test_flag_copy_libreoffice_html_fetches_nothing(self) -> None:
        self.flag_copy_case("flags-lo-html", "/forms/libreoffice/convert", "page.html", html)

    def test_flag_copy_libreoffice_docx_fetches_nothing(self) -> None:
        self.flag_copy_case("flags-lo-docx", "/forms/libreoffice/convert", "memo.docx", docx)

    def test_flag_copy_libreoffice_odt_fetches_nothing(self) -> None:
        self.flag_copy_case("flags-lo-odt", "/forms/libreoffice/convert", "notes.odt", odt)

    def test_flag_copy_chromium_html_fetches_nothing(self) -> None:
        self.flag_copy_case("flags-chromium-html", "/forms/chromium/convert/html", "index.html", html)

    def test_flag_copy_chromium_mail_body_fetches_nothing(self) -> None:
        def body(marker: str, urls: list[str]) -> bytes:
            message = email.message_from_bytes(eml(marker, urls))
            return next(p for p in message.walk() if p.get_content_type() == "text/html").get_payload(
                decode=True)
        self.flag_copy_case("flags-chromium-eml", "/forms/chromium/convert/html", "index.html", body)

    def test_flag_copy_refuses_webhook(self) -> None:
        case = "flags-webhook"
        url = self.urls(case)
        status, body = self.send("client", {
            "url": "http://flags:3000/forms/chromium/convert/html",
            "files": {"index.html": b64(b"<html><body><p>webhook</p></body></html>")},
            "headers": {"Gotenberg-Webhook-Url": url[0] + "-ok", "Gotenberg-Webhook-Error-Url": url[2] + "-err",
                        "Gotenberg-Webhook-Events-Url": url[3] + "-events"},
        })
        # 204 would mean the job was taken for delivery to the webhook.
        self.assertNotEqual(status, 204, body[:300])
        self.record["cases"][case + "-status"] = status
        self.assert_no_probe_hit(case)

    def test_flag_copy_refuses_download_from(self) -> None:
        case = "flags-download-from"
        entries = [{"url": u + ".html"} for u in self.urls(case)]
        status, body = self.send("client", {
            "url": "http://flags:3000/forms/chromium/convert/html",
            "fields": {"downloadFrom": json.dumps(entries)},
        })
        self.assertEqual(status, 400, body[:300])
        self.record["cases"][case + "-status"] = status
        self.assert_no_probe_hit(case)

    def test_flag_copy_runs_no_script(self) -> None:
        static, scripted = word("static"), word("script")
        page = (f"<html><body><p>Text {static}</p><p id='x'>plain</p>"
                f"<script>document.getElementById('x').textContent = 'Text {scripted}';</script>"
                "</body></html>").encode()
        status, pdf = self.send("client", {"url": "http://flags:3000/forms/chromium/convert/html",
                                           "files": {"index.html": b64(page)}})
        self.assertEqual(status, 200, pdf[:300])
        text = self.pdf_text(pdf)
        self.assertIn(static, text, "control: the static text is extracted")
        self.assertNotIn(scripted, text)

    def test_tika_fetches_nothing(self) -> None:
        for key in ("html", "docx", "odt", "eml"):
            name, build = FIXTURES[key]
            case = f"tika-reach-{key}"
            with self.subTest(fixture=name):
                status, body = self.send("client", {"url": "http://tika-reach:9998/rmeta/text",
                                                    "put": b64(build(word("zqtika"), self.urls(case))),
                                                    "headers": {"Accept": "application/json"}})
                self.assertEqual(status, 200, body[:300])
                self.assert_no_probe_hit(case)

    # --- the shipped converters, from Paperless -----------------------------------------

    def test_shipped_gotenberg_and_tika_fetch_nothing(self) -> None:
        cases = [
            ("shipped-lo-html", "http://paperless-gotenberg:3000/forms/libreoffice/convert", "page.html", html),
            ("shipped-lo-docx", "http://paperless-gotenberg:3000/forms/libreoffice/convert", "memo.docx", docx),
            ("shipped-lo-odt", "http://paperless-gotenberg:3000/forms/libreoffice/convert", "notes.odt", odt),
            ("shipped-chromium-html", "http://paperless-gotenberg:3000/forms/chromium/convert/html", "index.html",
             html),
        ]
        for case, url, name, build in cases:
            with self.subTest(case=case):
                status, body = self.send("paperless", {"url": url,
                                                       "files": {name: b64(build(word("zq"), self.urls(case)))}})
                self.assertEqual(status, 200, body[:300])
                self.assert_no_probe_hit(case)
        for key in ("html", "docx", "odt", "eml"):
            name, build = FIXTURES[key]
            case = f"shipped-tika-{key}"
            with self.subTest(case=case):
                status, body = self.send("paperless", {"url": "http://paperless-tika:9998/rmeta/text",
                                                       "put": b64(build(word("zq"), self.urls(case))),
                                                       "headers": {"Accept": "application/json"}})
                self.assertEqual(status, 200, body[:300])
                self.assert_no_probe_hit(case)

    def test_shipped_gotenberg_refuses_webhook_and_download_from(self) -> None:
        case = "shipped-webhook"
        url = self.urls(case)
        status, body = self.send("paperless", {
            "url": "http://paperless-gotenberg:3000/forms/chromium/convert/html",
            "files": {"index.html": b64(b"<html><body><p>webhook</p></body></html>")},
            "headers": {"Gotenberg-Webhook-Url": url[0] + "-ok", "Gotenberg-Webhook-Error-Url": url[2] + "-err"},
        })
        self.assertNotEqual(status, 204, body[:300])
        self.assert_no_probe_hit(case)
        case = "shipped-download-from"
        status, body = self.send("paperless", {
            "url": "http://paperless-gotenberg:3000/forms/chromium/convert/html",
            "fields": {"downloadFrom": json.dumps([{"url": u + ".html"} for u in self.urls(case)])},
        })
        self.assertEqual(status, 400, body[:300])
        self.assert_no_probe_hit(case)

    # --- networks -------------------------------------------------------------------------

    def ip_of(self, service: str) -> str:
        cid = self.compose("ps", "-q", service).stdout.strip()
        out = subprocess.run(["docker", "inspect", "-f",
                              "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", cid],
                             capture_output=True, text=True, check=True).stdout.split()
        return out[0]

    def test_converters_reach_nothing_but_themselves(self) -> None:
        common = [
            "paperless-db:5432", f"{self.ip_of('paperless-db')}:5432",
            "paperless-broker:6379", f"{self.ip_of('paperless-broker')}:6379",
            f"{self.ips['addon']}:{ADDON_PORT}", f"{self.ips['public']}:{PUBLIC_PORT}",
            f"{METADATA_IP}:{METADATA_PORT}", OUTSIDE,
        ]
        other = {
            "paperless-gotenberg": ["paperless-tika:9998", f"{self.ip_of('paperless-tika')}:9998"],
            "paperless-tika": ["paperless-gotenberg:3000", f"{self.ip_of('paperless-gotenberg')}:3000"],
        }
        own = {"paperless-gotenberg": "127.0.0.1:3000", "paperless-tika": "127.0.0.1:9998"}
        for service, foreign in other.items():
            with self.subTest(service=service):
                got = self.connect(service, own[service], *common, *foreign)
                self.assertEqual(got.pop(own[service]), "OPEN", "control: the check connects at all")
                self.assertEqual(set(got.values()), {"CLOSED"}, got)

    def test_paperless_reaches_both_converters(self) -> None:
        got = self.connect("paperless", "paperless-gotenberg:3000", "paperless-tika:9998", "paperless-db:5432")
        self.assertEqual(set(got.values()), {"OPEN"}, got)

    def test_no_container_publishes_a_port(self) -> None:
        ids = self.compose("ps", "-q").stdout.split()
        self.assertTrue(ids)
        for cid in ids:
            ports = subprocess.run(["docker", "port", cid], capture_output=True, text=True, check=False).stdout
            self.assertEqual(ports.strip(), "", cid)

    # --- conversion through Paperless -----------------------------------------------------

    def find(self, marker: str) -> dict:
        """The one document whose full text holds `marker`, once consumed."""
        deadline = time.monotonic() + CONSUME_TIMEOUT
        got: dict = {}
        while time.monotonic() < deadline:
            got = self.api(f"/api/documents/?query={marker}")
            if got["status"] == 200 and got["body"]["count"] >= 1:
                return got["body"]
            time.sleep(5)
        tasks = self.api("/api/tasks/").get("body") or []
        if isinstance(tasks, dict):
            tasks = tasks.get("results") or []
        failed = [
            {k: t.get(k) for k in ("task_file_name", "status", "result")}
            for t in tasks if isinstance(t, dict) and t.get("status") == "FAILURE"
        ]
        logs = self.compose("logs", "--tail", "400", "paperless", check=False).stdout
        names = [name for name, _ in FIXTURES.values()]
        about = [line[-300:] for line in logs.splitlines()
                 if any(n in line for n in names) or "unsupported" in line.lower()
                 or "mime" in line.lower()][-12:]
        self.fail(f"no document found for {marker}: {got.get('status')}; failed tasks: {failed}; "
                  f"log lines: {about}")

    def assert_consumed(self, key: str) -> None:
        body = self.find(MARKERS[key])
        self.assertEqual(body["count"], 1, body)
        doc = body["results"][0]
        self.assertTrue(doc.get("archived_file_name"), f"{key}: no archive version")
        self.assertIn(MARKERS[key], doc.get("content", ""))
        archive = self.api(f"/api/documents/{int(doc['id'])}/download/", "file")
        self.assertEqual(archive["status"], 200)
        self.assertEqual(archive["head"], "%PDF-")
        self.assertIn("application/pdf", archive["type"])
        self.assert_no_probe_hit(f"consume-{key}")

    def test_docx_becomes_a_searchable_document_with_archive_pdf(self) -> None:
        self.assert_consumed("docx")

    def test_xlsx_becomes_a_searchable_document_with_archive_pdf(self) -> None:
        self.assert_consumed("xlsx")

    def test_odt_becomes_a_searchable_document_with_archive_pdf(self) -> None:
        self.assert_consumed("odt")

    def test_eml_with_html_body_and_attachment_becomes_a_searchable_document(self) -> None:
        self.assert_consumed("eml")

    def test_html_reaches_the_converters_as_mail_body_only(self) -> None:
        # Paperless-ngx 3.5.1 takes no stand-alone .html file from the consume
        # directory (it never queues it, with or without this hardening); HTML reaches
        # Gotenberg as the body of a mail, which the .eml case converts, and the
        # Chromium cases above send HTML straight to the routes.
        self.find(MARKERS["eml"])
        time.sleep(10)
        name = FIXTURES["html"][0]
        self.assertTrue((self.tmp / "consume" / name).exists(), "the .html file was taken after all")
        logs = self.compose("logs", "paperless", check=False).stdout
        self.assertNotIn(name, logs)
        self.assert_no_probe_hit("consume-html")


if __name__ == "__main__":
    unittest.main()
