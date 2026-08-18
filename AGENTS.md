# papaia-addons — project context

This document provides structural and architectural context for contributors and automated tooling working in this repository.

---

## What this project does

`papaia-addons` is a catalog of add-ons for the [papaia](https://github.com/Fidonis/papaia) stack. Each add-on is an independently-installable bundle — a Compose service (or set of services), a manifest, and the integration assets needed to wire it into a running deployment — with no shared code between add-ons.

Current catalog:

| Add-on | What it adds |
|---|---|
| `paperless` | Paperless-ngx document management, an OIDC/RBAC MCP server for AI-assisted document access via LibreChat, and Keycloak SSO login for Paperless users |
| `paperless-connect` | The same MCP/Keycloak layer as `paperless`, pointed at an existing standalone Paperless-ngx instance instead of a bundled one |
| `qdrant` | A Qdrant vector database with an OIDC/RBAC-secured MCP server, giving LibreChat per-user vector search |
| `qdrant-connect` | The same MCP layer as `qdrant`, pointed at an existing standalone Qdrant instance |
| `qdrant-ingest` | Scheduled, multi-source document ingestion (S3, WebDAV, SFTP, SMB, FTP, Google Drive, Azure Blob, HTTP, local) into an existing Qdrant collection, plus an operator web interface for managing ingest jobs |

`papaia-ctl`, from the core `papaia` repository, is what actually installs an add-on: it reads the manifest, renders the Compose fragment and env files, imports the Keycloak clients, and registers the MCP server with LibreChat.

---

## Repository layout

```
papaia-addons/
├── <addon-name>/
│   ├── papaia-app.yaml        # Manifest: identity, compatibility, networks, env_prompts, integration
│   ├── docker-compose.yml     # Compose fragment(s) for this add-on
│   ├── .env.example           # Every declared env var with a placeholder value
│   ├── README.md              # What it does, a services table, the minimum papaia version
│   └── integration/
│       ├── infra/keycloak/*.json        # Client export(s) + audience mapper(s), imported on install
│       ├── ai/librechat/librechat.yaml  # MCP server registration for LibreChat
│       └── infra/nginx/*.conf           # Optional vhost fragment (public-facing add-ons only)
└── README.md
```

`qdrant-ingest` additionally ships `jobs.example.yaml`, a template for the ingest job definitions the operator web interface manages.

---

## The manifest contract (`papaia-app.yaml`)

Every add-on manifest carries the same shape:

| Field | Meaning |
|---|---|
| `name`, `version`, `addon_repo` | Identity — `addon_repo` is the add-on's own upstream repository name |
| `requires.addon_api` | The contract generation this add-on is built against; checked against the core's supported window by cores that read it |
| `papaia_compat` | SemVer-range fallback (`>=x.y.z`) for cores that don't read `requires` yet |
| `description` | One-line summary, shown in installer UIs |
| `networks.app_network` | The Docker network name this add-on's own services join |
| `networks.attach` | Core services this add-on's containers additionally attach to. Validated against the core's own Compose services — an add-on cannot list another add-on here |
| `local_ca_env` | Per-service list of env vars pointing at the bundled Keycloak CA cert (mounted from `$PAPAIA_CONFIG_DIR/certs`); `papaia-ctl` clears them via a generated override when `auth_provider=external_oidc`, so the add-on falls back to the system CA bundle instead of failing on a missing cert |
| `env_prompts.<VAR>` | Per-variable metadata: `label` (prompt text), `hint` (secondary explanation), `default` / `default_from_core` (pulls a value from the core's own `.env`), `type` (`text`\|`integer`\|`url`\|`decimal`), `secret` (forces masking, or opts out of the name heuristic), `min`/`max` (numeric bounds), `pattern` (regex). All fields are optional; unset ones fall back to the raw env var name |
| `env_replace_secrets.<VAR>` | Vars that cannot be generated automatically and must be copied in by hand after install — typically a Keycloak client secret, with a `hint` pointing at where to find it in the Keycloak Admin UI |
| `integration.keycloak.clients` | Paths to Keycloak client export JSON files imported on install |
| `integration.keycloak.client_mappers` | Paths to mapper JSON applied to an *existing* client (e.g. the LibreChat client's audience mapper) rather than a new client of this add-on's own |
| `integration.librechat` | Path to the MCP server registration consumed by LibreChat |
| `integration.nginx` | Path to an optional Nginx vhost fragment, for add-ons that publish a browser-facing UI |

Readers ignore unknown manifest keys, so new fields can be introduced without breaking older add-ons.

---

## Engineering conventions

### Branches

| Prefix | Use |
|---|---|
| `feat/<short>` | New user-facing functionality |
| `fix/<short>` | Bug fixes |
| `docs/<short>` | Documentation only |
| `refactor/<short>` | Refactoring without behaviour change |
| `test/<short>` | Adding or fixing tests |
| `ci/<short>` | CI/CD configuration |
| `chore/<short>` | Maintenance / housekeeping |
| `releases/<x.y.z>` | Long-lived milestone branch a release is cut from |

Never push directly to `main` or a `releases/*` branch — always open a pull request.

### PR titles — Conventional Commits

Format: `<type>[(<scope>)][!]: <subject>`

- Subject: lowercase, imperative mood, no trailing period
- `!` after the type or scope marks a breaking change
- CI enforces this format on every PR

### Merge strategy

Feature/fix/docs/chore PRs targeting `main` or a milestone branch are
**squash-merged** — the PR title becomes the commit message on the target
branch. When a milestone branch (`releases/x.y.z`) is integrated into `main`
at release-cut time, it is merged with a **merge commit** instead, so the
individual per-PR commits already on the milestone branch stay visible in
`main`'s history.

---

## Code style and local checks

Run before pushing changes that touch shell scripts or YAML:

```bash
yamllint .
shellcheck --severity=warning <script>
```

- **YAML**: `yamllint` with the project `.yamllint` config, two-space indent
- **Shell**: `shellcheck --severity=warning` must pass; `set -euo pipefail`, prefer `[[` over `[`, quote variables
- **Line endings**: LF everywhere, enforced via `.gitattributes`
- **Trailing whitespace**: forbidden
- Once a Python `src/` is introduced, `ruff check .` and the license-check workflow (see below) start applying

---

## Versioning & releases

- Semantic Versioning; tags follow `vX.Y.Z`.
- `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); it is updated by hand in a dedicated `docs:` PR at release-cut time, not by every feature/fix PR.
- [release-drafter](https://github.com/release-drafter/release-drafter) auto-drafts GitHub Release notes from merged PR titles on every push to `main`; publishing that draft is what creates the release tag.

---

## Security boundaries

- **Never commit `.env` files.** Every add-on ships only `.env.example`, with placeholder values (`.gitignore` enforces this).
- **Never commit generated secrets** — Keycloak realm exports, client secrets, or any other credential produced by an actual install.
- Variables marked `secret: true` in a manifest are either auto-generated on install or, for `env_replace_secrets` entries, filled in by hand from the Keycloak Admin UI after the client import — never hard-coded.
- **No copyleft dependencies** (GPL/LGPL/AGPL/EUPL/SSPL and similar) — enforced by the license-check workflow once a Python `src/` project exists.
