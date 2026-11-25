# papaia-addon-paperless-connect

Connects an existing standalone [Paperless-ngx](https://docs.paperless-ngx.com/) instance to [papaia](https://github.com/Fidonis/papaia).

Adds an OIDC/RBAC-secured MCP server for document access from papaia modules, and describes it in
the platform catalog (`exposes:`), from which the core creates the Keycloak client — without
touching the existing Paperless installation. The add-on is not a module of the suite
(`trust: addon`): the chat reaches it only after you allow it (see
[Chat access](#chat-access-operator-opt-in)).

Use the full [`paperless`](https://github.com/Fidonis/papaia-addon-paperless) add-on if you want
papaia to manage the Paperless-ngx service, its database, and supporting containers.

**Requires papaia ≥ 1.10.0** (`addon_api` 12).

> **Not part of the acceptance.** The existing Paperless and the reverse proxy in front of
> it are run by you, outside papaia. This variant relies on that proxy to keep the
> `X-Papaia-Remote-User` header away from everyone but the MCP server (see
> [Prerequisites](#prerequisites)); it is therefore not part of the acceptance of the papaia
> suite, which covers only the bundled [`paperless`](https://github.com/Fidonis/papaia-addon-paperless)
> add-on with its gate. Securing the proxy is your responsibility.

---

## Services

| Service | Port | Description |
|---|---|---|
| `paperless-mcp` | internal | OIDC/RBAC-secured MCP server for papaia modules and, after the opt-in, the chat |

All services run on the add-on's own isolated Docker bridge network,
`${PAPAIA_PROJECT:-papaia}-paperless-connect-net` — `papaia-paperless-connect-net` on a
default single-stack host, or `papaia-<env>-paperless-connect-net` when several papAIa
deployments share a host. The add-on attaches no service of the core to it.

## Components

The images this add-on runs. Each version is the default of its image variable; set the
variable in `<papaia-config>/addons/paperless-connect/.env` to run another one.

<!-- components:start -->
| Component | Version | Role | Image variable |
|---|---|---|---|
| [paperless-mcp-rbac](https://github.com/Fidonis/paperless-mcp-rbac) | `0.1.2` | primary | `PAPERLESS_MCP_IMAGE` |
<!-- components:end -->

**Before the 0.1.2 release of paperless-mcp-rbac.** Until it is published, the default of
`PAPERLESS_MCP_IMAGE` is its signed release candidate `0.1.2` from a local test registry
(`127.0.0.1:57500/unpublished/`), pinned by digest; it carries the security
fixes of this version. Hosts without that registry set `PAPERLESS_MCP_IMAGE` to an
image they can pull. `python3 tools/image_refs.py release` refuses any test
registry image, so the published `ghcr.io/fidonis/paperless-mcp-rbac:0.1.2@sha256:<digest>`
replaces the candidate before this add-on is released.

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

## TLS trust for the MCP server

`paperless-mcp` runs on an isolated network with no route to the papaia core's
internal `keycloak:8443`, so it always reaches Keycloak over the public
`OIDC_ISSUER` URL for OIDC discovery and JWKS. Its HTTP client **replaces** its
trust store with `PAPERLESS_MCP_SSL_CERT_FILE` when that variable is set — it
does not add to the system trust store.

| `PAPERLESS_MCP_SSL_CERT_FILE` | When |
|---|---|
| *empty* (default) | The Keycloak issuer URL and `PAPERLESS_MCP_PAPERLESS_URL` use publicly-trusted (e.g. Let's Encrypt) certificates. The normal case for a connect add-on, including a bundled papaia Keycloak published through a reverse proxy. |
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
git clone https://github.com/Fidonis/papaia-addon-paperless-connect addons/paperless-connect

# 2. Install — seeds .env in the config bundle, registers in deployment.yaml, renders config
papaia-ctl addon install paperless-connect --path=addons/paperless-connect

# 3. Edit CHANGE_ME values in <papaia-config>/addons/paperless-connect/.env

# 4. Start the addon
papaia-ctl addon start paperless-connect
```

With the bundled Keycloak, `papaia-ctl` creates the client `mcp-paperless` from the
manifest's `exposes:` entry; nothing is imported by hand. With an external identity
provider, create it by hand (step 2 below).

### Chat access (operator opt-in)

The catalog lists this add-on with `trust: addon`, because it relies on your proxy and is
not part of the acceptance of the suite. After installation the chat therefore has **no**
entry for `mcp-paperless`, and the `librechat` client carries no `mcp-paperless` audience.
The add-on never receives the shared chat token, the one token that carries the audiences
of every server of the suite: not before the opt-in, and not after it.

The opt-in is off by default, recorded per add-on, and can be withdrawn at any time. It
needs the add-on installed and started (`papaia-ctl addon start paperless-connect`).

**1. Request.** One host account runs:

```bash
papaia-ctl addon chat allow paperless-connect
```

The command first shows what the chat would reach: the catalog entry
`paperless.documents`, the audience `mcp-paperless`, every tool with its effect and data
class, the data class cap `professional_secret`, and that a second host account must
approve. It then asks you to confirm the audience that was shown. Without a terminal, name
it yourself: `papaia-ctl addon chat allow paperless-connect --approve=mcp-paperless`.
`-y` is refused. Because tools of this add-on write and delete, the opt-in now waits.

**2. Approval.** A second host account (another login on the host) runs the same command.
The account that asked is refused. Only then does the opt-in take effect. The four-eyes
rule (`approvals.four_eyes`, always on in the profile `kritis`) adds nothing here, because
this add-on always needs the second account.

**3. Check and apply.** `papaia-ctl addon chat list` shows the opt-in with its state
(`waiting for approval`, `allowed`, or `allowed, not served` with the reason). Then run
`papaia-ctl start`. It grants the core's chat relay the scope `aud-mcp-paperless`, puts
the relay on the add-on's network (`papaia-paperless-connect-net` on a default host), and
lets LibreChat read the new entry. With an external identity provider the core does not
manage the realm: create the relay's scope `aud-mcp-paperless` and its exchange rights
there by hand, as the core's `docs/chat-relay.md` describes; until then the relay refuses
every call.

**What the chat then reaches.** The chat gets one entry, `mcp-paperless` ("Document
archive"). It points to the core's chat relay,
`http://papaia-chat-relay:8080/servers/mcp-paperless/mcp`, never to `paperless-mcp`, and its
description ends with `Data class up to: professional_secret.`. For every call the relay
exchanges the person's chat token for a token for this server alone: audience exactly
`mcp-paperless`, `papaia-chat-relay` as the authorized party, and the same person.
`paperless-mcp` verifies that token and acts for the person in Paperless, which checks the
person's rights on every call. The relay serves only the tools of the catalog entry below, with the input schemas of
the catalog, and refuses any other method. Only the relay joins the add-on's network,
`librechat` never does. The catalog entry keeps `trust: addon`.

| Tool | Effect | Data class up to |
|---|---|---|
| `search_documents` | read | `professional_secret` |
| `get_document` | read | `professional_secret` |
| `get_document_metadata` | read | `professional_secret` |
| `get_document_content` | read | `professional_secret` |
| `download_document` | read | `professional_secret` |
| `list_tags` | read | `internal` |
| `list_correspondents` | read | `partner_contact` |
| `list_document_types` | read | `internal` |
| `get_notes` | read | `professional_secret` |
| `upload_document` | write | `professional_secret` |
| `update_document` | write | `professional_secret` |
| `delete_document` | delete | `professional_secret` |
| `add_note` | write | `professional_secret` |
| `create_tag` | write | `internal` |
| `create_correspondent` | write | `partner_contact` |
| `describe_access` | read | `internal` |
| `check_access` | check | `internal` |

Documents of the highest class, `professional_secret`, then reach the chat and the models it
uses. Allow the opt-in only where those models are cleared for that class.

**Your proxy stays in charge of the identity header.** The opt-in changes nothing between
`paperless-mcp` and your Paperless: `X-Papaia-Remote-User` still reaches your Paperless only
through your proxy, and the proxy must still reject it from anyone else (see
[Prerequisites](#prerequisites)). This variant stays outside the acceptance of the suite
with or without the opt-in.

**Revoke.** Any host account withdraws the opt-in, whether it is waiting or allowed:

```bash
papaia-ctl addon chat revoke paperless-connect
```

From that moment the relay refuses every call to the add-on. The chat entry and the relay's
scope go at the next `papaia-ctl start`. `papaia-ctl addon uninstall paperless-connect`
removes the opt-in as well. `papaia-ctl addon stop paperless-connect` takes the add-on off
the relay's list while the opt-in stays recorded, and starting the add-on again brings it
back. If an upgrade of the add-on declares tools with other effects, the opt-in is no
longer served until you revoke it and allow it again.

**Audit.** Every request, approval, refusal and revoke is one `papaia.audit/1` row
(`chat_relay.allow`, `chat_relay.approve`, `chat_relay.revoke`) with the host account, the
add-on and the data class cap. No row holds a token or document content. The core's
operator handbook (`docs/chat-relay.md` of papaia) describes the relay itself.

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
| `PAPERLESS_MCP_SSL_CERT_FILE` | Leave empty unless the Keycloak / Paperless certificates are signed by a private CA — see [TLS trust for the MCP server](#tls-trust-for-the-mcp-server) |

`mcp-paperless` needs no client secret: it is a Bearer-token resource server that verifies
incoming tokens against Keycloak's public JWKS endpoint and never authenticates itself to
Keycloak.

### 2. Register Keycloak client

With the bundled Keycloak, `papaia-ctl` creates this client itself from the manifest's
`exposes:` entry (realm sync on every start). Create it by hand only with an external
identity provider, in the `papaia` realm:

**Client `mcp-paperless`** (bearer-only resource server, no flows):
- Client ID: `mcp-paperless`
- Client authentication on, no redirect URIs
- Standard flow, implicit flow, direct access grants and service accounts off
- Full scope allowed off

Do **not** add an audience mapper for `mcp-paperless` to the `librechat` client, do not
attach `librechat` to the add-on's network and do not add an `mcpServers` entry for it by
hand. Each would give the chat a direct path to this server past the operator opt-in, and
the shared chat token, which carries the audiences of every server of the suite, would
reach a server outside the suite. Chat access goes only through the opt-in
([Chat access](#chat-access-operator-opt-in)).

`paperless-mcp` rejects any token whose audience does not match
`PAPERLESS_MCP_OIDC_AUDIENCE` (`401 invalid_token`).

### 3. Start the add-on

```bash
docker compose -f addons/paperless-connect/docker-compose.yml up -d
```

---

## Catalog entry (`exposes:`)

The manifest describes the MCP server once, in `exposes:` (contract generation 7,
schema `exposes.v1` of papaia-kit). The core renders the platform catalog from it,
and every module that talks to Paperless reads the catalog instead of keeping its
own description of the server.

The add-on runs the same tool server as the bundled `paperless` add-on, so the entry
keeps its id, audience and operations: a module that uses `paperless.documents` works
with either add-on. The two add-ons are alternatives; the core refuses an installation
with both (one id and one audience per installation).

| Field | Value |
|---|---|
| `id` | `paperless.documents` |
| Address | `paperless-mcp:8000/mcp` (internal) |
| Audience | `mcp-paperless` |
| Identity | the person, or a service account a consuming module declares |
| Operations | every tool of `paperless-mcp-rbac`, with effect (`read`, `write`, `delete`), input schema and highest data class, the same as in the `paperless` add-on; `delete_document` is confirmed with `check_access` first |
| Standard operations | `describe_access`, `check_access` |
| Header identity | `header_identity: false`: `paperless-mcp` accepts only a verified bearer token. The header `X-Papaia-Remote-User` is read by your Paperless, which this add-on does not run |

The core creates the resource-server client `mcp-paperless` from this entry. The add-on
ships no chat entry, no audience mapper and no client of its own, and has no
`integration/` files. Because the catalog entry has `trust: addon`, the core renders no
chat entry and no `librechat` audience mapper for it; the chat reaches it only through the
opt-in.

Data classes use the eight-class scale of papaia-kit. A general archive can hold health
data (Art. 9 GDPR) and secrets entrusted to professions bound to confidentiality
(Section 203 StGB), and Paperless sets no limit on what is filed. Every operation that
carries documents, their text, titles, metadata or notes therefore declares the highest
class, `professional_secret`; correspondents are `partner_contact`, tags and document
types `internal`. A Studio app, a Flow run or a model that is cleared only up to a lower
class cannot call these operations.

### Personal data

The add-on keeps no data of its own and has no named volume: `storage` declares
`personal_data: false`, and there is no backup item and no exit export. Documents, notes
and accounts stay in your Paperless, outside the papaia installation; record them in that
system's own records of processing, backup and erasure procedures.

`describe_access` and `check_access` need a `paperless-mcp-rbac` release that serves
them; with the 0.1 releases pinned here a call to them fails at the server, and every
consumer that follows the contract refuses `delete_document` (fails closed).

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
# Then re-render the papaia config
```

---

## Security notes

- **Network isolation:** `paperless-mcp` runs on the add-on's own bridge
  (`papaia-paperless-connect-net`, or `papaia-<env>-paperless-connect-net` — one per
  deployment on a shared host), isolated from the papaia core network. The add-on attaches
  no service of the core; only after the operator opt-in does the core put its chat relay
  on this network.
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

- **External identity provider:** the client `mcp-paperless` is created by hand (step 2
  above), and so are the chat relay's scope `aud-mcp-paperless` and its exchange rights for
  the chat opt-in.
- **Chat:** no chat access without the operator opt-in
  ([Chat access](#chat-access-operator-opt-in)).
