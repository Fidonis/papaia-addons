# papaia-addon-mediawiki

MediaWiki knowledge base add-on for [papaia](https://github.com/Fidonis/papaia).

Adds a MediaWiki wiki with Keycloak login and group sync, namespace-level
access tiers, and the [MediaWiki MCP Server](https://github.com/ProfessionalWiki/MediaWiki-MCP-Server),
through which LibreChat works in the wiki as the signed-in user -- within the
same tiers the browser enforces.

**Requires papaia ≥ 1.3.0** (`addon_api` 2).

---

## Services

| Service | Port | Description |
|---|---|---|
| `mediawiki` | `8020` (configurable) | MediaWiki 1.43 (LTS) with PluggableAuth, OpenIDConnect, Lockdown, OAuth, VisualEditor, Cite, ParserFunctions; also serves the MCP server under `/mcp` |
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

### The image

`image/` holds the Dockerfile of the wiki image: the official `mediawiki:1.43`
image plus PluggableAuth, OpenIDConnect, Lockdown and OAuth, each pinned to a
commit, and an environment-driven `LocalSettings.php`. When the image named in
`MEDIAWIKI_IMAGE` cannot be pulled, Docker Compose builds it from `image/` on
the first start, which needs internet access and takes a few minutes.

---

## Installation via papaia-ctl

```bash
# 1. Clone this add-on into your workspace
git clone https://github.com/Fidonis/papaia-addon-mediawiki addons/mediawiki

# 2. Install — seeds .env in the config bundle, registers in deployment.yaml, renders config
papaia-ctl addon install mediawiki --path=addons/mediawiki

# 3. Follow the Keycloak checklist printed by install (see "Keycloak" below)

# 4. Set MW_SERVER, MEDIAWIKI_MCP_PUBLIC_URL, MEDIAWIKI_MCP_ALLOWED_REDIRECTS
#    and KC_MEDIAWIKI_CLIENT_SECRET in <papaia-config>/addons/mediawiki/.env

# 5. Point LibreChat at the MCP server (see "LibreChat" below)

# 6. Start the add-on, then the core so nginx joins its network and
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
(`image/entrypoint.sh`), and every step is safe to repeat:

1. **Empty database:** `install.php` creates the schema and the break-glass
   admin. The add-on's own `LocalSettings.php` is used afterwards; the one
   `install.php` writes is discarded.
2. **Once per container:** `update.php` creates extension tables and migrates
   the schema after an image upgrade. A new image means a new container, so
   upgrades are covered without paying for it on every restart.
3. **Every start:** the break-glass admin gets its groups and the password
   from `MW_ADMIN_PASSWORD`; rotating it takes effect after a restart.
4. **OAuth:** the key pair that signs access tokens is generated once and kept
   on the `mediawiki-oauth` volume. The MCP server's OAuth 2.0 consumer
   (`papaia-mcp`, owned by the break-glass admin) is created and approved for
   the callback `<MEDIAWIKI_MCP_PUBLIC_URL>/oauth/callback`; later starts find
   it again. A changed public URL creates a new consumer and disables the old
   one. The key and secret go to the `mediawiki-mcp-credentials` volume,
   which the MCP server reads at start.

---

## Keycloak

Import one client into the `papaia` realm:

| File | Where |
|---|---|
| `integration/infra/keycloak/mediawiki.json` | Clients → Import client |

After the import:

- **Root URL of `mediawiki`:** set it to `MW_SERVER`. The redirect URIs are
  relative to it (`/index.php/Special:PluggableAuthLogin`); the imported value
  is the default `http://host.docker.internal:8020`.
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

# Members become sysop + bureaucrat + interface-admin
MW_SYSOP_GROUPS=papaia-admin
```

Every group named here becomes a wiki group of the same name. PluggableAuth
syncs it from the token on every login, adding and removing membership, so
access is managed in Keycloak only. A user needs to log in again to pick up a
new role -- and to lose one; see [Group changes](#group-changes). A namespace without groups, or groups for an unlisted namespace,
stops the wiki from starting with a message naming the mistake.

### What is protected

[Extension:Lockdown](https://www.mediawiki.org/wiki/Extension:Lockdown) refuses
every action on a tier page — view, raw, history, edit, move, diff, old
revisions — to users outside its groups, and removes the namespace from their
search. The add-on closes the paths Lockdown leaves open, per viewer:

| Path | Handling |
|---|---|
| Full-text search, UI and API | Tier namespaces hidden; hidden ones are dropped from the namespaces a search asks for, and a search in hidden ones only is refused |
| Action API prop modules (`prop=revisions`, `info`, …) | Pages from hidden tiers reduced to their title, so edit summaries and other revision metadata stay hidden |
| `Special:Export`, API `export` | Restricted to sysops and the `all-tiers` group |
| `Special:RecentChanges`, `Special:Watchlist`, their feeds, `Special:NewPages`, `Special:Contributions` | Entries from hidden tiers filtered out, including edit summaries |
| Action API list modules (`recentchanges`, `allpages`, `logevents`, `usercontribs`, `allrevisions`, …) | Entries from hidden tiers removed from the result |
| Transclusion (`{{:Internal:Page}}`, `msgnw:`, `raw:`, `expandtemplates`, `parse`) | Only allowed from a page in the same tier, so no rendered or cached page outside a tier carries its content; refused for a viewer outside the tier even when the parse names a tier page as context |
| Uploaded files | Served through `img_auth.php` with a permission check; direct `/images/` access is denied |

The break-glass admin is a member of the `all-tiers` group and passes every
tier.

### What is not protected

Lockdown is an access layer on top of a wiki built for openness, not a vault.
These still reveal that a page exists, and its title:

- red and blue link colours, `#ifexist`, `Special:WhatLinksHere`,
  `Special:AllPages`, category listings, `Special:Log`;
- the title of a tier page in a link from a page the user can read.

The rule of thumb: **the wiki holds knowledge with visibility levels, not
secrets.** Keep credentials, personal data that needs protection, and anything
whose title alone is sensitive out of it.

### Through the MCP server

The MCP server holds no access rules of its own. Each user signs in to it once
through the wiki (Keycloak login, then a consent page listing what the MCP
server may do), and every tool call runs as that wiki user with the user's
own groups. The tiers, the filters above and Lockdown therefore apply to
LibreChat exactly as to the browser, and every edit made through LibreChat is
recorded under the user's name.

The consent covers reading, editing, creating, moving, uploading, deleting
and protecting pages -- as far as the user's own rights reach; it never adds a
right. Each user can review and revoke it at
`Special:OAuthManageMyGrants`.

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

To cut tier access at once, remove the role in Keycloak first, then remove
the user from the tier group in the wiki (`Special:UserRights`, as a sysop).
That takes effect on the next request, in the browser and through the MCP
server alike; the next login would have done the same. In that order, a login
in between cannot add the group back.

The MCP connection itself ends when the user revokes it at
`Special:OAuthManageMyGrants`. An administrator can only disable the
`papaia-mcp` consumer as a whole (`Special:OAuthManageConsumers`), which signs
every user out of the MCP server until it is re-enabled.

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
| `PAPAIA_CONFIG_DIR` | papaia config directory; its `certs/local-ca.crt` is trusted for the OIDC calls |
| `MW_SERVER` | Browser-facing URL of the wiki, e.g. `https://wiki.example.com` |
| `MW_SECRET_KEY`, `MW_UPGRADE_KEY` | Random values |
| `MW_DB_PASSWORD` | Random value |
| `MW_ADMIN_PASSWORD` | Break-glass admin password |
| `KC_MEDIAWIKI_CLIENT_SECRET` | Secret of the `mediawiki` Keycloak client |
| `MEDIAWIKI_MCP_PUBLIC_URL` | `<MW_SERVER>/mcp`; https unless the host is `localhost` |
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

`integration/infra/nginx/mediawiki.conf` is a reference for the Nginx Proxy
Manager: a proxy host for `mediawiki:80` on the add-on network, with
`client_max_body_size` raised for uploads and response buffering off for
`/mcp`. Set `MW_SERVER` to the proxied URL and `MEDIAWIKI_MCP_PUBLIC_URL` to
the same URL plus `/mcp`.

---

## LibreChat

LibreChat reaches the MCP server at its public URL, as the user's browser does
during sign-in, so the address differs per deployment. The add-on's fragment
(`integration/ai/librechat/librechat.yaml`, merged on install) registers the
server with a placeholder; replace it in the customer overlay
`<papaia-config>/overlay/ai/librechat/librechat.yaml`, which is merged last:

```yaml
mcpServers:
  MediaWiki:
    url: https://wiki.example.org/mcp     # = MEDIAWIKI_MCP_PUBLIC_URL

mcpSettings:
  allowedDomains:
    - "wiki.example.org"                  # host of that URL
```

Then let the MCP server accept LibreChat's sign-in callback, in the add-on
`.env`:

```bash
# <DOMAIN_SERVER of LibreChat>/api/mcp/MediaWiki/oauth/callback
MEDIAWIKI_MCP_ALLOWED_REDIRECTS=https://chat.example.org/api/mcp/MediaWiki/oauth/callback
```

Requirements:

- `MEDIAWIKI_MCP_PUBLIC_URL` must be **https**, and LibreChat must trust its
  certificate. With the papaia local CA, LibreChat already does
  (`NODE_EXTRA_CA_CERTS`). A plain-http wiki on `localhost` serves MCP
  clients on the Docker host only, not LibreChat.
- The LibreChat container must reach that URL, as it fetches the OAuth
  metadata and exchanges tokens there.

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
- **Per-user identity:** the MCP server acts as the signed-in user, never as
  a shared account. The wiki's own permission checks decide every call, and
  edits are attributed to the user. Shared bot passwords are switched off.
- **Per-service environment:** no container loads the `.env` wholesale. The
  MCP server never sees the database or admin password; the wiki never sees
  the MCP signing key.
- **OAuth consumer:** only sysops can create or approve consumers. The
  add-on's consumer is confidential (client secret), uses PKCE through the MCP
  server, and accepts exactly one callback URL.
- **Sign-in callbacks:** the MCP server hands an authorization only to
  loopback addresses, a few vendor clients and the callbacks in
  `MEDIAWIKI_MCP_ALLOWED_REDIRECTS`.
- **Account linking by e-mail** is off (`MW_OIDC_MIGRATE_BY_EMAIL=false`), so
  an identity-provider account cannot take over a wiki account by claiming its
  address.
- **Uploads:** images and PDF only by default. SVG is excluded because it can
  carry script.

---

## Known limitations

- **Keycloak client registration** is manual; see [Keycloak](#keycloak).
- **LibreChat URL** of the MCP server is set per deployment in the overlay;
  see [LibreChat](#librechat).
- **Group changes** reach the MCP server only with the next browser login to
  the wiki; see [Group changes](#group-changes).
- **Search** uses MediaWiki's built-in MariaDB full-text search. Tier members
  tick the tier namespace under *Advanced search* once to include it.
