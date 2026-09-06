# papaia-addon-paperless

Paperless-ngx document management addon for [papaia](https://github.com/Fidonis/papaia).

Adds Paperless-ngx with full-text search and OCR, an OIDC-secured MCP server for
AI-assisted document access via LibreChat, and a Keycloak SSO login for Paperless users.

**Requires papaia ≥ 0.8.0.**

---

## Services

| Service | Port | Description |
|---|---|---|
| `paperless` | `8010` (configurable) | Paperless-ngx 3.0.5 web UI and API |
| `paperless-mcp` | internal | OIDC/RBAC-secured MCP server for LibreChat |
| `paperless-broker` | internal | Valkey task broker |
| `paperless-db` | internal | PostgreSQL database |
| `paperless-gotenberg` | internal | Office/email → PDF conversion |
| `paperless-tika` | internal | Text extraction |

All services run on the add-on's own isolated Docker bridge network,
`${PAPAIA_PROJECT:-papaia}-paperless-net` — `papaia-paperless-net` on a default
single-stack host, or `papaia-<env>-paperless-net` when several papAIa deployments
share a host. `papaia-ctl` resolves the same value when it generates the Seam-1
override.

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
| `PAPERLESS_PUBLIC_URL` | Browser-facing URL of Paperless (e.g. `https://docs.example.com`) |
| `PAPERLESS_DBPASS` | PostgreSQL password (generate randomly) |
| `PAPERLESS_SECRET_KEY` | Django secret key (generate ≥ 64 chars randomly) |
| `PAPERLESS_ADMIN_PASSWORD` | Bootstrap admin password (generate randomly) |
| `KC_PAPERLESS_CLIENT_SECRET` | Keycloak client secret for the `paperless` OIDC client |
| `KC_MCP_PAPERLESS_CLIENT_SECRET` | Keycloak client secret for `mcp-paperless` (can be any random value; the client does not use it for login) |

### 2. Register Keycloak clients

In the Keycloak admin UI, create two clients in the `papaia` realm:

**Client `paperless`** (confidential, standard flow):
- Client ID: `paperless`
- Client secret: value of `KC_PAPERLESS_CLIENT_SECRET`
- Redirect URIs: `<PAPERLESS_PUBLIC_URL>/*`
- Protocol mapper: realm roles → `groups` claim (see `integration/infra/keycloak/paperless.json`)

**Client `mcp-paperless`** (resource server, no flows):
- Client ID: `mcp-paperless`
- No redirect URIs
- All flows disabled
- (see `integration/infra/keycloak/mcp-paperless.json`)

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
- Add to access token: yes
- (see `integration/infra/keycloak/librechat-audience-mapper.json`)

### 3. Wire the network (Seam 1)

`papaia-ctl` writes this override for you (`papaia-ctl addon install paperless`),
resolving the network name from `PAPAIA_PROJECT` in the core `.env`. To wire it by
hand, attach `nginx` and `librechat` to the same network the add-on creates:

```yaml
# papaia-config/overrides/docker-compose.paperless.override.yml
services:
  librechat:
    networks:
      - ${PAPAIA_PROJECT:-papaia}-paperless-net
  nginx-proxy-manager:
    networks:
      - ${PAPAIA_PROJECT:-papaia}-paperless-net
networks:
  ${PAPAIA_PROJECT:-papaia}-paperless-net:
    external: true
```

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

Add the following to your effective `librechat.yaml`
(or to `papaia-config/overlay/ai/librechat/librechat.yaml`):

```yaml
mcpServers:
  Paperless:
    title: 'Paperless'
    description: 'OIDC-secured document management via Paperless-ngx.'
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

### 5. Configure reverse proxy (Seam 5, optional)

See `integration/infra/nginx/paperless.conf` for an Nginx configuration reference.
In the Nginx Proxy Manager web UI, add a new proxy host pointing to `paperless:8000`
on the add-on network (`papaia-paperless-net`, or `papaia-<env>-paperless-net`).

Behind a reverse proxy, Paperless needs to know which hop carries the real
client IP — it uses that for login rate limiting, and gets it wrong by
default, which surfaces as `403 Forbidden` on the login POST. Uncomment
`PAPERLESS_TRUSTED_PROXIES` and `PAPERLESS_ALLAUTH_TRUSTED_PROXY_COUNT` in
`.env` if you hit this. The count is the number of proxy hops in
`X-Forwarded-For`, which is not necessarily the number of listed IPs. Leave
both commented out when unused: an empty value is not the same as an unset
one and aborts startup.

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

## Security notes

- **Network isolation:** All Paperless services run on the add-on's own bridge
  (`papaia-paperless-net`, or `papaia-<env>-paperless-net` — one per deployment on a
  shared host), isolated from the papaia core network. Only `nginx` and `librechat` are
  attached to it via the generated compose override.
- **OIDC at the data boundary:** `paperless-mcp` validates every incoming Bearer token
  against Keycloak before forwarding requests to Paperless. Paperless enforces its own
  per-user RBAC. No admin credentials are stored in the MCP layer.
- **Remote-user auth:** `PAPERLESS_ENABLE_HTTP_REMOTE_USER` allows the MCP server to
  act on behalf of the authenticated user. The `X-Papaia-Remote-User` header is stripped
  by nginx at the ingress boundary (see `integration/infra/nginx/paperless.conf`).

---

## Known limitations

- **`mcpSettings.allowedDomains`** must be updated manually (or via overlay) when adding
  this addon alongside other MCP addons. The papaia config render merges dict keys
  but replaces lists wholesale; a fix is tracked in the papaia repository.
- **Keycloak client registration** is not automated in this version; see step 2 above.
