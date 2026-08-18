# papaia-addon-qdrant-ingest

Scheduled multi-source document ingestion into an existing Qdrant, for
[papaia](https://github.com/Fidonis/papaia). Built on
[qdrant-ingest](https://github.com/Fidonis/qdrant-ingest) with an
[Apache Tika](https://tika.apache.org/) sidecar for text extraction.

Vectorises documents — PDF, Word, Excel, PowerPoint, Markdown and more — from
S3, WebDAV, SFTP, SMB, FTP, Google Drive, Azure Blob, HTTP or a local
directory into a Qdrant collection, on a cron schedule or on demand.

This is the ingestion half that the [`qdrant`](https://github.com/Fidonis/papaia-addon-qdrant)
and [`qdrant-connect`](https://github.com/Fidonis/papaia-addon-qdrant-connect)
add-ons deliberately leave out. It works with either of them, and with a
Qdrant instance running somewhere else entirely.

**Requires papaia ≥ 0.8.0.**

---

## Prerequisites

Two things must be in place before the first ingestion run, and neither is
created by this add-on.

**1. A reachable Qdrant instance and its api-key.** The ingester writes points
and the per-collection embedding metadata directly to the Qdrant REST API, so
it holds the instance's api-key rather than a scoped token. With the `qdrant`
add-on on the same host that is `QDRANT_JWT_SECRET` from
`<papaia-config>/addons/qdrant/.env`, reachable at
`http://host.docker.internal:6333`.

**2. An embedding model that the endpoint actually serves.** The default
`nomic-embed-text` is defined in the core's LiteLLM configuration, but the
matching model download in `src/ai/localai/models.txt` is commented out by
default. Enable it — or point `QI_EMBEDDING_MODEL` at a model that is served —
before running a job; otherwise the dimension probe fails with a 404 from
LiteLLM that does not name the cause.

A Qdrant collection holds vectors of exactly one model: the `_collection_meta`
record the ingester writes carries one `embedding_model` per collection, and
the MCP layer reads it to embed queries the same way. Several jobs may write
into one collection, but they must agree on the model — a mismatch is a
validation error at catalog load time.

---

## Services

| Service | Port | Description |
|---|---|---|
| `qdrant-ingest` | internal (`QI_HTTP_PORT`, 8300) | Scheduler, ingestion engine, REST control plane, MCP endpoint, web interface |
| `qdrant-ingest-tika` | internal | Apache Tika server for text extraction |

Both run on the isolated `papaia-qdrant-ingest-net` Docker bridge network.
There is no published host port, in any configuration. The web interface
becomes reachable from outside the network only once `nginx-proxy-manager`
is attached and a proxy host is configured for `/ui` — see
[Web interface](#web-interface) below.

---

## How it reaches Qdrant

An add-on cannot attach to another add-on's network — `attach:` is validated
against the core's compose services — so this add-on reaches the vector
database over `QI_QDRANT_URL` plus `extra_hosts`, exactly as `qdrant-connect`
reaches a standalone instance. That is not a workaround: it is why the same
code path serves the `qdrant` add-on, `qdrant-connect`, and a remote Qdrant.

If you would rather avoid the host-port detour when the `qdrant` add-on runs
on the same machine, add your own override that also joins this container to
the other add-on's network:

```yaml
# <papaia-config>/overrides/addons/docker-compose.qdrant-ingest-net.override.yml
services:
  qdrant-ingest:
    networks:
      - papaia-qdrant-net
networks:
  papaia-qdrant-net:
    external: true
```

Then set `QI_QDRANT_URL=http://qdrant:6333`. Files in
`overrides/addons/` named `docker-compose.qdrant-ingest-*.override.yml` are
applied automatically by `papaia-ctl addon start`. This is an operator
decision, so it is documented here rather than shipped.

---

## How access control works

The three control surfaces are protected differently, because their
consumers are different.

**MCP — OIDC.** Every request must present a Keycloak Bearer token whose
signature, expiry, issuer and audience validate, and whose realm roles include
`QI_OIDC_OPERATOR_ROLE`. LibreChat forwards the caller's own token. The
add-on network is not a sufficient boundary on its own: LibreChat is a
multi-tenant, prompt-injectable, tool-executing runtime sitting on the same
bridge.

The tools are deliberately non-destructive: `list_ingest_jobs`,
`get_ingest_job`, `trigger_reindex`, `get_ingest_status`, `list_ingest_runs`,
`get_ingest_run`, `list_ingest_collections`, `reload_ingest_config`. There is
no collection deletion, no orphan cleanup, no run abort. `trigger_reindex` may
only pick a mode no more destructive than the configured one, and a full
reindex requires `mcp_allow_full: true` on the job.

**REST — static bearer token.** `QI_API_TOKEN` is generated on install and
required on every `/v1` route; `/health` is exempt so the container
healthcheck works, `/metrics` follows `QI_METRICS_AUTH`. The consumers here
are operators with `curl` and, later, machines without a user identity — a
client-credentials flow would mean a second Keycloak client and a client
secret in the bundle for a surface whose threat model is "another container on
this bridge".

**Web interface — OIDC session.** Sign-in is the authorization code flow with
PKCE against the confidential `qdrant-ingest-ui` client; authorization is the
same `QI_OIDC_OPERATOR_ROLE` the MCP surface requires, so no second realm
role has to be created. The session cookie is scoped to `QI_UI_PATH`, so it
grants nothing on `/v1` or `/mcp` — logging into the interface is not a way
into the token-authenticated surfaces. See [Web interface](#web-interface).

Only the web interface is meant to be published through the proxy. REST and
MCP are mutation and token surfaces respectively; putting either on a public
hostname would be a mistake.

---

## `jobs.yaml`

The job catalog lives at
`<papaia-config>/addons/qdrant-ingest/catalog/jobs.yaml` and is mounted
read-write into the container at `/config/catalog/jobs.yaml` — the one part
of the bundle directory the container may write; the bundle root stays
read-only because it also holds this addon's `.env`. Edit it directly, or
through the [web interface](#web-interface)'s catalog editor once that is
set up. Nothing creates the file for you on install:

```bash
cp qdrant-ingest/jobs.example.yaml \
   "$PAPAIA_CONFIG_DIR/addons/qdrant-ingest/catalog/jobs.yaml"
```

The container starts cleanly without it — zero jobs registered, REST, MCP
and the web interface all up, `/health` answering HTTP 200 with
`"status": "degraded"` and a `config_error`. That is on purpose: a missing
catalog must not put the container into a restart loop.

> **Upgrading from an install predating the web interface.** A catalog
> still at the old `/config/jobs.yaml` (i.e.
> `<papaia-config>/addons/qdrant-ingest/jobs.yaml`, without the `catalog/`
> subdirectory) keeps being read from there automatically — nothing breaks.
> It is served read-only until moved; the web interface offers a one-click
> migration, or move it by hand:
>
> ```bash
> mkdir -p "$PAPAIA_CONFIG_DIR/addons/qdrant-ingest/catalog"
> mv "$PAPAIA_CONFIG_DIR/addons/qdrant-ingest/jobs.yaml" "$PAPAIA_CONFIG_DIR/addons/qdrant-ingest/catalog/jobs.yaml"
> ```

---

## Web interface

An operator UI at `QI_UI_PATH` (default `/ui`) on port `QI_HTTP_PORT` —
the same process as REST and MCP, not a separate container. It shows the
health of Qdrant, the embeddings endpoint and Tika, the job catalog and
run history, the collections and the leftovers of a renamed job — and it
is the surface that can **write** `jobs.yaml`: create, edit and delete
jobs through a form, or edit the file directly with comments preserved.

It stays unmounted unless `OIDC_ISSUER`, `QI_UI_PUBLIC_URL`,
`QI_UI_CLIENT_SECRET` and `QI_UI_SESSION_SECRET` are all set — the same
shape as MCP disabling itself on an empty `OIDC_ISSUER`. Sign-in is OIDC
against the confidential `qdrant-ingest-ui` client; authorization is the
existing `QI_OIDC_OPERATOR_ROLE`, so no second realm role is needed.

Full reference, including the write path and the vendored look shared
with `papaia-manager`, is in the
[qdrant-ingest documentation](https://github.com/Fidonis/qdrant-ingest/blob/main/docs/ui.md).

### Enabling it

1. Fill in `QI_UI_PUBLIC_URL` in the bundle `.env` — the browser-facing
   URL of this addon, e.g. `https://ingest.example.com`.
2. Import `integration/infra/keycloak/qdrant-ingest-ui.json` (see the
   Keycloak checklist below) and copy the client secret it generated
   into `QI_UI_CLIENT_SECRET`.
3. Restart the addon: `papaia-ctl addon start qdrant-ingest`.
4. Sign in at `<QI_UI_PUBLIC_URL>/ui` with an account holding the
   `qdrant-ingest-operator` realm role.

### Publishing it (optional)

The interface is reachable inside the addon network at
`http://qdrant-ingest:8300/ui` regardless. To reach it from a browser
outside the host, attach `nginx-proxy-manager` (already in this addon's
`networks.attach`) and add a proxy host — see
`integration/infra/nginx/qdrant-ingest.conf` for the exact fragment.
**Only `/ui` is meant to be published.** `/v1` is a mutation API behind
a static bearer token and `/mcp` is a Keycloak-token resource server;
neither belongs on a public hostname.

### Running as the host user

The container now writes into a host bind mount — the `catalog/`
subdirectory the web interface saves `jobs.yaml` into — so it runs as
`${UID:-1000}:${GID:-1000}`, the same values `papaia-ctl setup` stamps
into the core `.env` for `papaia-manager`. If the host uid is not 1000
**and** the `qi-cache` / `qi-state` volumes already exist from an older
install, they were created with the old ownership; chown them once or
recreate them.

**Credentials never go in this file.** Every secret-typed field accepts only
the form `${env:QI_SECRET_<NAME>}` and resolves against the add-on `.env`; a
literal is a hard validation error naming the job and the field. Only names
matching `^QI_SECRET_[A-Z0-9_]+$` resolve, so a tampered catalog cannot read
`QI_QDRANT_API_KEY` or `QI_API_TOKEN`. Three empty slots ship in
`.env.example`, and any number of further `QI_SECRET_<NAME>` keys may be
appended to the bundle `.env` — the seeding step never removes or overwrites
keys you added.

Changes take effect on start, on `POST /v1/config/reload` (or the
`reload_ingest_config` tool), and through an mtime poll every
`QI_JOBS_RELOAD_INTERVAL` seconds. Reloads are transactional: if validation
fails, the previous catalog keeps serving and the errors appear under
`GET /v1/config` and in `/health.config_error`. A run in flight is never
interrupted; the new definition applies at the next fire.

Each job picks one of three modes:

| Mode | Behaviour |
|---|---|
| `full` | Rebuilds the job's slice from scratch via a generation sweep — no blind window, and sibling jobs writing to the same collection are untouched |
| `append` | Only adds documents whose source is not yet known. Never updates, never deletes; changed documents are counted so the drift is visible |
| `upsert` | Adds new documents, re-embeds changed ones, and removes those that vanished from the source, behind two deletion guards |

The full reference for the schema, the modes and the four-stage change
detection is in the
[qdrant-ingest documentation](https://github.com/Fidonis/qdrant-ingest/tree/main/docs).

Documents for sources of type `local` are read from `/data/local`, bound from
`QI_LOCAL_MOUNT`. Its default is this add-on's own bundle directory — a path
that always exists, so a fresh install never fails on the mount — and it is
meant to be pointed at the real document directory.

---

## Installation via papaia-ctl

```bash
# 1. Clone this addon into your workspace
git clone https://github.com/Fidonis/papaia-addon-qdrant-ingest addons/qdrant-ingest

# 2. Install — seeds .env in the config bundle, registers in deployment.yaml, renders config
papaia-ctl addon install qdrant-ingest --path=addons/qdrant-ingest

# 3. Follow the Keycloak checklist printed by install (see below)

# 4. Edit CHANGE_ME values in <papaia-config>/addons/qdrant-ingest/.env

# 5. Place the job catalog (or skip this and use the web interface's
#    catalog editor once it is set up -- see Web interface below)
cp addons/qdrant-ingest/jobs.example.yaml \
   "$PAPAIA_CONFIG_DIR/addons/qdrant-ingest/catalog/jobs.yaml"

# 6. Start the addon
papaia-ctl addon start qdrant-ingest
```

`QI_API_TOKEN` is generated during install — you never enter it by hand.

Verify afterwards:

```bash
curl -s -H "Authorization: Bearer $QI_API_TOKEN" \
     http://qdrant-ingest:8300/v1/jobs
```

`GET /health` reports the catalog state and the reachability of Qdrant, the
embeddings endpoint and Tika; `GET /v1/jobs/<id>/preview` shows what the
filters actually match, and `POST /v1/jobs/<id>/run` with `{"dry_run": true}`
runs everything except embedding and writing.

---

## Manual installation

If you are not using `papaia-ctl`, follow these steps manually.

### 1. Configure environment

Copy `.env.example` to `.env` in this directory:

```bash
cp .env.example .env
```

Edit `.env`, fill in all `CHANGE_ME` values, and replace `GENERATE_SECRET`
with a strong random value (`openssl rand -hex 24`):

| Variable | Description |
|---|---|
| `OIDC_ISSUER` | Keycloak issuer URL (same value as `AUTH_HOST` in your papaia setup) |
| `PAPAIA_CONFIG_DIR` | Path to the directory created by `papaia-ctl setup` |
| `QI_QDRANT_URL` | Qdrant URL as reachable from this container |
| `QI_QDRANT_API_KEY` | Qdrant api-key — `QDRANT_JWT_SECRET` when using the `qdrant` add-on |
| `QI_EMBEDDING_API_KEY` | `LITELLM_MASTER_KEY` from `<papaia-config>/ai/litellm/.env` |
| `QI_API_TOKEN` | Bearer token for the REST control plane |
| `QI_UI_PUBLIC_URL` | Browser-facing URL of the web interface, e.g. `https://ingest.example.com`; leave blank (not `CHANGE_ME`) to keep the interface off |
| `QI_UI_CLIENT_SECRET` | Keycloak client secret for the `qdrant-ingest-ui` OIDC client (see step 2) |
| `QI_UI_SESSION_SECRET` | Session-cookie signing key (generate like `QI_API_TOKEN`) |

`mcp-qdrant-ingest` needs no client secret: it is a Bearer-token resource
server that verifies incoming tokens against Keycloak's public JWKS endpoint
and never authenticates itself to Keycloak.

### 2. Register Keycloak objects

In the Keycloak admin UI, in the `papaia` realm:

**Client `mcp-qdrant-ingest`** (resource server, no flows):
- Client ID: `mcp-qdrant-ingest`
- No redirect URIs
- All flows disabled
- (see `integration/infra/keycloak/mcp-qdrant-ingest.json`)

**Client `qdrant-ingest-ui`** (confidential, standard flow) — only needed
if you are enabling the [web interface](#web-interface):
- Client ID: `qdrant-ingest-ui`
- Client secret: value of `QI_UI_CLIENT_SECRET`
- Redirect URIs: `<QI_UI_PUBLIC_URL>/ui/auth/callback`
- (see `integration/infra/keycloak/qdrant-ingest-ui.json`; it ships with a
  wildcard redirect URI as an import starting point — tighten it to the
  exact callback URL above)

No audience mapper and no second realm role: the interface validates its
own ID token (audience `qdrant-ingest-ui`) and checks the same
`qdrant-ingest-operator` role the MCP surface requires below.

**Audience mapper on the `librechat` client — mandatory, do not skip:**
Add a protocol mapper of type *Audience* to the existing `librechat` client:
- Name: `mcp-qdrant-ingest-audience`
- Included Client Audience: `mcp-qdrant-ingest`
- Add to access token: yes
- (see `integration/infra/keycloak/librechat-audience-mapper.json`)

> **Why this step is security-critical.** This mapper is the only thing that
> puts `mcp-qdrant-ingest` into the `aud` claim of the access token LibreChat
> forwards. Any token whose audience does not match `QI_OIDC_AUDIENCE` is
> rejected, so without the mapper every MCP call fails with `401`.
>
> Verify it with **Clients → librechat → Client scopes → Evaluate**: generate
> an access token and confirm `mcp-qdrant-ingest` appears in `aud`. Checking
> the *Dedicated* mappers tab alone is not sufficient — a mapper can also
> arrive through an assigned client scope.

**Realm role `qdrant-ingest-operator` — mandatory, manual:**
Realm roles are outside the add-on integration contract, so neither
`papaia-ctl` nor the papaia manager creates them.

- **Realm roles → Create role** → name `qdrant-ingest-operator`
  (see `integration/infra/keycloak/qdrant-ingest-operator-role.json`)
- Assign it to the users allowed to drive ingestion — **Users → *user* →
  Role mapping**.

The role name must match `QI_OIDC_OPERATOR_ROLE`. It reaches the server
through `realm_access.roles`, which the `librechat` client already emits.
Without it every MCP call is rejected with `403 missing_operator_role`.

### 3. Wire the network (Seam 1)

Create a compose override that attaches `librechat`, `litellm` and
(optionally) `nginx-proxy-manager` to `papaia-qdrant-ingest-net`:

```yaml
# papaia-config/overrides/docker-compose.qdrant-ingest.override.yml
services:
  librechat:
    networks:
      - papaia-qdrant-ingest-net
  litellm:
    networks:
      - papaia-qdrant-ingest-net
  nginx-proxy-manager:
    networks:
      - papaia-qdrant-ingest-net
networks:
  papaia-qdrant-ingest-net:
    external: true
```

`litellm` is attached for the embeddings endpoint; drop it if
`QI_EMBEDDING_API_URL` points somewhere else. `librechat` is the MCP client;
drop it if you only use the REST control plane. `nginx-proxy-manager` is
only needed to publish the [web interface](#web-interface) — drop it if
you reach `/ui` some other way, or not at all.

Start the addon network first (creates the Docker network):

```bash
docker compose -f addons/qdrant-ingest/docker-compose.yml up -d
```

Then restart the core stack to pick up the override:

```bash
docker compose -f papaia/src/docker-compose.yml \
  -f papaia-config/overrides/docker-compose.qdrant-ingest.override.yml \
  up -d
```

### 4. Add the MCP server to LibreChat (Seam 3)

Add the following to your effective `librechat.yaml`
(or to `papaia-config/overlay/ai/librechat/librechat.yaml`):

```yaml
mcpServers:
  QdrantIngest:
    title: 'Qdrant Ingest'
    description: 'Inspect and trigger the document ingestion jobs feeding your Qdrant collections.'
    type: streamable-http
    url: http://qdrant-ingest:8300/mcp
    headers:
      Authorization: "Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}"
    startup: false
```

Also add `qdrant-ingest:8300` to `mcpSettings.allowedDomains`:

```yaml
mcpSettings:
  allowedDomains:
    - "http://qdrant-ingest:8300"
    # ... keep any existing entries
```

### 5. Publish the web interface (optional)

Add a proxy host in Nginx Proxy Manager for `/ui` only — see
[Web interface](#web-interface) above and
`integration/infra/nginx/qdrant-ingest.conf` for the exact fragment.
Skip this step entirely if you are not enabling the interface.

---

## Stopping and removing

```bash
# Stop containers (leave config bundle and data intact)
papaia-ctl addon stop qdrant-ingest

# Stop and remove containers (volumes kept)
papaia-ctl addon stop qdrant-ingest --clean-up

# Remove integration only (config bundle kept, containers untouched)
papaia-ctl addon remove qdrant-ingest

# Uninstall completely (removes config bundle + deployment entry + containers)
papaia-ctl addon uninstall qdrant-ingest

# ... and delete the cache and state volumes as well
papaia-ctl addon uninstall qdrant-ingest --clean-up
```

The `qi-cache` and `qi-state` volumes survive everything except `--clean-up` /
`down -v`. Losing them is recoverable but not free: without `qi-state` an
`upsert` job re-extracts and re-embeds its whole corpus (correct, expensive),
and an `append` job relies on its state-loss probe to avoid duplicating it.
Vectors already written to Qdrant are never affected — they live in the
vector database, not here.

---

## Security notes

- **Network isolation:** both services run on `papaia-qdrant-ingest-net`,
  isolated from the papaia core network. Only `librechat`, `litellm` and
  (if the web interface is enabled) `nginx-proxy-manager` are attached,
  via the generated compose override.
- **The ingester holds the Qdrant api-key.** It writes `_collection_meta` on
  every run, which the MCP layer refuses as a system collection, so it talks
  to Qdrant directly. Treat this container as privileged with respect to the
  vector database.
- **Three authenticated surfaces, one of them public.** MCP validates
  Keycloak tokens and requires the operator realm role; REST requires a
  static bearer token; the web interface requires an OIDC session held by
  an account with the same operator role. The nginx fragment publishes
  **only** `/ui` — REST and MCP stay off the public hostname; a session
  cookie carries no access to either.
- **The catalog subdirectory is the one writable path in the bundle
  mount.** `/config/catalog` is read-write so the web interface can save
  `jobs.yaml`; everything else under `/config`, including this add-on's
  own `.env`, stays read-only from inside the container.
- **The container runs as the host uid** (`UID`/`GID` from the core
  `.env`, default 1000), not a fixed system uid, because it now writes
  to that bind mount.
- **The published port is opt-in.** `QI_EXT_PORT` and the `ports:` block are
  commented out by default. If you publish them, keep the binding on a trusted
  interface via `HOST_IP`.
- **Source credentials stay out of git.** They live only in the bundle `.env`;
  `jobs.yaml` may reference them but never contain them, and that is enforced,
  not merely documented.
- **`acl_tags` are what the MCP layer's document-level policies filter on.**
  A job that omits them produces documents no `doc_policy` can narrow.

---

## Known limitations

- **The web interface is a second login client, not a papaia-manager
  integration.** The manager owns add-on lifecycle and the bundle `.env`;
  it does not (yet) surface a per-addon UI of its own. `qdrant-ingest-ui`
  is deliberately independent of it — see the
  [qdrant-ingest documentation](https://github.com/Fidonis/qdrant-ingest/blob/main/docs/ui.md)
  for the alternatives that were considered.
- **No read-only role for the web interface.** Viewing the catalog and
  triggering a run share one realm role, `qdrant-ingest-operator` — the
  same one MCP already requires. A second, view-only role would be
  another manual Keycloak step for little gain; add one if you need it.
- **Realm roles** are outside the integration contract — `qdrant-ingest-operator`
  is always a manual step, as is the ACL grant that lets a role read the
  collection afterwards.
- **`mcpSettings.allowedDomains`** must be updated manually (or via overlay)
  when adding this addon to a papaia core that merges lists by replacement
  rather than by append.
- **Keycloak client registration** is not automated in this version; see
  step 2 above.
- **One embedding model per collection.** Changing a collection's model means
  a `full` run with `full_scope: collection` — that is the only path that
  recreates the collection with a new vector dimension.
- **OCR for scanned PDFs and images needs the `-full` Tika image**, selected
  via `QI_TIKA_IMAGE`. The default image skips image-only pages, records them
  as `skipped_no_text`, and does not retry until the file changes.
- **Renaming a job orphans its points.** They are detected at reload, listed
  under `GET /v1/orphans` and removed with
  `DELETE /v1/orphans/{job_id}?confirm=true`. Deliberately not exposed
  over MCP.
- **The bundle directory is mounted read-only at `/config`, except
  `/config/catalog`.** The add-on's own `.env` is visible inside the
  container as `/config/.env` regardless — those values are already in
  the process environment, so no additional secret is exposed by the
  mount. Only the nested `catalog/` subdirectory is writable, and only
  `jobs.yaml` lives there.
