# papaia-addon-qdrant-connect

Connects an existing standalone [Qdrant](https://qdrant.tech/) instance to
[papaia](https://github.com/Fidonis/papaia).

Adds an OIDC/RBAC-secured MCP server ([qdrant-mcp-rbac](https://github.com/Fidonis/qdrant-mcp-rbac))
for AI-assisted vector search via LibreChat and the matching Keycloak client registration —
without touching the existing Qdrant installation.

Use the full [`qdrant`](https://github.com/Fidonis/papaia-addon-qdrant) add-on if you want papaia
to manage the Qdrant vector database itself.

**Requires papaia ≥ 0.8.0.**

---

## Services

| Service | Port | Description |
|---|---|---|
| `qdrant-mcp` | internal | OIDC/RBAC-secured MCP server for LibreChat |

All services run on the add-on's own isolated Docker bridge network,
`${PAPAIA_PROJECT:-papaia}-qdrant-connect-net` — `papaia-qdrant-connect-net` on a default
single-stack host, or `papaia-<env>-qdrant-connect-net` when several papAIa deployments
share a host. `papaia-ctl` resolves the same value when it generates the Seam-1 override.

---

## How access control works

`qdrant-mcp` never hands a Qdrant credential to the caller. For every request it

1. validates the Keycloak Bearer token (signature, expiry, issuer, audience),
2. resolves the caller's realm roles against the ACL collection stored *inside* Qdrant
   (`_rbac_acl`), and
3. derives a short-lived Qdrant JWT scoped to exactly the collections — and, where a grant
   carries a `doc_policy`, the individual documents — those roles allow.

Grants are data, not configuration: an operator holding the break-glass role manages them at
runtime through the `grant_access` / `revoke_access` / `list_acl` MCP tools.

---

## Prerequisites

Your existing Qdrant instance must accept JWTs and use the same signing secret as its API key:

```yaml
# qdrant config.yaml
service:
  api_key: <same value as QDRANT_MCP_QDRANT_JWT_SECRET>
  jwt_rbac: true
```

The equivalent as environment variables:

```
QDRANT__SERVICE__API_KEY=<same value as QDRANT_MCP_QDRANT_JWT_SECRET>
QDRANT__SERVICE__JWT_RBAC=true
```

> **Exposure warning.** `QDRANT_MCP_QDRANT_JWT_SECRET` is the master api-key of your Qdrant
> instance — anyone holding it has unrestricted access, bypassing the RBAC layer entirely. Keep
> the instance off the public internet, or restrict it to the MCP server's source address. The
> per-user enforcement this add-on provides only holds for traffic that actually goes through
> `qdrant-mcp`.

Text search (`search_collection_by_text`) additionally requires an OpenAI-compatible embeddings
endpoint and a populated `_collection_meta` collection — see
[Ingesting documents](#ingesting-documents).

---

## TLS trust for the MCP server

`qdrant-mcp` runs on an isolated network with no route to the papaia core's
internal `keycloak:8443`, so it always reaches Keycloak over the public
`OIDC_ISSUER` URL for OIDC discovery and JWKS. Its HTTP client **replaces** its
trust store with `QDRANT_MCP_SSL_CERT_FILE` when that variable is set — it does
not add to the system trust store. The same file governs the connections to
`QDRANT_MCP_QDRANT_URL` and the embeddings endpoint.

| `QDRANT_MCP_SSL_CERT_FILE` | When |
|---|---|
| *empty* (default) | The Keycloak issuer URL and `QDRANT_MCP_QDRANT_URL` use publicly-trusted (e.g. Let's Encrypt) certificates. The normal case for a connect add-on, including a bundled papaia Keycloak published through a reverse proxy. |
| `/certs/local-ca.crt` | Local-dev core only: Keycloak is served at `host.docker.internal` with the bundled self-signed CA. |

With `AUTH_PROVIDER=external_oidc`, `papaia-ctl` additionally forces this
variable empty via a generated override.

> If the MCP server logs `Unexpected error during OIDC validation` with an
> `ssl.SSLCertVerificationError` and clients see `auth_internal_error`, this
> variable is pointed at a CA that does not sign the Keycloak certificate.

---

## Installation via papaia-ctl

```bash
# 1. Clone this addon into your workspace
git clone https://github.com/Fidonis/papaia-addon-qdrant-connect addons/qdrant-connect

# 2. Install — seeds .env in the config bundle, registers in deployment.yaml, renders config
papaia-ctl addon install qdrant-connect --path=addons/qdrant-connect

# 3. Follow the Keycloak checklist printed by install (see below)

# 4. Edit CHANGE_ME values in <papaia-config>/addons/qdrant-connect/.env

# 5. Start the addon
papaia-ctl addon start qdrant-connect
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
| `QDRANT_MCP_QDRANT_URL` | URL of the existing Qdrant instance as reachable from the MCP container — `http://host.docker.internal:6333` when it runs on the same host |
| `QDRANT_MCP_QDRANT_JWT_SECRET` | The existing instance's `service.api_key` |
| `QDRANT_MCP_EMBEDDING_API_KEY` | `LITELLM_MASTER_KEY` from `<papaia-config>/ai/litellm/.env` |
| `QDRANT_MCP_SSL_CERT_FILE` | Leave empty unless the Keycloak / Qdrant certificates are signed by a private CA — see [TLS trust for the MCP server](#tls-trust-for-the-mcp-server) |

`mcp-qdrant` needs no client secret: it is a Bearer-token resource server that verifies incoming
tokens against Keycloak's public JWKS endpoint and never authenticates itself to Keycloak.

### 2. Register Keycloak objects

In the Keycloak admin UI, in the `papaia` realm:

**Client `mcp-qdrant`** (resource server, no flows):
- Client ID: `mcp-qdrant`
- No redirect URIs
- All flows disabled
- (see `integration/infra/keycloak/mcp-qdrant.json`)

**Audience mapper on the `librechat` client — mandatory, do not skip:**
Add a protocol mapper of type *Audience* to the existing `librechat` client:
- Name: `mcp-qdrant-audience`
- Included Client Audience: `mcp-qdrant`
- Add to access token: yes
- (see `integration/infra/keycloak/librechat-audience-mapper.json`)

> **Why this step is security-critical.** This mapper is the only thing that puts `mcp-qdrant`
> into the `aud` claim of the access token LibreChat forwards. `qdrant-mcp` rejects any token
> whose audience does not match `QDRANT_MCP_OIDC_AUDIENCE`, so without the mapper every MCP call
> fails with `401 invalid_token`.
>
> Verify it with **Clients → librechat → Client scopes → Evaluate**: generate an access token and
> confirm `mcp-qdrant` appears in `aud`. Checking the *Dedicated* mappers tab alone is not
> sufficient — a mapper can also arrive through an assigned client scope.

**Realm role `qdrant-admin` — mandatory, manual:**
Realm roles are outside the add-on integration contract, so neither `papaia-ctl` nor the papaia
manager creates them.

- **Realm roles → Create role** → name `qdrant-admin`
  (see `integration/infra/keycloak/qdrant-admin-role.json`)
- Assign it to the operator who will bootstrap the ACL — **Users → *user* → Role mapping**.

The role name must match `QDRANT_MCP_RBAC_ADMIN_ROLE`. It reaches the MCP server through
`realm_access.roles`, which the `librechat` client already emits.

### 3. Wire the network (Seam 1)

`papaia-ctl` writes this override for you (`papaia-ctl addon install qdrant-connect`),
resolving the network name from `PAPAIA_PROJECT` in the core `.env`. To wire it by hand,
attach `librechat` and `litellm` to the same network the add-on creates:

```yaml
# papaia-config/overrides/docker-compose.qdrant-connect.override.yml
services:
  librechat:
    networks:
      - ${PAPAIA_PROJECT:-papaia}-qdrant-connect-net
  litellm:
    networks:
      - ${PAPAIA_PROJECT:-papaia}-qdrant-connect-net
networks:
  ${PAPAIA_PROJECT:-papaia}-qdrant-connect-net:
    external: true
```

`litellm` is attached so the MCP server can reach the embeddings endpoint. Drop it if
`QDRANT_MCP_EMBEDDING_API_URL` points somewhere else.

Start the addon network first (creates the Docker network):

```bash
docker compose -f addons/qdrant-connect/docker-compose.yml up -d
```

Then restart the core stack to pick up the override:

```bash
docker compose -f papaia/src/docker-compose.yml \
  -f papaia-config/overrides/docker-compose.qdrant-connect.override.yml \
  up -d
```

### 4. Add the MCP server to LibreChat (Seam 3)

Add the following to your effective `librechat.yaml`
(or to `papaia-config/overlay/ai/librechat/librechat.yaml`):

```yaml
mcpServers:
  Qdrant:
    title: 'Qdrant'
    description: 'OIDC-secured vector search over your Qdrant collections.'
    type: streamable-http
    url: http://qdrant-mcp:8000/mcp
    headers:
      Authorization: "Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}"
    startup: false
```

Also add `qdrant-mcp:8000` to `mcpSettings.allowedDomains`:

```yaml
mcpSettings:
  allowedDomains:
    - "http://qdrant-mcp:8000"
    # ... keep any existing entries
```

---

## Bootstrapping access

The ACL collection starts empty, so a plain user sees nothing. As the user holding the
`qdrant-admin` realm role, provision the first grants from LibreChat:

```jsonc
{ "tool": "grant_access", "args": { "role": "finance", "collection": "finance", "access": "r" } }
```

`access` is `r` (read), `rw` (read/write) or `m` (global manage). A grant may additionally carry
a `doc_policy` restricting which individual documents inside the collection the role can see;
see the [qdrant-mcp-rbac README](https://github.com/Fidonis/qdrant-mcp-rbac#document-level-access-control-doc_policy)
for the schema. `list_acl` shows the current state, `revoke_access` removes a grant.

---

## Ingesting documents

This add-on ships no ingestion pipeline — it reads whatever is already in your Qdrant instance.
For `search_collection_by_text` to work, each searchable collection needs an entry in
`_collection_meta` recording the embedding model its vectors were produced with; the ingest job
writes it. The reference implementation is `demo/bootstrap/vectorize.py` in the
[qdrant-mcp-rbac repository](https://github.com/Fidonis/qdrant-mcp-rbac).

Collections without such an entry remain fully usable through `search_collection` (vector in,
results out), `scroll_collection` and `list_documents`.

---

## Stopping and removing

```bash
# Stop containers (leave config bundle intact)
papaia-ctl addon stop qdrant-connect

# Stop and remove containers
papaia-ctl addon stop qdrant-connect --clean-up

# Remove integration only (config bundle kept, containers untouched)
papaia-ctl addon remove qdrant-connect

# Uninstall completely (removes config bundle + deployment entry + containers)
papaia-ctl addon uninstall qdrant-connect
```

Or manually:

```bash
docker compose -f addons/qdrant-connect/docker-compose.yml down
# Remove the compose override and re-render papaia config
```

Your Qdrant data is untouched by any of these — this add-on does not own the instance.

---

## Security notes

- **Network isolation:** `qdrant-mcp` runs on the add-on's own bridge
  (`papaia-qdrant-connect-net`, or `papaia-<env>-qdrant-connect-net` — one per deployment on
  a shared host), isolated from the papaia core network. Only `librechat` and `litellm` are
  attached to it via the generated compose override.
- **OIDC at the data boundary:** every incoming Bearer token is validated against Keycloak
  before any Qdrant request is made.
- **Least privilege per request:** the derived Qdrant JWT is scoped to the caller's grants and
  expires after `QDRANT_MCP_QDRANT_JWT_TTL` seconds. The master api-key never leaves the MCP
  container.
- **Break-glass role:** anyone holding `qdrant-admin` has global manage access to the whole
  instance. Assign it deliberately and to as few accounts as possible.

---

## Known limitations

- **Mutually exclusive with the `qdrant` add-on.** Both register the same Keycloak client
  (`mcp-qdrant`), the same LibreChat MCP entry and the same `qdrant-mcp` service name. Run one
  or the other, not both.
- **`mcpSettings.allowedDomains`** must be updated manually (or via overlay) when adding this
  addon to a papaia core that merges lists by replacement rather than by append.
- **Keycloak client registration** is not automated in this version; see step 2 above.
- **Realm roles** are outside the integration contract — `qdrant-admin` is always a manual step.
