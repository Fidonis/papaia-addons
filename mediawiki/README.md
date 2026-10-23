# papaia-addon-mediawiki

MediaWiki knowledge base add-on for [papaia](https://github.com/Fidonis/papaia).

Adds a MediaWiki wiki with Keycloak login and group sync, namespace-level
access tiers, and the [MediaWiki MCP Server](https://github.com/ProfessionalWiki/MediaWiki-MCP-Server),
through which LibreChat works in the wiki as the signed-in user -- within the
same tiers the browser enforces.

**Requires papaia ≥ 1.3.0** (`addon_api` 2).

---

## Before you start: one URL for the browser and LibreChat

The wiki and its MCP server share one URL, `MW_SERVER` (the MCP server is
`<MW_SERVER>/mcp`). The browser uses it to log in and to consent, and the
LibreChat container uses it to fetch the sign-in metadata, exchange tokens and
call the tools. MCP clients refuse a plain-http server, so it must be **https**
and reachable **from both sides**:

| Setup | `MW_SERVER` | What else |
|---|---|---|
| Single host with the bundled Keycloak (core default `https://host.docker.internal:8110`) | `https://host.docker.internal:8020` (the default) | Nothing: the wiki's port speaks https with a certificate from the papaia local CA, which LibreChat and the browsers that use the bundled Keycloak already trust, and the add-on's LibreChat fragment points there. |
| Behind a reverse proxy with a real host name | `https://wiki.example.org` | Proxy host for `mediawiki:80` ([Reverse proxy](#reverse-proxy)), LibreChat overlay with the URL ([LibreChat](#librechat)). |

Either way:

- Only the **wiki login** has a Keycloak client (`mediawiki.json`). The MCP
  server has none: users sign in to it through the wiki.
- LibreChat's sign-in callback must be allowed. The default covers LibreChat
  at `http://host.docker.internal:8000` and at `http://localhost:<any port>`;
  any other `DOMAIN_SERVER` goes into `MEDIAWIKI_MCP_ALLOWED_REDIRECTS`.

If LibreChat reports *"The MCP server must be authenticated"* or *"No OAuth
metadata discovered"* when connecting, one of these does not hold; see
[LibreChat](#librechat).

---

## Services

| Service | Port | Description |
|---|---|---|
| `mediawiki` | `8020` (configurable), **https** | MediaWiki 1.43 (LTS) with PluggableAuth, OpenIDConnect, Lockdown, OAuth, VisualEditor, Cite, ParserFunctions; also serves the MCP server under `/mcp`. Plain-http port 80 serves the reverse proxy on the add-on network. |
| `mediawiki-db` | internal | MariaDB 11.8 |
| `mediawiki-mcp` | via the wiki, `/mcp` | [MediaWiki MCP Server](https://github.com/ProfessionalWiki/MediaWiki-MCP-Server) with per-user OAuth sign-in |

All services run on the add-on's own bridge network,
`${PAPAIA_PROJECT:-papaia}-mediawiki-net` — `papaia-mediawiki-net` on a default
single-stack host, or `papaia-<env>-mediawiki-net` when several papAIa
deployments share a host. Only the wiki is published on a host port. It
proxies `/mcp` and the OAuth discovery paths (`/.well-known/oauth-*`) to the
MCP server, so one host name, certificate and proxy entry serve both.

| Volume | Content |
|---|---|
| `mediawiki-dbdata` | Pages, revisions, users, groups |
| `mediawiki-images` | Uploaded files |
| `mediawiki-oauth` | Key pair that signs the wiki's OAuth access tokens |
| `mediawiki-mcp-credentials` | The MCP server's OAuth consumer key and secret, written by the wiki |
| `mediawiki-mcp-data` | The MCP server's encrypted session store (who is signed in) |
| `mediawiki-tls` | Certificate and key of the published https port |

### The image

`image/` is the build context of the wiki image: the official `mediawiki:1.43`
image plus PluggableAuth, OpenIDConnect, Lockdown and OAuth, each pinned to a
commit, and the Apache configuration. It is published as
`ghcr.io/fidonis/papaia-mediawiki:<MediaWiki version>-<add-on version>`
(currently `1.43.11-1.0.0`), the tag `docker-compose.yml` pulls by default.

The add-on logic is not baked in: `runtime/` (the environment-driven
`LocalSettings.php` with the tier hooks, the entrypoint and the OAuth
bootstrap) is mounted read-only into the container. An add-on update
therefore takes effect on the next start of an existing install, without a
new image, and a change in `image/` comes with a new add-on version and so a
new tag.

When the image cannot be pulled, Docker Compose builds it from `image/`
instead. That needs internet access and takes several minutes; if an
automatic start after install gives up during the build, start the add-on
again once the build has finished.

---

## Installation via papaia-ctl

```bash
# 1. Clone this add-on into your workspace
git clone https://github.com/Fidonis/papaia-addon-mediawiki addons/mediawiki

# 2. Install — seeds .env in the config bundle, registers in deployment.yaml, renders config
papaia-ctl addon install mediawiki --path=addons/mediawiki

# 3. Import the Keycloak client and copy its secret (see "Keycloak" below)
#    into KC_MEDIAWIKI_CLIENT_SECRET in <papaia-config>/addons/mediawiki/.env

# 4. Not the single-host default? Set MW_SERVER (and, if LibreChat's
#    DOMAIN_SERVER differs, MEDIAWIKI_MCP_ALLOWED_REDIRECTS) in that .env,
#    and point LibreChat at <MW_SERVER>/mcp (see "LibreChat" below)

# 5. Start the add-on, then the core so nginx joins its network and
#    LibreChat picks up the MCP server
papaia-ctl addon start mediawiki
papaia-ctl start
```

Database password, wiki keys, the break-glass admin password and the MCP
server's signing key are generated on install. The wiki creates the MCP
server's OAuth consumer itself and hands it the credentials over a shared
volume, so no MCP credential has to be copied by hand.

---

## First start

The wiki container brings itself into a usable state before Apache starts
(`runtime/entrypoint.sh`), and every step is safe to repeat:

1. **TLS:** the published port gets a certificate for the host of
   `MW_SERVER`, issued by the papaia local CA (`$PAPAIA_CONFIG_DIR/certs`)
   when it is present, self-signed otherwise. It is kept on the
   `mediawiki-tls` volume and renewed when the host or the CA changes or
   expiry is less than 30 days away.
2. **Empty database:** `install.php` creates the schema and the break-glass
   admin. The add-on's own `LocalSettings.php` is used afterwards; the one
   `install.php` writes is discarded.
3. **Once per container:** `update.php` creates extension tables and migrates
   the schema after an image upgrade. A new image means a new container, so
   upgrades are covered without paying for it on every restart.
4. **Every start:** the break-glass admin gets its groups and the password
   from `MW_ADMIN_PASSWORD`; rotating it takes effect after a restart.
5. **OAuth:** the key pair that signs access tokens is generated once and kept
   on the `mediawiki-oauth` volume. The MCP server's OAuth 2.0 consumer
   (`papaia-mcp`, owned by the break-glass admin) is created and approved for
   the callback `<MCP public URL>/oauth/callback` and the grants in
   `MEDIAWIKI_MCP_GRANTS`; later starts find it again. The OAuth extension
   never changes the callback or grants of an existing consumer, so a changed
   public URL or grant list creates a new consumer and disables the old one;
   users then consent once more. The key and secret go to the
   `mediawiki-mcp-credentials` volume, which the MCP server reads at start.

---

## Keycloak

Import one client into the `papaia` realm — it is for the **wiki login**
only:

| File | Where |
|---|---|
| `integration/infra/keycloak/mediawiki.json` | Clients → Import client |

After the import:

- **Root URL of `mediawiki`:** set it to `MW_SERVER`. The redirect URIs are
  relative to it (`/index.php/Special:PluggableAuthLogin`); the imported value
  is the default `https://host.docker.internal:8020`.
- **Secret:** copy the client secret (Credentials tab) into
  `KC_MEDIAWIKI_CLIENT_SECRET`.

The `groups` protocol mapper on the client puts the user's realm roles into
the `groups` claim, the same shape as the paperless add-on. The MCP server
needs no Keycloak client: users sign in to it through the wiki, so their
Keycloak login is the wiki's.

---

## Access tiers

A tier is a MediaWiki namespace that only some groups may read and edit.
Everyone who logs in reads and edits the main namespace; anonymous visitors
see nothing but the login.

```bash
# Name:id — ids even and >= 3000; each gets a talk namespace at id+1
MW_NAMESPACES=Internal:3000,Management:3002,Operations:3004

# Name=group,group;Name=group — values of the groups claim
MW_NAMESPACE_READ_GROUPS=Internal=internal,management,papaia-admin;Management=management,papaia-admin;Operations=papaia-admin

# Members become sysop + bureaucrat + interface-admin, and can read every tier
MW_SYSOP_GROUPS=papaia-admin
```

Every tier group named here becomes a wiki group of the same name.
PluggableAuth syncs it from the token on every login, adding and removing
membership, so tier membership is managed in Keycloak -- with the exception of
the [administrators](#administrators). A user needs to log in again to pick up
a new role -- and to lose one; see [Group changes](#group-changes). A
namespace without groups, or groups for an unlisted namespace, stops the wiki
from starting with a message naming the mistake.

### Files

Uploaded files all live in MediaWiki's `File:` namespace, which is not a tier.
A file belongs to a tier through its **name**: `File:Management-Plan.pdf` is in
the Management tier and readable only by its groups. Upload it as
`Management-Plan.pdf`, and embed it as `[[File:Management-Plan.pdf]]`. The
prefix is matched case-insensitively. A name typed as `Management:Plan.pdf`
lands in the same place, since MediaWiki turns the colon into a hyphen; the
upload form then asks once to confirm the changed name.

A file without a tier prefix is readable by **every logged-in user**, no
matter on which page it is embedded. A tier file embedded on a page outside
its tier is shown as a missing file there.

Users outside a tier can neither upload a file with its prefix nor overwrite,
move or delete one.

### What is protected

[Extension:Lockdown](https://www.mediawiki.org/wiki/Extension:Lockdown) refuses
every action on a tier page — view, raw, history, edit, move, diff, old
revisions — to users outside its groups, and removes the namespace from their
search. The add-on applies the same to tier files and closes the paths
Lockdown leaves open, per viewer:

| Path | Handling |
|---|---|
| Tier files: file page, history, talk page, upload, re-upload, move, delete | Refused like a tier page |
| Tier files: original, thumbnails (`img_auth.php`, `thumb.php`, `Special:FilePath`), REST file endpoints | Refused; direct `/images/` access is denied for every file |
| Full-text search, UI and API | Tier namespaces and tier files hidden; hidden namespaces are dropped from the ones a search asks for, and a search in hidden ones only is refused |
| Action API prop modules (`prop=revisions`, `imageinfo`, `info`, …) | Pages and files from hidden tiers reduced to their title, so edit summaries, upload comments, file URLs and metadata stay hidden |
| `Special:Export`, API `export` | Restricted to sysops and the `all-tiers` group |
| `Special:RecentChanges`, `Special:Watchlist`, their feeds, `Special:NewPages`, `Special:Contributions`, `Special:Log` | Entries from hidden tiers filtered out, including edit summaries and upload comments |
| `Special:ListFiles`, `NewFiles`, `MIMESearch`, `ListDuplicatedFiles`, `FileDuplicateSearch`, `UnusedFiles`, `UncategorizedFiles` | Refused to users who cannot read every tier: these list all files with their upload comments and offer no filter |
| Action API list modules (`recentchanges`, `allpages`, `allimages`, `logevents`, `usercontribs`, `allrevisions`, …) | Entries from hidden tiers removed from the result |
| Lookups by content: the duplicate warnings on upload (UI and API), `prop=duplicatefiles` | Tier files left out, so uploading a copy of a document does not reveal whether, or under which name, it exists in a hidden tier; this covers deleted tier files too |
| Transclusion (`{{:Internal:Page}}`, `msgnw:`, `raw:`, `expandtemplates`, `parse`) and embedded tier files | Only allowed from a page in the same tier, so no rendered or cached page outside a tier carries its content; refused for a viewer outside the tier even when the parse names a tier page as context |

The break-glass admin is a member of the `all-tiers` group and passes every
tier.

`tests/run.sh` checks all of this automatically (see [Testing](#testing)).

### What is not protected

Lockdown is an access layer on top of a wiki built for openness, not a vault.
These still reveal that a page or file exists, and its title:

- red and blue link colours, `#ifexist`, `Special:WhatLinksHere`,
  `Special:AllPages`, category listings, `Special:WantedFiles`, title
  suggestions while typing;
- the title of a tier page or file in a link from a page the user can read.

Files without a tier prefix are not protected beyond the login, see
[Files](#files). Members of `MW_SYSOP_GROUPS` can reach every tier, see
[Administrators](#administrators).

The rule of thumb: **the wiki holds knowledge with visibility levels, not
secrets.** Keep credentials, personal data that needs protection, and anything
whose title alone is sensitive out of it. To keep files out of the wiki
altogether, set `MW_ENABLE_UPLOADS=false`.

### Administrators

Members of `MW_SYSOP_GROUPS` become sysop, bureaucrat and interface-admin in
the wiki. That makes them able to **read every tier**, whatever
`MW_NAMESPACE_READ_GROUPS` says:

- a bureaucrat can add any user, including themselves, to any tier group at
  `Special:UserRights`;
- a sysop may use `Special:Export`;
- an interface-admin can edit site-wide JavaScript, which runs in every
  user's browser.

A bureaucrat's change at `Special:UserRights` lasts until that user's next
login, when the group sync sets the tier groups from Keycloak again. List
only groups in `MW_SYSOP_GROUPS` whose members may see everything; the
default, `papaia-admin`, is in every tier anyway.

### Through the MCP server

The MCP server holds no access rules of its own. Each user signs in to it once
through the wiki (Keycloak login, then a consent page listing what the MCP
server may do), and every tool call runs as that wiki user with the user's
own groups. The tiers, the filters above and Lockdown therefore apply to
LibreChat exactly as to the browser, and every edit made through LibreChat is
recorded under the user's name.

What the MCP server may do is the grant list in `MEDIAWIKI_MCP_GRANTS`. The
default covers reading, editing, creating and moving pages and uploading
files; add `delete` and `protect` to allow deleting and protecting pages, and
`highvolume` for higher API limits. A grant never adds a right the user does
not have. Changing the list asks every user to consent again. Each user can
review and revoke the consent at `Special:OAuthManageMyGrants`.

### Group changes

Wiki groups follow Keycloak at the moment of a **browser login to the wiki**,
not continuously. Two consequences:

- **Granting a role** takes effect when the user next logs in to the wiki in
  the browser. Signing in to the MCP server from LibreChat counts as such a
  login only when the browser has no wiki session yet.
- **Revoking a role** takes effect at the same point. Until then the user keeps
  the tier -- in the browser while the wiki session lasts, and in LibreChat
  while the MCP connection lasts: the MCP server refreshes its wiki token on
  its own for up to 30 days without a new login.

### Offboarding

Removing a role in Keycloak alone does **not** end tier access on the MCP
path for up to 30 days (see above). To cut it at once:

1. **Keycloak:** remove the role (or disable the user).
2. **Wiki:** remove the user from the tier group at `Special:UserRights`, as a
   sysop. This takes effect on the next request, in the browser and through
   the MCP server alike.

In that order, a login in between cannot add the group back. The user's MCP
connection itself stays valid for the main namespace until it expires (30
days without use) or the user revokes it at `Special:OAuthManageMyGrants`. An
administrator can only end it by disabling the `papaia-mcp` consumer as a
whole (`Special:OAuthManageConsumers`), which signs every user out of the MCP
server until it is re-enabled.

---

## Mail

```bash
MW_SMTP_HOST=mail-relay:25        # host:port, no authentication; empty = off
MW_EMAIL_FROM=wiki@example.com    # sender and emergency contact
```

With a relay set the wiki sends watchlist and user-talk notifications. Users
cannot mail each other through the wiki. The relay must accept mail from this
host without authentication, as a local relay does; the papaia core ships none.

---

## Break-glass admin

`MW_ADMIN_USER` (default `Admin`) is a local account with sysop and bureaucrat
rights that passes every tier. Its password is `MW_ADMIN_PASSWORD` from the
`.env`, re-applied on every start.

Local login is off in the browser. To use the account:

```bash
# Browser: show the password form for the duration of the emergency
#   MW_ENABLE_LOCAL_LOGIN=true in the .env, then
papaia-ctl addon start mediawiki
#   ... log in at <MW_SERVER>/index.php/Special:UserLogin ...
#   MW_ENABLE_LOCAL_LOGIN=false, then start again

# Command line: no login needed
docker compose -p mediawiki -f addons/mediawiki/docker-compose.yml exec mediawiki \
  php maintenance/run.php createAndPromote --force --sysop --bureaucrat <WikiUser>
```

---

## Manual installation

If you are not using `papaia-ctl`, follow these steps.

### 1. Configure environment

```bash
cp .env.example .env
```

Fill in every `CHANGE_ME`, replace every `GENERATE_SECRET` with a random value
(e.g. `openssl rand -hex 24`), and set `KC_MEDIAWIKI_CLIENT_SECRET` after
step 2.

| Variable | Description |
|---|---|
| `OIDC_ISSUER` | Keycloak issuer URL, browser-facing (same value as the core `OIDC_ISSUER`) |
| `PAPAIA_CONFIG_DIR` | papaia config directory; its `certs/local-ca.crt` is trusted for the OIDC calls, and `certs/local-ca.key` issues the certificate of the https port |
| `MW_SERVER` | URL of the wiki that the browser and LibreChat both reach over https, see [Before you start](#before-you-start-one-url-for-the-browser-and-librechat) |
| `MW_SECRET_KEY`, `MW_UPGRADE_KEY` | Random values |
| `MW_DB_PASSWORD` | Random value |
| `MW_ADMIN_PASSWORD` | Break-glass admin password |
| `KC_MEDIAWIKI_CLIENT_SECRET` | Secret of the `mediawiki` Keycloak client |
| `MEDIAWIKI_MCP_SIGNING_KEY` | Random value, at least 32 characters |
| `MEDIAWIKI_MCP_ALLOWED_REDIRECTS` | OAuth callbacks of the MCP clients, e.g. LibreChat's |

With an external identity provider whose certificate is publicly trusted, set
`MW_OIDC_CA_FILE` to an empty value in a compose override, so the system CA
bundle is used.

### 2. Register Keycloak clients

See [Keycloak](#keycloak) above.

### 3. Wire the network (Seam 1)

Attach `nginx-proxy-manager` to the add-on network:

```yaml
# papaia-config/overrides/docker-compose.mediawiki.override.yml
services:
  nginx-proxy-manager:
    networks:
      - ${PAPAIA_PROJECT:-papaia}-mediawiki-net
networks:
  ${PAPAIA_PROJECT:-papaia}-mediawiki-net:
    external: true
```

Start the add-on first (it creates the network), then restart the core with
the override:

```bash
docker compose -p mediawiki -f addons/mediawiki/docker-compose.yml \
  --env-file papaia/src/.env --env-file addons/mediawiki/.env up -d
docker compose -f papaia/src/docker-compose.yml \
  -f papaia-config/overrides/docker-compose.mediawiki.override.yml up -d
```

### 4. Add the MCP server to LibreChat (Seam 3)

See [LibreChat](#librechat) below.

### 5. Configure the reverse proxy (Seam 5, optional)

See [Reverse proxy](#reverse-proxy) below.

---

## Reverse proxy

For a real host name, put the wiki behind a TLS-terminating reverse proxy.
`integration/infra/nginx/mediawiki.conf` is a reference for the Nginx Proxy
Manager: a proxy host for `mediawiki:80` (plain http on the add-on network),
with `client_max_body_size` raised for uploads and response buffering off for
`/mcp`. Set `MW_SERVER` to the proxied URL; the MCP server follows at
`<MW_SERVER>/mcp`.

The wiki's own host port speaks https with a certificate from the papaia
local CA (or a self-signed one), which suits a single host and internal use.
Production use with users outside that trust belongs behind the proxy; the
host port can then be bound to `127.0.0.1` (`HOST_IP`) or left unused.

---

## LibreChat

LibreChat reaches the MCP server at `<MW_SERVER>/mcp`, as the user's browser
does during sign-in. The add-on's fragment
(`integration/ai/librechat/librechat.yaml`, merged on install) registers it
at `https://host.docker.internal:8020/mcp`, the default. With a different
`MW_SERVER`, replace the URL in the customer overlay
`<papaia-config>/overlay/ai/librechat/librechat.yaml`, which is merged last:

```yaml
mcpServers:
  MediaWiki:
    url: https://wiki.example.org/mcp     # = <MW_SERVER>/mcp

mcpSettings:
  allowedDomains:
    - "wiki.example.org"                  # host of that URL
```

The MCP server accepts LibreChat's sign-in callback,
`<DOMAIN_SERVER>/api/mcp/MediaWiki/oauth/callback`, for the core default
`DOMAIN_SERVER=http://host.docker.internal:8000` and for any
`http://localhost` port. Any other `DOMAIN_SERVER` goes into the add-on `.env`:

```bash
MEDIAWIKI_MCP_ALLOWED_REDIRECTS=https://chat.example.org/api/mcp/MediaWiki/oauth/callback
```

Requirements, and the symptom when one is missing:

| Requirement | Symptom (LibreChat log, `logs/error-*.log`) |
|---|---|
| The LibreChat `url` is `<MW_SERVER>/mcp` | `No OAuth metadata discovered from https://...`, then *"The MCP server must be authenticated"* |
| The URL is https, and LibreChat trusts its certificate (papaia local CA via `NODE_EXTRA_CA_CERTS`, or a public CA) | connection or certificate errors on `/.well-known/oauth-protected-resource` |
| The LibreChat container reaches the URL -- not `localhost`, which is the container itself | `ECONNREFUSED`; the MCP container logs a warning for a `localhost` URL at start |
| The host of the URL is in `mcpSettings.allowedDomains` | `MCPDomainNotAllowedError: Domain "https://..." is not allowed`; the server is reported unreachable |
| LibreChat's callback is allowed | `[MCPOAuth] Failed to initiate OAuth flow` with `a redirect_uri is not permitted`; connecting fails before any sign-in page opens |

Each user then connects once: in the chat, select the MediaWiki MCP server,
follow the sign-in link (wiki login via Keycloak, then the consent page), and
the tools are available. The connection lasts until the user revokes it or
does not use it for 30 days.

Other MCP clients sign in the same way. The MCP server trusts loopback
callbacks and those of a few hosted clients by default (see its
documentation); any other client's callback goes into
`MEDIAWIKI_MCP_ALLOWED_REDIRECTS`.

---

## Site-specific settings

Every `*.php` file in `settings.d/` (or in `MW_SETTINGS_DIR`) is loaded after
the add-on's own settings — a logo, a skin default, another bundled extension.

---

## Testing

```bash
tests/run.sh            # [--port=18020] [--keep]
```

Starts the wiki and its database from this `docker-compose.yml` under a
throw-away project (local login on, no Keycloak needed), seeds a tier member
and a user outside the tiers, and runs `tests/tier_check.py`: the member
writes a tier page, a tier file and a file talk page with random markers in
content, summaries, upload comments and file bytes, and the outsider then
requests every path listed under [What is protected](#what-is-protected) --
browser, feeds, logs, search, Action API, REST API, `img_auth.php`,
thumbnails -- and tries to upload, overwrite, edit and move into the tier.
No response may carry a marker. The member and the break-glass admin are
positive controls. Everything is removed afterwards.

The `mediawiki-image` workflow runs it on every change to the add-on, before
an image is published. Run it after changing the tier hooks in
`runtime/LocalSettings.php` and after bumping MediaWiki or an extension;
Dependabot proposes base-image bumps, the extension commits in
`image/Dockerfile` are updated by hand.

---

## Stopping and removing

```bash
# Stop containers (config bundle and volumes kept)
papaia-ctl addon stop mediawiki

# Stop and remove containers and the network (volumes kept)
papaia-ctl addon stop mediawiki --clean-up

# Remove integration only (config bundle kept, containers untouched)
papaia-ctl addon remove mediawiki

# Uninstall: config bundle, deployment entry and containers removed, volumes kept
papaia-ctl addon uninstall mediawiki

# Uninstall and delete the volumes — wiki content, uploads and MCP sign-ins are gone
papaia-ctl addon uninstall mediawiki --clean-up
```

`papaia-ctl backup` captures the volumes and the config bundle with the
generated secrets; a restore brings back pages, users, groups, uploads, the
OAuth signing keys and the MCP sign-ins.

---

## Security notes

- **Network isolation:** the database and the MCP server are not published;
  the MCP server is reachable only through the wiki's `/mcp` path. Only
  `nginx` joins the add-on network.
- **TLS:** the published host port speaks https only; plain http is served
  only on the add-on network, to the reverse proxy. For users outside the
  papaia local CA's trust, publish the wiki through the reverse proxy.
- **Per-user identity:** the MCP server acts as the signed-in user, never as
  a shared account. The wiki's own permission checks decide every call, and
  edits are attributed to the user. Shared bot passwords are switched off.
- **Least privilege for the MCP server:** it acts with the grants in
  `MEDIAWIKI_MCP_GRANTS` only; deleting and protecting pages are off by
  default.
- **Per-service environment:** no container loads the `.env` wholesale. The
  MCP server never sees the database or admin password; the wiki never sees
  the MCP signing key.
- **OAuth consumer:** only sysops can create or approve consumers. The
  add-on's consumer is confidential (client secret), uses PKCE through the MCP
  server, and accepts exactly one callback URL.
- **Sign-in callbacks:** the MCP server hands an authorization only to
  loopback addresses, a few vendor clients and the callbacks in
  `MEDIAWIKI_MCP_ALLOWED_REDIRECTS`.
- **Administrators** (`MW_SYSOP_GROUPS`) can reach every tier; see
  [Administrators](#administrators).
- **Account linking by e-mail** is off (`MW_OIDC_MIGRATE_BY_EMAIL=false`), so
  an identity-provider account cannot take over a wiki account by claiming its
  address.
- **Uploads:** images and PDF only by default. SVG is excluded because it can
  carry script. Files follow the tiers through their name only; see
  [Files](#files).

---

## Known limitations

- **Keycloak client registration** is manual; see [Keycloak](#keycloak).
- **LibreChat URL** of the MCP server is set in the overlay for any
  `MW_SERVER` other than the default; see [LibreChat](#librechat).
- **Group changes** reach the MCP server only with the next browser login to
  the wiki; see [Group changes](#group-changes) and [Offboarding](#offboarding).
- **File listings** (`Special:ListFiles` and similar) are available only to
  users who can read every tier.
- **Search** uses MediaWiki's built-in MariaDB full-text search. Tier members
  tick the tier namespace under *Advanced search* once to include it.
