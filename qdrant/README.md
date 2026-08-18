# papaia-addon-qdrant

[Qdrant](https://qdrant.tech/) vector database with an OIDC/RBAC-secured MCP server
([qdrant-mcp-rbac](https://github.com/Fidonis/qdrant-mcp-rbac)) for
[papaia](https://github.com/Fidonis/papaia).

Gives LibreChat per-user vector search: the MCP server validates the caller's Keycloak token and
derives a Qdrant JWT scoped to exactly the collections — and, optionally, the documents — the
caller's roles grant.

Use the [`qdrant-connect`](https://github.com/Fidonis/papaia-addon-qdrant-connect) add-on
instead if you already run a Qdrant instance and only want the MCP layer.

**Requires papaia ≥ 0.8.0.**

---

## Services

| Service | Port | Description |
|---|---|---|
| `qdrant` | `QDRANT_EXT_PORT` (6333) | Vector database, JWT RBAC enabled |
| `qdrant-mcp` | internal | OIDC/RBAC-secured MCP server for LibreChat |

All services run on the isolated `papaia-qdrant-net` Docker bridge network. The published Qdrant
port is the ingestion and administration path; there is no public ingress.

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

`QDRANT_JWT_SECRET` is Qdrant's api-key *and* the signing secret of those derived tokens. Both
services read the same value; a drift between them rejects every derived token.

---

## Installation via papaia-ctl

```bash
# 1. Clone this addon into your workspace
git clone https://github.com/Fidonis/papaia-addon-qdrant addons/qdrant

# 2. Install — seeds .env in the config bundle, registers in deployment.yaml, renders config
papaia-ctl addon install qdrant --path=addons/qdrant

# 3. Follow the Keycloak checklist printed by install (see below)

# 4. Edit CHANGE_ME values in <papaia-config>/addons/qdrant/.env

# 5. Start the addon
papaia-ctl addon start qdrant
```

`QDRANT_JWT_SECRET` is generated during install — you never enter it by hand.

---

## Manual installation

If you are not using `papaia-ctl`, follow these steps manually.

### 1. Configure environment

Copy `.env.example` to `.env` in this directory:

```bash
cp .env.example .env
```

Edit `.env` and fill in all `CHANGE_ME` values, and replace `GENERATE_SECRET` with a strong
random value (`openssl rand -hex 24`):

| Variable | Description |
|---|---|
| `OIDC_ISSUER` | Keycloak issuer URL (same value as `AUTH_HOST` in your papaia setup) |
| `PAPAIA_CONFIG_DIR` | Path to the directory created by `papaia-ctl setup` |
| `QDRANT_JWT_SECRET` | Qdrant api-key and JWT signing secret — one value for both services |
| `QDRANT_MCP_EMBEDDING_API_KEY` | `LITELLM_MASTER_KEY` from `<papaia-config>/ai/litellm/.env` |

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

Create a compose override that attaches `librechat` and `litellm` to `papaia-qdrant-net`:

```yaml
# papaia-config/overrides/docker-compose.qdrant.override.yml
services:
  librechat:
    networks:
      - papaia-qdrant-net
  litellm:
    networks:
      - papaia-qdrant-net
networks:
  papaia-qdrant-net:
    external: true
```

`litellm` is attached so the MCP server can reach the embeddings endpoint. Drop it if
`QDRANT_MCP_EMBEDDING_API_URL` points somewhere else.

Start the addon network first (creates the Docker network):

```bash
docker compose -f addons/qdrant/docker-compose.yml up -d
```

Then restart the core stack to pick up the override:

```bash
docker compose -f papaia/src/docker-compose.yml \
  -f papaia-config/overrides/docker-compose.qdrant.override.yml \
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

This add-on ships the database, not an ingestion pipeline. Load collections through the
published Qdrant port using `QDRANT_JWT_SECRET` as the api-key:

```bash
curl -H "api-key: $QDRANT_JWT_SECRET" http://localhost:6333/collections
```

For `search_collection_by_text` to work, each searchable collection needs an entry in
`_collection_meta` recording the embedding model its vectors were produced with; the ingest job
writes it. The reference implementation is `demo/bootstrap/vectorize.py` in the
[qdrant-mcp-rbac repository](https://github.com/Fidonis/qdrant-mcp-rbac) — point it at
`http://localhost:6333` and at the same embeddings endpoint the MCP server uses, so the vectors
match at query time.

Collections without such an entry remain fully usable through `search_collection` (vector in,
results out), `scroll_collection` and `list_documents`.

---

## Stopping and removing

```bash
# Stop containers (leave config bundle and data intact)
papaia-ctl addon stop qdrant

# Stop and remove containers (volume kept)
papaia-ctl addon stop qdrant --clean-up

# Remove integration only (config bundle kept, containers untouched)
papaia-ctl addon remove qdrant

# Uninstall completely (removes config bundle + deployment entry + containers)
papaia-ctl addon uninstall qdrant

# ... and delete the vector data as well
papaia-ctl addon uninstall qdrant --clean-up
```

Or manually:

```bash
docker compose -f addons/qdrant/docker-compose.yml down
# Remove the compose override and re-render papaia config
```

The `qdrant-storage` volume survives everything except `--clean-up` / `down -v`.

---

## Security notes

- **Network isolation:** both services run on `papaia-qdrant-net`, isolated from the papaia core
  network. Only `librechat` and `litellm` are attached to this network via the generated compose
  override.
- **OIDC at the data boundary:** every incoming Bearer token is validated against Keycloak before
  any Qdrant request is made.
- **Least privilege per request:** the derived Qdrant JWT is scoped to the caller's grants and
  expires after `QDRANT_MCP_QDRANT_JWT_TTL` seconds. The master api-key never leaves the MCP
  container.
- **The published port bypasses RBAC.** `QDRANT_EXT_PORT` exposes the raw API, where the api-key
  grants unrestricted access to every collection. Bind it to a trusted interface via `HOST_IP`,
  or remove the port mapping once ingestion runs inside the add-on network.
- **Break-glass role:** anyone holding `qdrant-admin` has global manage access to the whole
  instance. Assign it deliberately and to as few accounts as possible.

---

## Known limitations

- **Mutually exclusive with the `qdrant-connect` add-on.** Both register the same Keycloak client
  (`mcp-qdrant`), the same LibreChat MCP entry and the same `qdrant-mcp` service name. Run one or
  the other, not both.
- **`mcpSettings.allowedDomains`** must be updated manually (or via overlay) when adding this
  addon to a papaia core that merges lists by replacement rather than by append.
- **Keycloak client registration** is not automated in this version; see step 2 above.
- **Realm roles** are outside the integration contract — `qdrant-admin` is always a manual step.
- **No ingestion pipeline** ships with this add-on; see
  [Ingesting documents](#ingesting-documents).
