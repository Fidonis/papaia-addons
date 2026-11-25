# papaia-addon-paperless

Paperless-ngx document management addon for [papaia](https://github.com/Fidonis/papaia).

Adds Paperless-ngx with full-text search and OCR, an OIDC-secured MCP server for
AI-assisted document access via LibreChat, and a Keycloak SSO login for Paperless users.

**Requires papaia ≥ 1.10.0** (`addon_api` 12).

---

## Services

| Service | Port | Description |
|---|---|---|
| `paperless` | none (loopback of its container) | Paperless-ngx web UI and API |
| `paperless-gate` | `8001`, internal (`paperless:8001`) | The only way to Paperless; passes a request only with a caller's secret, and a person only by a name the realm sync lists |
| `paperless-mcp` | internal | OIDC/RBAC-secured MCP server for LibreChat |
| `paperless-broker` | internal | Valkey task broker |
| `paperless-db` | internal | PostgreSQL database |
| `paperless-gotenberg` | internal | Office/email → PDF conversion |
| `paperless-tika` | internal | Text extraction |

No container of the add-on publishes a port, on any address. Paperless is reached
through the proxy in the browser and through `paperless-mcp` from LibreChat; see
[Paperless behind its gate](#paperless-behind-its-gate).

All services run on the add-on's own isolated Docker bridge network,
`${PAPAIA_PROJECT:-papaia}-paperless-net` — `papaia-paperless-net` on a default
single-stack host, or `papaia-<env>-paperless-net` when several papAIa deployments
share a host. `papaia-ctl` resolves the same value when it generates the Seam-1
override. `paperless-mcp` also joins `${PAPAIA_PROJECT:-papaia}-paperless-front`,
the only network LibreChat shares with the add-on.

## Components

The images this add-on runs. Each version is the default of its image variable; set the
variable in `<papaia-config>/addons/paperless/.env` to run another one.

<!-- components:start -->
| Component | Version | Role | Image variable |
|---|---|---|---|
| [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) | `3.5.1` | primary | `PAPERLESS_IMAGE` |
| [paperless-mcp-rbac](https://github.com/Fidonis/paperless-mcp-rbac) | `0.2.0-rc.2` | primary | `PAPERLESS_MCP_IMAGE` |
| [nginx](https://nginx.org) | `1.31.10-alpine` | sidecar | `PAPERLESS_GATE_IMAGE` |
| [Valkey](https://github.com/valkey-io/valkey) | `9.2.0-alpine` | sidecar | `PAPERLESS_REDIS_IMAGE` |
| [PostgreSQL](https://www.postgresql.org) | `18.7` | sidecar | `PAPERLESS_DB_IMAGE` |
| [Gotenberg](https://github.com/gotenberg/gotenberg) | `8.40.0` | sidecar | `PAPERLESS_GOTENBERG_IMAGE` |
| [Apache Tika](https://tika.apache.org) | `3.4.0.0` | sidecar | `PAPERLESS_TIKA_IMAGE` |
<!-- components:end -->

---

## Installation via papaia-ctl

```bash
# 1. Clone this addon into your workspace
git clone https://github.com/Fidonis/papaia-addon-paperless addons/paperless

# 2. Install — seeds .env in the config bundle, registers in deployment.yaml, renders config
papaia-ctl addon install paperless --path=addons/paperless

# 3. Follow the Keycloak checklist printed by install (see below)

# 4. Edit CHANGE_ME values in <papaia-config>/addons/paperless/.env

# 5. Start the addon
papaia-ctl addon start paperless
```

---

## Manual installation

If you are not using `papaia-ctl`, follow these steps manually.

### 1. Configure environment

Copy `.env.example` to `.env` in this directory:

```bash
cp .env.example .env
```

Edit `.env` and fill in all `CHANGE_ME` values:

| Variable | Description |
|---|---|
| `OIDC_ISSUER` | Keycloak issuer URL (same value as `AUTH_HOST` in your papaia setup) |
| `PAPERLESS_PUBLIC_URL` | Browser-facing URL of Paperless: its proxy host, a host name without a port (e.g. `https://docs.example.com`) |
| `PAPERLESS_DBPASS` | PostgreSQL password (generate randomly) |
| `PAPERLESS_SECRET_KEY` | Django secret key (generate ≥ 64 chars randomly) |
| `PAPERLESS_ADMIN_PASSWORD` | Bootstrap admin password (generate randomly) |
| `KC_PAPERLESS_CLIENT_SECRET` | Keycloak client secret for the `paperless` OIDC client |

Paperless accepts requests only for the host name of `PAPERLESS_PUBLIC_URL`,
`localhost` and the names in `PAPERLESS_ALLOWED_HOSTS` (default `paperless`,
the name under which `paperless-mcp` reaches it). If you set
`PAPERLESS_ALLOWED_HOSTS` in `.env`, keep `paperless` in the list.

### 2. Register Keycloak clients

In the Keycloak admin UI, create two clients in the `papaia` realm:

**Client `paperless`** (confidential, standard flow):
- Client ID: `paperless`
- Client secret: value of `KC_PAPERLESS_CLIENT_SECRET`
- Redirect URIs: `<PAPERLESS_PUBLIC_URL>/*`
- Protocol mapper: realm roles → `groups` claim (see `integration/infra/keycloak/paperless.json`)

**Client `mcp-paperless`** (bearer-only resource server, no flows):
- Client ID: `mcp-paperless`
- Client authentication on, no redirect URIs
- Standard flow, implicit flow, direct access grants and service accounts off
- Full scope allowed off

With the bundled Keycloak, `papaia-ctl` creates this client itself from the
manifest's `exposes:` entry (realm sync on every start); create it by hand only
with an external identity provider.

**Role → group mapping:**
The `groups` protocol mapper puts the user's realm roles into the token, and
Paperless assigns the user to the identically named Paperless groups on every
login. Paperless only matches groups that **already exist** — it never creates
them. Create the groups you want to drive from Keycloak (e.g. `admin`) once
under *Paperless → Admin → Groups* and give them the permissions the
corresponding realm role should carry. Paperless has no setting that grants
superuser status from a claim; break-glass superuser access stays
`PAPERLESS_ADMIN_USER` / `PAPERLESS_ADMIN_PASSWORD`.

**Audience mapper on the `librechat` client:**
Add a protocol mapper of type *Audience* to the existing `librechat` client:
- Name: `mcp-paperless-audience`
- Included Client Audience: `mcp-paperless`
- Add to ID token: no; add to access token: yes; add to token introspection: yes

With the bundled Keycloak, `papaia-ctl` adds this mapper itself; with an external
identity provider, `papaia-ctl addon install` names the audience to map by hand.

### 3. Wire the network (Seam 1)

`papaia-ctl` writes this override for you (`papaia-ctl addon install paperless`),
resolving the network names from `PAPAIA_PROJECT` in the core `.env`. To wire it by
hand, attach the proxy to the add-on network and LibreChat to the front network only:

```yaml
# papaia-config/overrides/docker-compose.paperless.override.yml
services:
  librechat:
    networks:
      - ${PAPAIA_PROJECT:-papaia}-paperless-front
  nginx-proxy-manager:
    networks:
      - ${PAPAIA_PROJECT:-papaia}-paperless-net
    volumes:
      # The proxy's gate secret, see "Paperless behind its gate".
      - ${PAPAIA_CONFIG_DIR}/secrets/paperless-gate/proxy:/run/papaia/paperless-gate:ro
networks:
  ${PAPAIA_PROJECT:-papaia}-paperless-net:
    external: true
  ${PAPAIA_PROJECT:-papaia}-paperless-front:
    external: true
```

Never attach LibreChat or any other service to `papaia-paperless-net`: every member
of that network can reach the gate, and only Paperless, its data services, the MCP
server and the proxy belong there.

The gate needs the proxy's secret before it starts:
`$PAPAIA_CONFIG_DIR/secrets/paperless-gate/proxy/secret` (one line, 32 to 512
characters of `A-Z a-z 0-9 . _ ~ + / = -`, directory `0700`, file `0600`) and, beside
it, `nginx.conf` with `more_set_input_headers "X-Papaia-Gate: <the same value>";`.
`papaia-ctl setup` writes both. By hand:

```bash
dir="$PAPAIA_CONFIG_DIR/secrets/paperless-gate/proxy"
mkdir -p "$dir" && chmod 0700 "$dir"
umask 077
secret="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')"
printf '%s\n' "$secret" > "$dir/secret"
printf 'more_set_input_headers "X-Papaia-Gate: %s";\n' "$secret" > "$dir/nginx.conf"
unset secret
```

It also needs the directory of the name list,
`$PAPAIA_CONFIG_DIR/security/paperless-gate`, which `papaia-ctl realm sync` writes;
by hand see "Name list" in [Paperless behind its gate](#paperless-behind-its-gate).

Start the addon network first (creates the Docker network):

```bash
docker compose -f addons/paperless/docker-compose.yml up -d
```

Then restart the core stack to pick up the override:

```bash
docker compose -f papaia/src/docker-compose.yml \
  -f papaia-config/overrides/docker-compose.paperless.override.yml \
  up -d
```

### 4. Add the MCP server to LibreChat (Seam 3)

`papaia-ctl` renders this entry from the manifest's `exposes:` entry; the add-on
ships no LibreChat fragment. Without `papaia-ctl`, add the following to your
effective `librechat.yaml` (or to `papaia-config/overlay/ai/librechat/librechat.yaml`):

```yaml
mcpServers:
  mcp-paperless:
    title: 'Document archive'
    description: 'Search, read, file and annotate documents in Paperless with the rights of the person.'
    type: streamable-http
    url: http://paperless-mcp:8000/mcp
    headers:
      Authorization: "Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}"
    startup: false
```

Also add `paperless-mcp:8000` to `mcpSettings.allowedDomains`:

```yaml
mcpSettings:
  allowedDomains:
    - "http://paperless-mcp:8000"
    # ... keep any existing entries
```

### 5. Configure the reverse proxy (Seam 5)

The proxy is the only browser path to Paperless. `papaia-ctl` creates the proxy
host while the add-on is active. By hand, see `integration/infra/nginx/paperless.conf`:
a proxy host for the host name of `PAPERLESS_PUBLIC_URL`, forwarding to
`paperless:8001` (the gate) on the add-on network, that clears
`X-Papaia-Remote-User` and `X-Papaia-Gate` and includes
`/run/papaia/paperless-gate/nginx.conf`. Without that include every request ends
with `403`.

Behind a reverse proxy, Paperless needs to know which hop carries the real
client IP — it uses that for login rate limiting, and gets it wrong by
default, which surfaces as `403 Forbidden` on the login POST. Uncomment
`PAPERLESS_TRUSTED_PROXIES` and `PAPERLESS_ALLAUTH_TRUSTED_PROXY_COUNT` in
`.env` if you hit this. The count is the number of proxy hops in
`X-Forwarded-For`, which is not necessarily the number of listed IPs. Leave
both commented out when unused: an empty value is not the same as an unset
one and aborts startup.

---

## Paperless behind its gate

Paperless takes the person of a request from the header `X-Papaia-Remote-User`.
Whoever can send that header to Paperless acts as anyone, and Paperless creates
an account for a name it does not know. The add-on therefore lets no one but its
two callers reach Paperless:

| Layer | What it does |
|---|---|
| Loopback | `PAPERLESS_BIND_ADDR=127.0.0.1`: Paperless listens only inside its container. A container in the host network, a foreign Compose network or a stranger on the add-on network gets no connection on port 8000. |
| No published port | No container of the add-on publishes a port, also not on `127.0.0.1`. |
| `paperless-gate` | Stock nginx in Paperless' network namespace (`network_mode: service:paperless`), port 8001. A request passes only when `X-Papaia-Gate` carries the secret of a caller; any other request is answered with `403` before Paperless. The gate removes `X-Papaia-Gate` and header names with underscores, and forwards `X-Papaia-Remote-User` only for the MCP server (a name of letters, digits and `._@+-`, at most 150 characters, else `400`). |
| Callers | `proxy`: the secret papaia-ctl writes (mounted read-only from `$PAPAIA_CONFIG_DIR/secrets/paperless-gate/proxy`). `mcp`: generated by the gate on its first start into the volume `paperless-gate-mcp`, mounted read-only into `paperless-mcp`. Each caller has its own secret; the gate refuses to start with a missing, malformed or shared one. A caller is known by its secret only: the proxy's secret never carries a person, whatever header comes with it. |
| Known names | The MCP server's `X-Papaia-Remote-User` passes only for a name on the list the realm sync writes (see below), compared byte for byte (`Anna` is not `anna`). Any other name is answered with `403` and `{"error":"paperless_unknown_user"}` before Paperless, so Paperless creates no account for it, and the gate prints one `papaia.audit/1` row (event `paperless_unknown_user`, the request's `X-Papaia-Correlation-Id` or a request id, never the name). |
| Browser path | The proxy clears both headers on every request and sets only its secret, so a browser request never carries an identity: the person signs in to Paperless with Keycloak. |
| Networks | `papaia-paperless-net`: Paperless, its data services, the MCP server and the proxy. LibreChat is only on `papaia-paperless-front` with the MCP server. |
| Log | One JSON line per request with the caller (`proxy`, `mcp` or empty) and the status; no path, query, header value or secret. Messages about the name list give the number of names, never a name. |

**Name list.** `papaia-ctl realm sync` (also run on every `start`) writes
`$PAPAIA_CONFIG_DIR/security/paperless-gate/users.json`: the user names of the active
accounts of the realm that hold a role of the Paperless module, in this form:

```json
{
  "format": "papaia.paperless-users/1",
  "users": ["anna", "ben"]
}
```

The gate mounts the **directory** read-only at `/run/paperless-gate/users` (the sync
renames a new list into place; a mount of the single file would keep the old one) and
checks it every 5 seconds: a changed list is loaded without a restart, so an account
removed from the realm is refused after the next sync. The gate reads the list
strictly: exactly the members `format` and `users`, each name a letter or digit
followed by letters, digits and `._@+-` (at most 255 characters), no repeated name and
no two names that differ only in case. A list that is missing, a symbolic link, larger
than 1 MiB or malformed admits no name; the browser path through the proxy, which
carries no identity, keeps working. If the gate cannot load a changed list (for
example because a caller's secret broke meanwhile), it stops rather than keep the old
names admitted. Without the directory the gate does not start. By hand (directory
`0755`, file `0644`, written beside the old list and renamed into place):

```bash
dir="$PAPAIA_CONFIG_DIR/security/paperless-gate"
mkdir -p "$dir" && chmod 0755 "$dir"
printf '{"format": "papaia.paperless-users/1", "users": ["anna"]}\n' > "$dir/.users.json.new"
chmod 0644 "$dir/.users.json.new" && mv -f "$dir/.users.json.new" "$dir/users.json"
```

A listed person still needs permissions in Paperless (groups synced from realm roles,
see `PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS`); the list only decides who may be named.

**Rotation.**

- MCP server: `docker compose exec paperless-gate sh /gate/gate.sh rotate mcp`. The
  gate writes a new secret and reloads; the MCP server reads the file again on its
  next call. The gate prints one `papaia.audit/1` line for the change (never the
  value), as it does when it creates the secret.
- Proxy: `papaia-ctl secrets rotate paperless-gate/proxy`, then
  `docker compose exec paperless-gate sh /gate/gate.sh reload` (or restart the add-on).
  Until the gate has reloaded, browser requests end with `403`.

**Requirements.** papaia 1.10.0 or newer (it writes the proxy's secret and the name
list and creates the proxy host) and paperless-mcp-rbac 0.2.0 or newer (it sends its gate secret;
0.1.1 sends none, and the gate refuses every call).

**Before the 0.2.0 release of paperless-mcp-rbac.** Until it is published, the
default of `PAPERLESS_MCP_IMAGE` is its signed release candidate `0.2.0-rc.2` from a
local test registry (`127.0.0.1:57500/unpublished/`), pinned by digest. Hosts
without that registry set `PAPERLESS_MCP_IMAGE` to an image they can pull.
`python3 tools/image_refs.py release` refuses any test registry image and any
release candidate tag; CI runs it for `main` and for every pull request into it,
so the published `ghcr.io/fidonis/paperless-mcp-rbac:0.2.0@sha256:<digest>`
replaces the candidate (in `docker-compose.yml`, `.env.example`, the manifest and
the table above) before this add-on is released.

---

## Stopping and removing

```bash
# Stop containers (leave config bundle intact)
papaia-ctl addon stop paperless

# Stop and remove containers
papaia-ctl addon stop paperless --clean-up

# Remove integration only (config bundle kept, containers untouched)
papaia-ctl addon remove paperless

# Uninstall completely (removes config bundle + deployment entry + containers, volumes kept)
papaia-ctl addon uninstall paperless

# Uninstall and delete volumes
papaia-ctl addon uninstall paperless --clean-up
```

Or manually:

```bash
docker compose -f addons/paperless/docker-compose.yml down -v
# Remove the compose override and re-render papaia config
```

---

## Catalog entry (`exposes:`)

The manifest describes the MCP server once, in `exposes:` (contract generation 7,
schema `exposes.v1` of papaia-kit). The core renders the platform catalog from it,
and every module that talks to Paperless reads the catalog instead of keeping its
own description of the server.

| Field | Value |
|---|---|
| `id` | `paperless.documents` |
| Address | `paperless-mcp:8000/mcp` (internal) |
| Audience | `mcp-paperless` |
| Identity | the person, or a service account a consuming module declares |
| Operations | every tool of `paperless-mcp-rbac`, with effect (`read`, `write`, `delete`), input schema and highest data class; `delete_document` is confirmed with `check_access` first |
| Standard operations | `describe_access`, `check_access` |
| Header identity | `header_identity: true`: Paperless takes the person from `X-Papaia-Remote-User`, so the core never publishes a port of the entry's service |

The core generates from this entry what the add-on used to ship: the
resource-server client `mcp-paperless`, the audience mapper
`mcp-paperless-audience` on the `librechat` client and the LibreChat entry
`mcp-paperless`. Only the web sign-in client `paperless` and the proxy host stay
under `integration/`.

Data classes use the eight-class scale of papaia-kit. A general archive can hold
health data (Art. 9 GDPR) and secrets entrusted to professions bound to
confidentiality (Section 203 StGB), and Paperless sets no limit on what is filed.
Every operation that carries documents, their text, titles, metadata or notes
therefore declares the highest class, `professional_secret`; correspondents are
`partner_contact`, tags and document types `internal`. A Studio app, a Flow run or
a model that is cleared only up to a lower class cannot call these operations.

### Personal data and backup

| Category | Data class | Kept until | Erasure |
|---|---|---|---|
| Accounts (created at the first SSO login) | `personal` | the account is deleted | on request: after the person is deleted in Keycloak, the archive administration deletes the Paperless user; their documents and notes are handled as below |
| Documents | `professional_secret` | the operator's retention rule | on request: kept under the retention rule or handed to a new owner (archive administration) |
| Notes | `professional_secret` | as their document | on request, with their document |

| Volume | Backup |
|---|---|
| `paperless-data` (search index, classifier, application data) | included |
| `paperless-pgdata` (database) | included |
| `paperless-valkeydata` (task queue of the broker) | excluded, rebuilt on start |

The documents themselves live in the bind mounts `MEDIA_DIRECTORY`,
`EXPORT_DIRECTORY` and `CONSUME_DIRECTORY` (default `./data/…` next to the
compose file). They are host paths, not named volumes, so a backup of the
named volumes restores the database without the original files; include
`MEDIA_DIRECTORY` in the host's backup.

Deleting a person in Keycloak does not delete their Paperless user: Paperless
keeps the account, and the documents it owns, until a Paperless administrator
deletes it. Delete the user, or hand its documents to a new
owner first, as part of the erasure of the person.

The exit export is the directory written by Paperless-ngx's own exporter
(`docker compose exec paperless document_exporter ../export`), restored with
`document_importer` of the same Paperless-ngx version. `schemas/paperless-export-1.json`
describes its `manifest.json`.

**Upgrade test.** `tools/test_paperless_upgrade_live.py` installs the add-on with
Paperless-ngx 3.4.2, writes synthetic documents, users, groups and permissions,
switches to the add-on's default image on the same volumes and checks that files,
tag tree, search, users, permissions and the gate are unchanged. It needs Docker and
runs only when asked for:

```bash
PAPERLESS_UPGRADE_LIVE_PROJECT=paperless-upgrade \
PAPERLESS_UPGRADE_LIVE_SUBNET=10.212.121.0/24 \
  python3 -m unittest discover -s tools -p test_paperless_upgrade_live.py
```

---

## Security notes

- **Network isolation:** All Paperless services run on the add-on's own bridge
  (`papaia-paperless-net`, or `papaia-<env>-paperless-net` — one per deployment on a
  shared host), isolated from the papaia core network. Only the proxy is attached to
  it; LibreChat reaches the MCP server over `papaia-paperless-front`.
- **No way around the gate:** Paperless listens on its container's loopback and
  publishes no port; `paperless-gate` passes a request only with the proxy's or the
  MCP server's own secret, and a person only by a name on the realm's list (see
  [Paperless behind its gate](#paperless-behind-its-gate)).
- **OIDC at the data boundary:** `paperless-mcp` validates every incoming Bearer token
  against Keycloak before forwarding requests to Paperless. Paperless enforces its own
  per-user RBAC. No admin credentials are stored in the MCP layer.
- **Remote-user auth:** `PAPERLESS_ENABLE_HTTP_REMOTE_USER` allows the MCP server to
  act on behalf of the authenticated user. The `X-Papaia-Remote-User` header is cleared
  by the proxy on every request (see `integration/infra/nginx/paperless.conf`) and
  again by the gate for every caller but the MCP server.

---

## Known limitations

- **Keycloak client `paperless`** (web sign-in) is still imported from
  `integration/infra/keycloak/paperless.json`; its secret is copied by hand after the
  import (`KC_PAPERLESS_CLIENT_SECRET`, see step 2 above).
