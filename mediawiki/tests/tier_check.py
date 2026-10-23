#!/usr/bin/env python3
"""Access-tier leak checks for the mediawiki add-on.

Runs against a wiki started by run.sh (local login on, users seeded). A user
in the tier group writes a page, a file and a file talk page into the
Management tier, each carrying a random marker in content, edit summary,
upload comment and file bytes. A user outside the tier then requests every
path the add-on is meant to close -- page and file views, img_auth.php,
thumbnails, listings, feeds, logs, search, the Action API and the REST API,
revision and page ids, edit forms, link and category tables, the watchlist
and its notification mails -- and none of the responses may carry a marker
or serve the file. The tier
member and the break-glass admin act as positive controls, so a check cannot
pass merely because nothing was there to leak.

Standard library only. Exit status 0 when every check passes.
"""

import argparse
import http.cookiejar
import json
import re
import secrets
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

TIER = "Management"
PAGE = f"{TIER}:Plan"
UPLOAD_NAME = f"{TIER}:Plan.png"  # MediaWiki stores it as Management-Plan.png
FILE = f"{TIER}-Plan.png"
VARIANT_FILE = "MANAGEMENT -Variant.png"  # stored as MANAGEMENT_-Variant.png
TWIN_FILE = f"{TIER}-Twin.png"  # same content as PUBLIC_TWIN
PUBLIC_TWIN = "Public-twin.png"
GONE_FILE = f"{TIER}-Gone.png"  # deleted after the upload
PUBLIC_PAGE = "Public page"


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        # The test wiki serves a self-signed certificate.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def request(self, path: str, params=None, data=None, headers=None):
        """Return (status, body text, final URL); never raises on HTTP errors."""
        url = self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with self.opener.open(req, timeout=60) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), resp.geturl()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8", "replace"), url

    def api(self, params: dict, post: bool = False):
        params = {**params, "format": "json", "formatversion": "2"}
        if post:
            status, body, _ = self.request(
                "/api.php", data=urllib.parse.urlencode(params).encode()
            )
        else:
            status, body, _ = self.request("/api.php", params)
        return status, body, json.loads(body)

    def token(self, kind: str = "csrf") -> str:
        _, _, data = self.api({"action": "query", "meta": "tokens", "type": kind})
        return data["query"]["tokens"][f"{kind}token"]

    def login(self, user: str, password: str) -> None:
        _, _, data = self.api(
            {
                "action": "clientlogin",
                "username": user,
                "password": password,
                "loginreturnurl": self.base + "/",
                "logintoken": self.token("login"),
            },
            post=True,
        )
        if data.get("clientlogin", {}).get("status") != "PASS":
            raise SystemExit(f"login as {user} failed: {data}")

    def edit(self, title: str, text: str, summary: str) -> int:
        _, _, data = self.api(
            {"action": "edit", "title": title, "text": text, "summary": summary, "token": self.token()},
            post=True,
        )
        if data.get("edit", {}).get("result") != "Success":
            raise SystemExit(f"edit of {title} failed: {data}")
        return data["edit"]["newrevid"]

    def post_multipart(self, path: str, fields: dict, file_field: str, content: bytes) -> str:
        boundary = "----papaia" + secrets.token_hex(8)
        parts = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="upload.png"\r\n'
            "Content-Type: image/png\r\n\r\n".encode()
            + content
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        _, body, _ = self.request(
            path,
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return body

    def upload(self, filename: str, content: bytes, comment: str, text: str,
               ignore_warnings: bool = True) -> dict:
        fields = {
            "action": "upload",
            "filename": filename,
            "comment": comment,
            "text": text,
            "format": "json",
            "formatversion": "2",
            "token": self.token(),
        }
        if ignore_warnings:
            fields["ignorewarnings"] = "1"
        return json.loads(self.post_multipart("/api.php", fields, "file", content))

    def special_upload(self, filename: str, content: bytes) -> str:
        """Upload through the Special:Upload form; returns the page, e.g. its warnings."""
        fields = {
            "title": "Special:Upload",
            "wpSourceType": "file",
            "wpDestFile": filename,
            "wpUploadDescription": "form upload",
            "wpEditToken": self.token(),
            "wpUpload": "Upload file",
        }
        return self.post_multipart("/index.php", fields, "wpUploadFile", content)

    def get_bytes_status(self, path: str) -> int:
        try:
            with self.opener.open(self.base + path, timeout=60) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code


class MailCatcher:
    """The Mailpit HTTP API that run.sh sets up as the wiki's relay."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _call(self, path: str, method: str = "GET"):
        req = urllib.request.Request(self.base + path, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
        # DELETE answers a plain "ok".
        return json.loads(body) if body.startswith((b"{", b"[")) else None

    def clear(self) -> None:
        self._call("/api/v1/messages", "DELETE")

    def messages(self):
        """[(recipients, full text)] of every caught message."""
        out = []
        for msg in self._call("/api/v1/messages?limit=500").get("messages", []):
            full = self._call(f"/api/v1/message/{msg['ID']}")
            to = " ".join(t["Address"] for t in full.get("To", []))
            out.append((to, f"{full.get('Subject', '')}\n{full.get('Text', '')}\n{full.get('HTML', '')}"))
        return out

    def wait_for(self, recipient: str, nudge, seconds: int = 60):
        """Messages once one reaches the recipient (or the time is up); nudge() runs queued jobs."""
        deadline = time.monotonic() + seconds
        while True:
            texts = self.messages()
            if any(recipient in to for to, _ in texts) or time.monotonic() > deadline:
                # A notification to another watcher may be one job behind.
                nudge()
                time.sleep(2)
                return self.messages()
            nudge()
            time.sleep(1)


def png(marker: str, size: int = 200) -> bytes:
    """A solid-colour PNG carrying the marker in a tEXt chunk."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x20\x60\xa0" * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\x00" + marker.encode())
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class Checks:
    def __init__(self):
        self.failures = []
        self.count = 0

    def ok(self, name: str, condition: bool, detail: str = "") -> None:
        self.count += 1
        if condition:
            print(f"  ok    {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            self.failures.append(name)

    def no_leak(self, name: str, body: str, markers) -> None:
        found = [m for m in markers if m in body]
        self.ok(name, not found, f"(leaks {', '.join(found)})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="wiki URL, e.g. https://127.0.0.1:18020")
    ap.add_argument("--member", default="Member")
    ap.add_argument("--outsider", default="Outsider")
    ap.add_argument("--admin", default="Admin")
    ap.add_argument("--password", required=True, help="password of member and outsider")
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--mail-api", help="Mailpit API of the wiki's relay; watchlist mail checks run only with it")
    args = ap.parse_args()

    run = secrets.token_hex(4)
    m_content = f"ZQXPAGE{run}"
    m_summary = f"ZQXSUMMARY{run}"
    m_upload = f"ZQXUPLOAD{run}"
    m_filedesc = f"ZQXFILEDESC{run}"
    m_bytes = f"ZQXBYTES{run}"
    m_talk = f"ZQXTALK{run}"
    m_tiertalk = f"ZQXTIERTALK{run}"
    # What the tier page's text puts into the link, category and property
    # tables: listings built from those tables must not carry it either.
    m_ext = f"ZQXEXT{run}"
    m_red = f"ZQXRED{run}"
    m_cat = f"ZQXCAT{run}"
    m_tpl = f"ZQXTPL{run}"
    m_wanted = f"ZQXWANTED{run}"
    m_sort = f"ZQXSORT{run}"
    m_mail = f"ZQXMAIL{run}"
    m_redir = f"ZQXREDIR{run}"
    # Search terms: a search page echoes its query, so the queries use words
    # that sit next to the markers instead of the markers themselves.
    w_page = f"ZQXWORDPAGE{run}"
    w_file = f"ZQXWORDFILE{run}"
    ext_host = f"zqxext{run}.example.org"
    markers = [m_content, m_summary, m_upload, m_filedesc, m_talk, m_tiertalk,
               m_ext, m_red, m_cat, m_tpl, m_wanted, m_sort, m_mail, m_redir]
    checks = Checks()

    # ── Seed, as the tier member ───────────────────────────────────────────
    member = Client(args.base)
    member.login(args.member, args.password)
    # A visible entry older than the tier entries, so filtered lists have
    # entries removed from their middle.
    member.edit("Early public page", "Early.", "early public")
    member.upload("Early-public.png", png("public"), "early public file", "Public file.")
    up = member.upload(UPLOAD_NAME, png(m_bytes), f"upload {m_upload}", f"Description {w_file} {m_filedesc}")
    stored = up.get("upload", {}).get("filename")
    checks.ok("upload named Tier:name lands as Tier-name", stored == FILE, f"({up})")
    up = member.upload(VARIANT_FILE, png(m_bytes + "v"), f"upload {m_upload}", f"Description {m_filedesc}")
    variant = up.get("upload", {}).get("filename", "")
    checks.ok("non-canonical tier prefix upload", variant.lower().startswith("management_-"), f"({up})")
    twin_bytes = png(f"twin{run}")
    gone_bytes = png(f"gone{run}")
    member.upload(TWIN_FILE, twin_bytes, "twin", "Twin.")
    member.upload(PUBLIC_TWIN, twin_bytes, "public twin", "Public twin.")
    member.upload(GONE_FILE, gone_bytes, "gone", "Gone.")
    member.edit(f"File talk:{FILE}", f"Talk {m_talk}", f"talk {m_summary}")
    rev1 = member.edit(PAGE, f"First {m_content}", f"first {m_summary}")
    rev2 = member.edit(
        PAGE,
        f"Plan {w_page} {m_content}\n\n[[File:{FILE}|50px]]\n\n"
        f"https://{ext_host}/{m_ext} [[{m_red}]] {{{{{m_tpl}}}}} [[File:{m_wanted}.png]]\n\n"
        f"[[Category:{m_cat}]] {{{{DEFAULTSORT:{m_sort}}}}}",
        f"second {m_summary}",
    )
    member.edit(f"{TIER} talk:Plan", f"Tier talk {m_tiertalk}", f"tier talk {m_summary}")
    member.edit(f"{TIER}:Shortcut", f"#REDIRECT [[{m_redir}]]", "tier redirect")
    pub_rev = member.edit(
        PUBLIC_PAGE,
        f"Public text.\n\n{{{{:{PAGE}}}}}\n\n[[File:{FILE}|50px]]\n\n[[Media:{FILE}]]",
        "public page",
    )
    _, _, data = member.api({"action": "query", "titles": PAGE, "prop": "info"})
    page_id = data["query"]["pages"][0]["pageid"]

    _, html, _ = member.request("/index.php", {"title": PAGE})
    thumb = re.search(r'src="(/img_auth\.php/thumb/[^"]+)"', html)
    _, _, info = member.api(
        {"action": "query", "titles": f"File:{FILE}", "prop": "imageinfo", "iiprop": "url"}
    )
    original = urllib.parse.urlparse(info["query"]["pages"][0]["imageinfo"][0]["url"]).path
    _, _, info = member.api(
        {"action": "query", "titles": f"File:{variant}", "prop": "imageinfo", "iiprop": "url"}
    )
    variant_original = urllib.parse.urlparse(info["query"]["pages"][0]["imageinfo"][0]["url"]).path

    print("\nPositive controls (tier member, admin)")
    checks.ok("member reads the tier page", m_content in html)
    checks.ok("member's tier page embeds the tier file", thumb is not None)
    checks.ok("member gets the original file", member.get_bytes_status(original) == 200)
    if thumb:
        checks.ok("member gets the thumbnail", member.get_bytes_status(thumb.group(1)) == 200)
    _, body, _ = member.api(
        {"action": "query", "list": "search", "srsearch": w_file, "srnamespace": "6", "srwhat": "text"}
    )
    checks.ok("member finds the file description", FILE in body.replace(" ", "_"), body[:300])
    _, body, _ = member.request("/index.php", {"title": "Special:Search", "search": w_page, "fulltext": "1", "ns3002": "1"})
    checks.ok("member finds the tier page", m_content in body)
    admin = Client(args.base)
    admin.login(args.admin, args.admin_password)
    _, body, _ = admin.request("/index.php", {"title": "Special:ListFiles", "limit": "500"})
    checks.ok("admin lists the tier file", m_upload in body)
    _, body, _ = admin.request("/index.php", {"title": "Special:Log", "type": "upload"})
    checks.ok("admin sees the upload log entry", m_upload in body)
    _, _, data = admin.api(
        {"action": "delete", "title": f"File:{GONE_FILE}", "reason": "gone", "token": admin.token()}, post=True)
    if "delete" not in data:
        raise SystemExit(f"delete of {GONE_FILE} failed: {data}")
    _, body, _ = member.api({"action": "query", "titles": f"File:{PUBLIC_TWIN}", "prop": "duplicatefiles"})
    checks.ok("member sees the tier twin in prop=duplicatefiles", TWIN_FILE in body.replace(" ", "_"), body[:300])
    dup = json.dumps(member.upload("Member-twin.png", twin_bytes, "x", "x", ignore_warnings=False))
    checks.ok("member's duplicate warning names the tier twin", TWIN_FILE in dup, dup[:300])
    dup = json.dumps(member.upload("Member-gone.png", gone_bytes, "x", "x", ignore_warnings=False))
    checks.ok("member's deleted-duplicate warning names the tier file", GONE_FILE in dup, dup[:300])
    # Link-table listings are refused to everyone with a hidden tier, the
    # member included (Operations); the admin reads every tier.
    _, body, _ = admin.api({"action": "query", "list": "alllinks", "allimit": "max"})
    checks.ok("admin sees the tier page's link target in list=alllinks", m_red in body)
    _, body, _ = member.api({"action": "query", "list": "exturlusage", "euquery": ext_host, "eulimit": "max"})
    checks.ok("member finds the tier page's external link", m_ext in body, body[:300])
    _, body, _ = admin.request("/index.php", {"title": "Special:WantedPages", "limit": "500"})
    checks.ok("admin sees the tier page's red link in Special:WantedPages", m_red in body)
    _, body, _ = admin.api({"action": "query", "list": "allredirects", "arprop": "title", "arlimit": "max"})
    checks.ok("admin sees the tier redirect's target in list=allredirects", m_redir in body)
    _, body, _ = member.request("/index.php", {"title": f"{TIER} talk:Plan"})
    checks.ok("member reads the tier talk page", m_tiertalk in body)

    # ── Outsider ───────────────────────────────────────────────────────────
    out = Client(args.base)
    out.login(args.outsider, args.password)
    wiki = lambda title, **p: out.request("/index.php", {"title": title, **p})[1]

    print("\nTier page, browser")
    checks.no_leak("view", wiki(PAGE), markers)
    checks.no_leak("raw", wiki(PAGE, action="raw"), markers)
    checks.no_leak("history", wiki(PAGE, action="history"), markers)
    checks.no_leak("old revision", out.request("/index.php", {"oldid": rev1})[1], markers)
    checks.no_leak("diff", out.request("/index.php", {"diff": rev2, "oldid": rev1})[1], markers)
    for variant_title in (f":{PAGE}", f"{TIER} :Plan", f"{TIER.upper()}:Plan", f"{TIER.lower()}:Plan"):
        checks.no_leak(f"view {variant_title!r}", wiki(variant_title), markers)
    checks.no_leak("Special:Search", wiki("Special:Search", search=w_page, fulltext="1", ns3002="1"), markers)
    checks.no_leak("Special:Export", wiki(f"Special:Export/{PAGE}"), markers)
    checks.no_leak("Special:RecentChanges", wiki("Special:RecentChanges", days="30", limit="500"), markers)
    checks.no_leak("RecentChanges feed", wiki("Special:RecentChanges", feed="atom", days="30"), markers)
    checks.no_leak("Special:NewPages", wiki("Special:NewPages", limit="500"), markers)
    checks.no_leak("Special:Contributions", wiki(f"Special:Contributions/{args.member}", limit="500"), markers)
    checks.no_leak("Special:Log", wiki("Special:Log", limit="500"), markers)
    checks.no_leak("Special:ExpandTemplates", wiki(
        "Special:ExpandTemplates", wpInput=f"{{{{:{PAGE}}}}}", wpContextTitle=PAGE), markers)
    checks.no_leak("transclusion on a public page", wiki(PUBLIC_PAGE), markers)

    print("\nTier page, Action API")
    api = lambda **p: out.api(p)[1]
    checks.no_leak("prop=revisions by title", api(
        action="query", titles=PAGE, prop="revisions", rvprop="content|comment", rvslots="main"), markers)
    checks.no_leak("prop=revisions by revid", api(
        action="query", revids=rev1, prop="revisions", rvprop="content|comment", rvslots="main"), markers)
    checks.no_leak("generator=allpages", api(
        action="query", generator="allpages", gapnamespace="3002", prop="revisions",
        rvprop="content|comment", rvslots="main"), markers)
    checks.no_leak("parse page", api(action="parse", page=PAGE), markers)
    checks.no_leak("parse text with a tier context title", api(
        action="parse", text=f"{{{{:{PAGE}}}}}", title=f"{TIER}:Other", contentmodel="wikitext"), markers)
    checks.no_leak("parse text with a public context title", api(
        action="parse", text=f"{{{{:{PAGE}}}}}", title=PUBLIC_PAGE, contentmodel="wikitext"), markers)
    checks.no_leak("expandtemplates", api(
        action="expandtemplates", text=f"{{{{:{PAGE}}}}}", title=PAGE, prop="wikitext"), markers)
    checks.no_leak("compare", api(action="compare", fromrev=rev1, torev=rev2, prop="diff|comment"), markers)
    checks.no_leak("list=search", api(
        action="query", list="search", srsearch=w_page, srnamespace="0|6|3002", srwhat="text"), markers)
    checks.no_leak("list=recentchanges", api(
        action="query", list="recentchanges", rcprop="title|comment", rclimit="500"), markers)
    for module, prefix in (("recentchanges", "rc"), ("logevents", "le"), ("allimages", "ai")):
        _, _, data = out.api({"action": "query", "list": module, f"{prefix}limit": "500"})
        entries = data.get("query", {}).get(module)
        checks.ok(f"list={module} stays a JSON list after filtering", isinstance(entries, list) and len(entries) > 0,
                  f"({type(entries).__name__})")
    checks.no_leak("list=allpages", api(action="query", list="allpages", apnamespace="3002"), [])
    checks.no_leak("list=logevents", api(
        action="query", list="logevents", leprop="title|comment", lelimit="500"), markers)
    checks.no_leak("list=usercontribs", api(
        action="query", list="usercontribs", ucuser=args.member, ucprop="title|comment", uclimit="500"), markers)
    checks.no_leak("list=allrevisions", api(
        action="query", list="allrevisions", arvprop="content|comment", arvslots="main", arvlimit="50"), markers)
    checks.no_leak("export", api(action="query", titles=PAGE, export="1"), markers)

    print("\nTier page, REST API")
    rest = lambda path: out.request("/rest.php" + path)[1]
    enc = lambda title: urllib.parse.quote(title.replace(" ", "_"), safe="")
    for variant_title in (PAGE, f"{TIER.upper()}:Plan", f"{TIER} :Plan"):
        checks.no_leak(f"page {variant_title!r}", rest(f"/v1/page/{enc(variant_title)}"), markers)
    checks.no_leak("page/html", rest(f"/v1/page/{enc(PAGE)}/html"), markers)
    checks.no_leak("page/with_html", rest(f"/v1/page/{enc(PAGE)}/with_html"), markers)
    checks.no_leak("page/history", rest(f"/v1/page/{enc(PAGE)}/history"), markers)
    checks.no_leak("revision", rest(f"/v1/revision/{rev1}"), markers)
    checks.no_leak("revision/compare", rest(f"/v1/revision/{rev1}/compare/{rev2}"), markers)
    checks.no_leak("search/page", rest(f"/v1/search/page?q={w_page}"), markers)

    print("\nTier file")
    file_markers = markers + [m_bytes]
    checks.no_leak("file page", wiki(f"File:{FILE}"), file_markers)
    checks.no_leak("file page, raw", wiki(f"File:{FILE}", action="raw"), file_markers)
    checks.no_leak("file page, history", wiki(f"File:{FILE}", action="history"), file_markers)
    checks.no_leak("file talk page", wiki(f"File talk:{FILE}"), file_markers)
    checks.no_leak("file page, non-canonical prefix", wiki(f"File:{TIER.lower()}-Plan.png"), file_markers)
    checks.ok("img_auth.php refuses the original", out.get_bytes_status(original) == 403)
    if thumb:
        checks.ok("img_auth.php refuses the thumbnail", out.get_bytes_status(thumb.group(1)) == 403)
    checks.ok("img_auth.php refuses the non-canonical variant", out.get_bytes_status(variant_original) == 403)
    checks.ok("Special:FilePath does not serve it",
              out.get_bytes_status(f"/index.php?title=Special:FilePath/{urllib.parse.quote(FILE)}") != 200)
    checks.ok("thumb.php does not serve it",
              out.get_bytes_status(f"/thumb.php?f={urllib.parse.quote(FILE)}&width=40") != 200)
    status, body, _ = out.request(f"/rest.php/v1/file/{enc('File:' + FILE)}")
    checks.ok("REST file endpoint refuses it", status in (403, 404), f"({status})")
    checks.no_leak("REST file endpoint", body, file_markers)
    public_html = wiki(PUBLIC_PAGE)
    checks.ok("public page does not embed the tier file",
              "img_auth.php" not in public_html or FILE.replace(" ", "_") not in urllib.parse.unquote(public_html))
    checks.no_leak("parse Media: link with a public context", api(
        action="parse", text=f"[[Media:{FILE}]] [[File:{FILE}]]", title=PUBLIC_PAGE,
        contentmodel="wikitext"), file_markers + ["img_auth.php"])
    for special in ("ListFiles", "NewFiles", "MIMESearch/image/png", "ListDuplicatedFiles",
                    "FileDuplicateSearch", "UnusedFiles", "UncategorizedFiles"):
        checks.no_leak(f"Special:{special}", wiki(f"Special:{special}", limit="500"), file_markers)
    checks.no_leak("Special:Log/upload", wiki("Special:Log", type="upload", limit="500"), file_markers)
    checks.no_leak("Special:Search, files", wiki(
        "Special:Search", search=w_file, fulltext="1", ns6="1"), file_markers)
    checks.no_leak("Special:RecentChanges, uploads", wiki(
        "Special:RecentChanges", days="30", limit="500", hidebots="0"), file_markers)
    checks.no_leak("list=allimages", api(
        action="query", list="allimages", aiprop="comment|url|metadata", ailimit="500"), file_markers)
    body = api(action="query", list="allimages", ailimit="500")
    checks.ok("list=allimages omits the tier files", "Plan.png" not in body and "Variant.png" not in body)
    checks.no_leak("prop=imageinfo", api(
        action="query", titles=f"File:{FILE}", prop="imageinfo",
        iiprop="url|comment|metadata|sha1"), file_markers + [original])
    checks.no_leak("list=search, files", api(
        action="query", list="search", srsearch=w_file, srnamespace="6", srwhat="text"), file_markers)
    checks.no_leak("list=logevents, uploads", api(
        action="query", list="logevents", letype="upload", leprop="title|comment"), file_markers)
    checks.no_leak("list=usercontribs, files", api(
        action="query", list="usercontribs", ucuser=args.member, ucnamespace="6|7",
        ucprop="title|comment"), file_markers)
    checks.no_leak("REST page of the file", rest(f"/v1/page/{enc('File:' + FILE)}"), file_markers)
    checks.no_leak("REST search, files", rest(f"/v1/search/page?q={w_file}"), file_markers)

    print("\nDuplicate lookups by content")
    checks.no_leak("prop=duplicatefiles", api(
        action="query", titles=f"File:{PUBLIC_TWIN}", prop="duplicatefiles"), [TWIN_FILE])
    dup = json.dumps(out.upload("Outsider-twin.png", twin_bytes, "x", "x", ignore_warnings=False))
    checks.ok("API upload still warns about the visible duplicate", PUBLIC_TWIN in dup, dup[:300])
    checks.no_leak("API upload duplicate warning", dup, [TWIN_FILE])
    page = out.special_upload("Outsider-twin.png", twin_bytes)
    checks.ok("Special:Upload still warns about the visible duplicate", PUBLIC_TWIN in page.replace(" ", "_"))
    checks.no_leak("Special:Upload duplicate warning", page.replace(" ", "_"), [TWIN_FILE])
    dup = json.dumps(out.upload("Outsider-gone.png", gone_bytes, "x", "x", ignore_warnings=False))
    checks.no_leak("API upload deleted-duplicate warning", dup, [GONE_FILE])
    page = out.special_upload("Outsider-gone.png", gone_bytes)
    checks.no_leak("Special:Upload deleted-duplicate warning", page.replace(" ", "_"), [GONE_FILE])

    print("\nTier page by id, and the tier talk page")
    checks.no_leak("curid", out.request("/index.php", {"curid": page_id})[1], markers)
    for label, special in (("PermanentLink", f"PermanentLink/{rev1}"),
                           ("Redirect/revision", f"Redirect/revision/{rev1}"),
                           ("Redirect/page", f"Redirect/page/{page_id}"),
                           ("Diff", f"Diff/{rev2}"), ("Diff between revisions", f"Diff/{rev1}/{rev2}")):
        checks.no_leak(f"Special:{label}", wiki(f"Special:{special}"), markers)
    checks.no_leak("tier revision under a public title", wiki(PUBLIC_PAGE, oldid=rev1), markers)
    checks.no_leak("tier revision under a public title, raw", wiki(PUBLIC_PAGE, oldid=rev1, action="raw"), markers)
    checks.no_leak("diff from a public revision to a tier revision", wiki(PUBLIC_PAGE, diff=rev2, oldid=pub_rev),
                   markers)
    checks.no_leak("action=info", wiki(PAGE, action="info"), markers)
    checks.no_leak("action=credits", wiki(PAGE, action="credits"), markers)
    checks.no_leak("Special:ComparePages by title", wiki(
        "Special:ComparePages", page1=PUBLIC_PAGE, page2=PAGE), markers)
    checks.no_leak("Special:ComparePages by revision", wiki(
        "Special:ComparePages", rev1=pub_rev, rev2=rev2), markers)
    checks.no_leak("tier talk page", wiki(f"{TIER} talk:Plan"), markers)
    checks.no_leak("tier talk page, raw", wiki(f"{TIER} talk:Plan", action="raw"), markers)
    for path in (f"/v1/revision/{rev1}/bare", f"/v1/revision/{rev1}/html", f"/v1/revision/{rev1}/with_html",
                 f"/v1/page/{enc(PAGE)}/bare", f"/v1/page/{enc(TIER + '_talk:Plan')}"):
        checks.no_leak(f"REST {path.split('/')[2]} {path.rsplit('/', 1)[-1]}", rest(path), markers)

    print("\nEdit form of a public page")
    checks.no_leak("preload from the tier", wiki(PUBLIC_PAGE, action="edit", preload=PAGE), markers)
    checks.no_leak("preload into a new page", wiki("Outsider sandbox", action="edit", preload=PAGE), markers)
    checks.no_leak("preload into a new section", wiki(
        PUBLIC_PAGE, action="edit", section="new", preload=PAGE), markers)
    checks.no_leak("undo of tier revisions", wiki(PUBLIC_PAGE, action="edit", undo=rev2, undoafter=rev1), markers)
    checks.no_leak("edit form for a tier revision", wiki(PUBLIC_PAGE, action="edit", oldid=rev1), markers)
    checks.no_leak("edit form of the tier page", wiki(PAGE, action="edit"), markers)

    print("\nLinks, categories and properties of the tier page")
    checks.no_leak("prop=links|templates|categories|extlinks|images|pageprops", api(
        action="query", titles=PAGE, prop="links|templates|categories|extlinks|images|pageprops",
        pllimit="max", tllimit="max", cllimit="max", ellimit="max", imlimit="max"), markers)
    checks.no_leak("list=alllinks", api(action="query", list="alllinks", allimit="max"), markers)
    checks.no_leak("list=alltransclusions", api(action="query", list="alltransclusions", atlimit="max"), markers)
    checks.no_leak("list=allfileusages", api(action="query", list="allfileusages", aflimit="max"), markers)
    checks.no_leak("list=allcategories", api(action="query", list="allcategories", aclimit="max"), markers)
    checks.no_leak("list=exturlusage", api(
        action="query", list="exturlusage", euquery=ext_host, euprop="title|url", eulimit="max"), markers)
    checks.no_leak("list=exturlusage, all", api(
        action="query", list="exturlusage", euprop="title|url", eulimit="max"), markers)
    checks.no_leak("list=pageswithprop", api(
        action="query", list="pageswithprop", pwppropname="defaultsort", pwpprop="title|value"), markers)
    checks.no_leak("list=allredirects", api(action="query", list="allredirects", arprop="title|ids", arlimit="max"),
                   markers)
    querypages = ("Wantedpages", "Wantedcategories", "Wantedtemplates", "Wantedfiles", "Mostlinked",
                  "Mostlinkedcategories", "Mostlinkedtemplates", "Mostcategories", "Fewestrevisions",
                  "Deadendpages", "Lonelypages", "Shortpages", "Longpages", "Ancientpages", "Listredirects")
    for qp in querypages:
        checks.no_leak(f"list=querypage {qp}", api(action="query", list="querypage", qppage=qp, qplimit="max"),
                       markers)
    for special in querypages + ("Categories", "UncategorizedPages", "AllPages"):
        checks.no_leak(f"Special:{special}", wiki(f"Special:{special}", limit="500"), markers)
    checks.no_leak("Special:LinkSearch", wiki("Special:LinkSearch", target=ext_host), markers)
    checks.no_leak("Special:LinkSearch, all", wiki("Special:LinkSearch", target="*.example.org"), markers)
    checks.no_leak("Special:PagesWithProp", wiki("Special:PagesWithProp", propname="defaultsort"), markers)

    print("\nWatchlist")
    mail = MailCatcher(args.mail_api) if args.mail_api else None
    if mail:
        mail.clear()
    for client in (member, out):
        client.api({"action": "watch", "titles": PAGE, "token": client.token("watch")}, post=True)
    admin.edit(PAGE, f"Plan {w_page} {m_content} after review", f"review {m_mail}")
    checks.no_leak("list=watchlist", api(
        action="query", list="watchlist", wlprop="title|comment|parsedcomment|user", wllimit="max"), markers)
    checks.no_leak("Special:Watchlist", wiki("Special:Watchlist", days="30"), markers)
    feed = lambda **p: out.request("/api.php", p)[1]  # XML, not JSON
    checks.no_leak("watchlist feed", feed(action="feedwatchlist", feedformat="atom"), markers)
    checks.no_leak("contributions feed", feed(action="feedcontributions", user=args.admin), markers)
    checks.no_leak("recent changes feed", feed(action="feedrecentchanges", days="30"), markers)
    if mail:
        # Notifications go out from the job queue, which runs on web requests.
        texts = mail.wait_for("member@example.org", lambda: member.request("/index.php", {"title": PUBLIC_PAGE}))
        checks.ok("member gets the watchlist mail for the tier page",
                  any(m_mail in t for to, t in texts if "member@example.org" in to))
        outsider_mail = [t for to, t in texts if "outsider@example.org" in to]
        checks.ok("outsider gets no watchlist mail for the tier page", not outsider_mail,
                  f"({len(outsider_mail)} mails)")
        checks.no_leak("watchlist mails to the outsider", "\n".join(outsider_mail), markers)
    else:
        print("  skip  watchlist mail (no --mail-api)")

    print("\nWrites")
    _, _, data = out.api(
        {"action": "edit", "title": f"{TIER}:Outsider", "text": "x", "createonly": "1", "token": out.token()},
        post=True)
    checks.ok("outsider cannot create a page in the tier", "error" in data, f"({data})")
    _, _, data = out.api(
        {"action": "edit", "title": f"{TIER} talk:Plan", "appendtext": "x", "token": out.token()}, post=True)
    checks.ok("outsider cannot edit the tier talk page", "error" in data, f"({data})")
    out.edit("Outsider page", "Outsider text.", "outsider page")
    _, _, data = out.api(
        {"action": "move", "from": "Outsider page", "to": f"{TIER}:Outsider page", "token": out.token()}, post=True)
    checks.ok("outsider cannot move a page into the tier", "error" in data, f"({data})")
    out.edit("Outsider redirect", f"#REDIRECT [[{PAGE}]]", "redirect")
    checks.no_leak("own redirect to the tier page", wiki("Outsider redirect"), markers)
    out.edit("Outsider lever", f"{{{{:{PAGE}}}}} {{{{msgnw::{PAGE}}}}} {{{{raw::{PAGE}}}}} "
             f"{{{{{TIER}:Plan}}}} {{{{:{TIER.lower()}:Plan}}}} {{{{:{TIER} talk:Plan}}}}", "lever")
    checks.no_leak("own page transcluding the tier", wiki("Outsider lever"), markers)
    checks.no_leak("own page transcluding the tier, parsed by the API", api(action="parse", page="Outsider lever"),
                   markers)
    planted = out.upload(f"{TIER}-Planted.png", png("planted"), "planted", "planted")
    checks.ok("outsider cannot upload into the tier", "error" in planted, f"({planted})")
    over = out.upload(FILE, png("overwrite"), "overwrite", "overwrite")
    checks.ok("outsider cannot overwrite a tier file", "error" in over, f"({over})")
    _, _, data = out.api(
        {"action": "edit", "title": PAGE, "text": "x", "token": out.token()}, post=True)
    checks.ok("outsider cannot edit the tier page", "error" in data, f"({data})")
    _, _, data = out.api(
        {"action": "move", "from": "File:" + FILE, "to": "File:Moved.png", "token": out.token()}, post=True)
    checks.ok("outsider cannot move a tier file out", "error" in data, f"({data})")

    print(f"\n{checks.count - len(checks.failures)}/{checks.count} checks passed")
    if checks.failures:
        print("Failed: " + "; ".join(checks.failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
