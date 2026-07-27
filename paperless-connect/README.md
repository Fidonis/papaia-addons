# papaia-addon-paperless-connect

Connects an existing standalone [Paperless-ngx](https://docs.paperless-ngx.com/) instance to [papaia](https://github.com/Fidonis/papaia).

Adds an OIDC/RBAC-secured MCP server for AI-assisted document access via LibreChat and the
matching Keycloak client registration — without touching the existing Paperless installation.

Use the full [`paperless`](https://github.com/Fidonis/papaia-addon-paperless) add-on if you want
papaia to manage the Paperless-ngx service, its database, and supporting containers.

**Requires papaia ≥ 0.8.0.**

---

## Services

| Service | Port | Description |
|---|---|---|
| `paperless-mcp` | internal | OIDC/RBAC-secured MCP server for LibreChat |

All services run on the isolated `papaia-paperless-connect-net` Docker bridge network.

---

## Prerequisites

Your existing Paperless-ngx instance must have HTTP remote-user authentication enabled so
`paperless-mcp` can act on behalf of authenticated users without storing admin credentials:

```
PAPERLESS_ENABLE_HTTP_REMOTE_USER=true
PAPERLESS_ENABLE_HTTP_REMOTE_USER_API=true
PAPERLESS_HTTP_REMOTE_USER_HEADER_NAME=HTTP_X_PAPAIA_REMOTE_USER
```

The header value is the Paperless username assigned during the user's first login (typically
the `preferred_username` claim from Keycloak).

> **Reverse-proxy warning.** If `PAPERLESS_MCP_PAPERLESS_URL` points at a public URL, the MCP
> server's requests pass through your reverse proxy. That proxy must **forward**
> `X-Papaia-Remote-User` — if it strips the header, every MCP request arrives unauthenticated
> and per-user RBAC silently stops working. At the same time the header must be **rejected on
> requests from anywhere else**, or an external client could impersonate any Paperless user by
> setting it themselves. Restrict it to the MCP server's source address, or give the MCP server
> a private path to Paperless that bypasses the public ingress.

---

## Installation via papaia-ctl

```bash
# 1. Clone this addon into your workspace
git clone https://github.com/Fidonis/papaia-addon-paperless-connect addons/paperless-connect

# 2. Install — seeds .env in the config bundle, registers in deployment.yaml, renders config
papaia-ctl addon install paperless-connect --path=addons/paperless-connect

# 3. Follow the Keycloak checklist printed by install (see below)

# 4. Edit CHANGE_ME values in <papaia-config>/addons/paperless-connect/.env

# 5. Start the addon
papaia-ctl addon start paperless-connect
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
| `PAPAIA_CONFIG_DIR` | Path to the directory created by `papaia-ctl setup` |
| `PAPERLESS_MCP_PAPERLESS_URL` | URL of the existing Paperless instance as reachable from the MCP container — often the public URL (e.g. `https://docs.example.com`), or `http://host.docker.internal:8000` when it runs on the same host |
| `KC_MCP_PAPERLESS_CLIENT_SECRET` | Keycloak client secret for `mcp-paperless` (any random value; the client does not use it for login) |

### 2. Register Keycloak client

In the Keycloak admin UI, create one client in the `papaia` realm:

**Client `mcp-paperless`** (resource server, no flows):
- Client ID: `mcp-paperless`
- No redirect URIs
- All flows disabled
- (see `integration/infra/keycloak/mcp-paperless.json`)

**Audience mapper on the `librechat` client:**
Add a protocol mapper of type *Audience* to the existing `librechat` client:
- Name: `mcp-paperless-audience`
- Included Client Audience: `mcp-paperless`
- Add to access token: yes
- (see `integration/infra/keycloak/librechat-audience-mapper.json`)

### 3. Wire the network (Seam 1)

Create a compose override that attaches `librechat` to `papaia-paperless-connect-net`:

```yaml
# papaia-config/overrides/docker-compose.paperless-connect.override.yml
services:
  librechat:
    networks:
      - papaia-paperless-connect-net
networks:
  papaia-paperless-connect-net:
    external: true
```

Start the addon network first (creates the Docker network):

```bash
docker compose -f addons/paperless-connect/docker-compose.yml up -d
```

Then restart the core stack to pick up the override:

```bash
docker compose -f papaia/src/docker-compose.yml \
  -f papaia-config/overrides/docker-compose.paperless-connect.override.yml \
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

---

## Stopping and removing

```bash
# Stop containers (leave config bundle intact)
papaia-ctl addon stop paperless-connect

# Stop and remove containers
papaia-ctl addon stop paperless-connect --clean-up

# Remove integration only (config bundle kept, containers untouched)
papaia-ctl addon remove paperless-connect

# Uninstall completely (removes config bundle + deployment entry + containers)
papaia-ctl addon uninstall paperless-connect
```

Or manually:

```bash
docker compose -f addons/paperless-connect/docker-compose.yml down
# Remove the compose override and re-render papaia config
```

---

## Security notes

- **Network isolation:** `paperless-mcp` runs on `papaia-paperless-connect-net`, isolated from
  the papaia core network. Only `librechat` is attached to this network via the generated compose
  override.
- **OIDC at the data boundary:** `paperless-mcp` validates every incoming Bearer token against
  Keycloak before forwarding requests to Paperless. No admin credentials are stored in the MCP
  layer.
- **Remote-user auth:** `paperless-mcp` acts on behalf of the authenticated user by forwarding
  the `X-Papaia-Remote-User` header. Because this add-on does not own your ingress, securing that
  header is the operator's responsibility — see the reverse-proxy warning under
  [Prerequisites](#prerequisites). Accepting it from any source is equivalent to letting anyone
  log in as any Paperless user.

---

## Known limitations

- **`mcpSettings.allowedDomains`** must be updated manually (or via overlay) when adding this
  addon alongside other MCP addons. The papaia config render merges dict keys but replaces lists
  wholesale; a fix is tracked in the papaia repository.
- **Keycloak client registration** is not automated in this version; see step 2 above.
- **No Homepage entry.** Homepage resolves link targets from `HOMEPAGE_VAR_*` on its own
  container, and an add-on has no mechanism to set those (the config render deep-merges without
  env substitution, and the override generator only writes network attachments). A dashboard
  entry for the external Paperless has to be added to the Homepage overlay by hand.
