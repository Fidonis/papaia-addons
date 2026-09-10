# papaia-addon-n8n

[n8n](https://n8n.io) workflow automation for
[papaia](https://github.com/Fidonis/papaia), behind a Keycloak SSO gate.

Builds the connective tissue between the stack and everything around it:
scheduled jobs, webhook-triggered flows, and API calls into LiteLLM or
LibreChat — authored in a visual editor rather than in code.

**Requires papaia ≥ 1.0.0.**

---

## Why the SSO gate

n8n Community Edition has no OIDC login. Its own user management is a single
owner account with an e-mail and a password, and an unprotected instance
hands any visitor the ability to run arbitrary code on the host.

This add-on therefore never publishes the engine. An
[oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) sidecar is the
only bound port; every browser request passes Keycloak first, exactly as the
core does it for services without native OIDC. The one deliberate exception
is webhook traffic — see [Webhooks](#webhooks) below.

---

## Services

| Service | Port | Description |
|---|---|---|
| `n8n-auth` | `N8N_EXT_PORT` (8400) | oauth2-proxy SSO gate — the only published port |
| `n8n-proxy` | internal | nginx logout shim, rewrites n8n's sign-out |
| `n8n` | internal | Workflow engine and editor |
| `n8n-db` | internal | PostgreSQL — workflows, credentials, execution history |

All four run on the isolated `papaia-n8n-net` bridge network. `papaia-ctl`
attaches `nginx-proxy-manager` to it so the editor can be published under a
real hostname.

```
browser ──► nginx-proxy-manager ──► n8n-auth ──► n8n-proxy ──► n8n
                                        │                        │
                                        ▼                        ▼
                                    Keycloak                  n8n-db
```

---

## Install

```bash
papaia-ctl addon install n8n
```

The installer prompts for the values in `.env.example`, pulling the OIDC
endpoints from the core configuration. Two steps need attention afterwards:

**1. The Keycloak client secret.** The `n8n` client is imported from
`integration/infra/keycloak/n8n.json`, and Keycloak generates its secret on
import. Copy it from **Keycloak Admin UI → Clients → n8n → Credentials →
Client Secret** into `KC_N8N_CLIENT_SECRET` in
`<papaia-config>/addons/n8n/.env`.

**2. The redirect URIs.** The imported client accepts `*` for both the login
and the post-logout redirect, so the install does not need to know the final
hostname. Once n8n is reachable, narrow them in the same Keycloak client —
**Valid redirect URIs** to `<N8N_PUBLIC_URL>/oauth2/callback`, **Valid post
logout redirect URIs** to `<N8N_PUBLIC_URL>/oauth2/sign_out`. A wildcard
redirect URI on a confidential client is a standing invitation to have
authorization codes delivered somewhere else.

Then:

```bash
papaia-ctl addon start n8n
```

The first browser visit goes through Keycloak and lands in the editor. n8n
still shows its own owner-account setup screen on first run; complete it with
any address — that account is not what protects the instance, the gate is.

---

## Logout

Logging out of n8n ends the Keycloak session, not just n8n's own. The logout
shim hands n8n's post-logout page load to Keycloak's end-session endpoint;
Keycloak asks the user to confirm, then returns to the gate's
`/oauth2/sign_out`, which clears the gate cookie — the next visit is a real
Keycloak login. The Keycloak session is shared across the realm, so this
signs the user out of the other papaia services as well.

---

## Webhooks

Webhook callers are machines. They send no session cookie and cannot follow a
login redirect, so three path prefixes bypass the gate:

```
/webhook/          /webhook-test/          /webhook-waiting/
```

**A workflow's webhook path is its only secret.** Anything reachable under
those prefixes is reachable by anyone who knows or guesses the path, so treat
every enabled production webhook as internet-facing: validate payloads inside
the workflow, and add a shared-secret header check as the first node of any
flow that does something consequential.

These prefixes are fixed in the compose file rather than driven by an
environment variable, because an empty value would compile to a regex
matching every path — turning one misconfigured line into an open editor.
To drop the exemption entirely (no external system posts to n8n), remove the
`--skip-auth-route` arguments via an override:

```yaml
# <papaia-config>/overrides/addons/docker-compose.n8n-no-webhooks.override.yml
services:
  n8n-auth:
    command:
      # ... repeat the arguments from the add-on's compose file, minus the
      # three --skip-auth-route lines. Compose replaces command wholesale.
```

---

## Restricting access

By default every authenticated user of the papaia realm may enter. To gate
the editor behind a realm role instead, create the role in Keycloak, assign
it, and add the flag via an override:

```yaml
# <papaia-config>/overrides/addons/docker-compose.n8n-role.override.yml
services:
  n8n-auth:
    command:
      # ... repeat the add-on's arguments, plus:
      - --allowed-role=n8n-access
```

The role is not created by the add-on: the manifest contract imports
Keycloak *clients*, and realm roles live outside that scope.

---

## Backup and the encryption key

n8n encrypts stored workflow credentials with `N8N_ENCRYPTION_KEY`. When the
variable is unset, n8n generates a key into its data volume on first start —
which quietly makes backups unrestorable, since credentials restored onto a
fresh volume cannot be decrypted with a newly generated key.

This add-on therefore sets the key explicitly, and `papaia-ctl backup`
captures it along with the rest of the config bundle. Two rules follow:

- **Never rotate the key while credentials exist.** Every stored credential
  becomes unreadable, without an error at rotation time — the failure
  surfaces later, one workflow at a time.
- **Keep the key with the backup.** A volume snapshot without it is a
  database of undecryptable secrets.

---

## Configuration

Every variable is documented in [`.env.example`](.env.example). The ones
worth a second look:

| Variable | Why it matters |
|---|---|
| `N8N_PUBLIC_URL` | Every webhook and OAuth callback URL n8n generates is derived from it. Wrong value = webhooks that point nowhere. |
| `N8N_PROXY_HOPS` | Number of proxy hops in `X-Forwarded-For`. `2` with the bundled chain; add one per extra proxy in front. Too low and n8n sees the proxy's IP as the client. |
| `N8N_COOKIE_SECURE` | Must be `true` behind HTTPS — browsers silently drop Secure cookies over plain HTTP, which presents as an endless login loop. |
| `N8N_ENCRYPTION_KEY` | See above. |
| `N8N_DIAGNOSTICS_ENABLED` | n8n's telemetry ping, off by default. |

The engine does not load the add-on's `.env` wholesale; it receives only the
variables its `environment:` block lists. That keeps the gate's client and
cookie secrets out of the container workflows run in — which matters as soon
as `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` lets workflows read the environment.
Pass further n8n settings through an override, not through `.env`:

```yaml
# <papaia-config>/overrides/addons/docker-compose.n8n-env.override.yml
services:
  n8n:
    environment:
      EXECUTIONS_DATA_MAX_AGE: "168"
```

---

## Talking to the rest of the stack

The add-on ships no MCP server, so LibreChat is not attached to its network.
Workflows reach the stack the same way they reach any other API — over its
public endpoints, with a credential stored in n8n:

- **LiteLLM** (`LITELLM_PUBLIC_URL`) for model calls from inside a workflow,
  authenticated with a LiteLLM API key. This is the useful direction: n8n
  orchestrates, LiteLLM routes the model call, and the request still leaves
  through the single auditable gateway.
- **Any add-on's HTTP API** the same way, over its published URL.

n8n's own AI nodes work against LiteLLM's OpenAI-compatible endpoint, so the
standard OpenAI node needs only a changed base URL.

---

## Licensing

n8n is distributed under the
[Sustainable Use License](https://github.com/n8n-io/n8n/blob/master/LICENSE.md),
not an OSI-approved open-source licence. Internal business use of a
self-hosted instance is permitted; reselling n8n itself as a hosted service
is not. Review the licence against your deployment model before offering it
to third parties.

This add-on bundles the official image without modification.
