# papaia-addon-mediawiki

MediaWiki knowledge base add-on for [papaia](https://github.com/Fidonis/papaia).

Adds a MediaWiki wiki with Keycloak login and group sync, namespace-level
access tiers, and an OIDC/RBAC-secured MCP server that gives LibreChat the
same tiers the browser enforces.

**Requires papaia ≥ 1.3.0** (`addon_api` 2).

---

## Services

| Service | Port | Description |
|---|---|---|
| `mediawiki` | `8020` (configurable) | MediaWiki 1.43 (LTS) with PluggableAuth, OpenIDConnect, Lockdown, VisualEditor, Cite, ParserFunctions |
| `mediawiki-db` | internal | MariaDB 11.8 |
| `mediawiki-mcp` | internal | [mediawiki-mcp-rbac](https://github.com/Fidonis/mediawiki-mcp-rbac) MCP server for LibreChat |
| `mediawiki-mcp-acl-seed` | — | One-shot: writes the MCP server's initial grants from the access tiers |

All services run on the add-on's own bridge network,
`${PAPAIA_PROJECT:-papaia}-mediawiki-net` — `papaia-mediawiki-net` on a default
single-stack host, or `papaia-<env>-mediawiki-net` when several papAIa
deployments share a host. Only the wiki is published on a host port; LibreChat
reaches the MCP server over this network.

| Volume | Content |
|---|---|
| `mediawiki-dbdata` | Pages, revisions, users, groups |
| `mediawiki-images` | Uploaded files |
| `mediawiki-mcp-data` | MCP access grants and login-proxy state |

### The image

`image/` holds the Dockerfile of the wiki image: the official `mediawiki:1.43`
image plus PluggableAuth, OpenIDConnect and Lockdown, each pinned to a commit,
and an environment-driven `LocalSettings.php`. When the image named in
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

# 4. Set MW_SERVER and the two KC_*_CLIENT_SECRET values in
#    <papaia-config>/addons/mediawiki/.env

# 5. Start the add-on, then the core so nginx and librechat join its network
papaia-ctl addon start mediawiki
papaia-ctl start
```

Database password, wiki keys, the break-glass admin password and the MCP bot
password are generated on install. Nothing else has to be copied by hand: the
wiki creates the bot account and sets its bot password from the same `.env`
the MCP server reads.

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
   from `MW_ADMIN_PASSWORD`, and the MCP bot account gets the bot password from
   `WIKI_BOT_PASSWORD`. Rotating either value takes effect after a restart.

---

## Keycloak

Import both clients into the `papaia` realm and add the audience mapper to the
existing `librechat` client:

| File | Where |
|---|---|
| `integration/infra/keycloak/mediawiki.json` | Clients → Import client |
| `integration/infra/keycloak/mcp-mediawiki.json` | Clients → Import client |
| `integration/infra/keycloak/librechat-audience-mapper.json` | Clients → librechat → Client scopes → Dedicated → Add mapper → By configuration |

After the import:

- **Root URL of `mediawiki`:** set it to `MW_SERVER`. The redirect URIs are
  relative to it (`/index.php/Special:PluggableAuthLogin`); the imported value
  is the default `http://host.docker.internal:8020`.
- **Secrets:** copy the client secrets of `mediawiki` and `mcp-mediawiki`
  (Credentials tab) into `KC_MEDIAWIKI_CLIENT_SECRET` and
  `KC_MCP_MEDIAWIKI_CLIENT_SECRET`.

The `groups` protocol mapper on the `mediawiki` client puts the user's realm
roles into the `groups` claim, the same shape as the paperless add-on. The
token LibreChat forwards to the MCP server comes from the core `librechat`
client, which carries the same realm roles under `roles` — hence
`MEDIAWIKI_MCP_GROUPS_CLAIM=roles`. Both claims hold the same names, so a
user's tier is the same in the browser and in LibreChat.

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
new role. A namespace without groups, or groups for an unlisted namespace,
stops the wiki from starting with a message naming the mistake.

### What is protected

[Extension:Lockdown](https://www.mediawiki.org/wiki/Extension:Lockdown) refuses
every action on a tier page — view, raw, history, edit, move, diff, old
revisions — to users outside its groups, and removes the namespace from their
search. The add-on closes the paths Lockdown leaves open, per viewer:

| Path | Handling |
|---|---|
| Full-text search, UI and API | Tier namespaces hidden; explicitly searching one is refused |
| `Special:Export`, API `export` | Restricted to sysops and the `all-tiers` group |
| `Special:RecentChanges`, `Special:Watchlist`, their feeds, `Special:NewPages`, `Special:Contributions` | Entries from hidden tiers filtered out, including edit summaries |
| Action API list modules (`recentchanges`, `allpages`, `logevents`, `usercontribs`, `allrevisions`, …) | Entries from hidden tiers removed from the result |
| Transclusion (`{{:Internal:Page}}`, `msgnw:`, `raw:`, `expandtemplates`, `parse`) | Only allowed from a page in the same tier, so no rendered or cached page outside a tier carries its content |
| Uploaded files | Served through `img_auth.php` with a permission check; direct `/images/` access is denied |

The break-glass admin and the MCP bot are members of the `all-tiers` group and
pass every tier.

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

The MCP server does not ask the wiki who may read what: it calls the Action
API with one bot account that passes every tier, and enforces its own
page-prefix ACL keyed to the caller's groups claim. On the first start the
`mediawiki-mcp-acl-seed` container derives that ACL from the tiers above:

- `rw` on `Name:`, `Name talk:` and `Name_talk:` for each group of a tier;
- `rw` on the main namespace (`*`) for `MEDIAWIKI_MCP_MAIN_GROUPS`, by default
  every group named in a tier.

A caller outside the tier gets an explicit `forbidden` error, not an empty
result. A caller in none of the groups is refused by the MCP server outright.
Members of `MEDIAWIKI_MCP_ADMIN_GROUP` (default `papaia-admin`) bypass the ACL
and can change it at runtime with the `grant_access`, `revoke_access` and
`list_acl` tools.

`MEDIAWIKI_MCP_ACL_SEED` controls how the ACL follows later changes of the
tiers:

| Value | Effect |
|---|---|
| `if-empty` (default) | Seed an empty ACL only; later changes via `grant_access` are kept |
| `sync` | Add the derived grants on every start; never revokes |
| `off` | Manage the ACL with the admin tools only |

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
(e.g. `openssl rand -hex 24`), and set the two `KC_*_CLIENT_SECRET` values after
step 2.

| Variable | Description |
|---|---|
| `OIDC_ISSUER` | Keycloak issuer URL, browser-facing (same value as the core `OIDC_ISSUER`) |
| `PAPAIA_CONFIG_DIR` | papaia config directory; its `certs/local-ca.crt` is trusted for the OIDC calls |
| `MW_SERVER` | Browser-facing URL of the wiki, e.g. `https://wiki.example.com` |
| `MW_SECRET_KEY`, `MW_UPGRADE_KEY` | Random values |
| `MW_DB_PASSWORD` | Random value |
| `MW_ADMIN_PASSWORD` | Break-glass admin password |
| `WIKI_BOT_PASSWORD` | MCP bot password, at least 16 characters |
| `KC_MEDIAWIKI_CLIENT_SECRET` | Secret of the `mediawiki` Keycloak client |
| `KC_MCP_MEDIAWIKI_CLIENT_SECRET` | Secret of the `mcp-mediawiki` Keycloak client |

With an external identity provider whose certificate is publicly trusted, set
`MW_OIDC_CA_FILE` and `SSL_CERT_FILE` to an empty value in a compose override,
so the system CA bundle is used.

### 2. Register Keycloak clients

See [Keycloak](#keycloak) above.

### 3. Wire the network (Seam 1)

Attach `nginx-proxy-manager` and `librechat` to the add-on network:

```yaml
# papaia-config/overrides/docker-compose.mediawiki.override.yml
services:
  librechat:
    networks:
      - ${PAPAIA_PROJECT:-papaia}-mediawiki-net
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

Merge `integration/ai/librechat/librechat.yaml` into your effective
`librechat.yaml` (or `papaia-config/overlay/ai/librechat/librechat.yaml`):

```yaml
mcpServers:
  MediaWiki:
    title: 'MediaWiki'
    description: 'OIDC-secured knowledge base with access tiers via MediaWiki.'
    type: streamable-http
    url: http://mediawiki-mcp:8000/mcp
    headers:
      Authorization: "Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}"
    startup: false

mcpSettings:
  allowedDomains:
    - "http://mediawiki-mcp:8000"
    # ... keep any existing entries
```

### 5. Configure the reverse proxy (Seam 5, optional)

`integration/infra/nginx/mediawiki.conf` is a reference for the Nginx Proxy
Manager: a proxy host for `mediawiki:80` on the add-on network, with
`client_max_body_size` raised for uploads. Set `MW_SERVER` to the proxied URL.

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

# Uninstall and delete the volumes — wiki content, uploads and MCP grants are gone
papaia-ctl addon uninstall mediawiki --clean-up
```

`papaia-ctl backup` captures all three volumes and the config bundle with the
generated secrets; a restore brings back pages, users, groups, uploads and the
MCP grants.

---

## Security notes

- **Network isolation:** the database and the MCP server are not published.
  Only `nginx` and `librechat` join the add-on network.
- **Per-service environment:** no container loads the `.env` wholesale. The
  MCP server never sees the database or admin password; the wiki never sees
  the MCP client secret.
- **Bot password scope:** the MCP bot logs in with a bot password limited to
  reading, editing, moving and deleting pages. It cannot change user rights or
  site configuration.
- **Account linking by e-mail** is off (`MW_OIDC_MIGRATE_BY_EMAIL=false`), so
  an identity-provider account cannot take over a wiki account by claiming its
  address.
- **Uploads:** images and PDF only by default. SVG is excluded because it can
  carry script.

---

## Known limitations

- **Keycloak client registration** is manual; see [Keycloak](#keycloak).
- **Search** uses MediaWiki's built-in MariaDB full-text search. Tier members
  tick the tier namespace under *Advanced search* once to include it.
