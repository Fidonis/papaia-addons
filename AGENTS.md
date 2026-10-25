# papaia-addons — project context

This document provides structural and architectural context for contributors and automated tooling working in this repository.

---

## What this project does

`papaia-addons` is a catalog of add-ons for the [papaia](https://github.com/Fidonis/papaia) stack. Each add-on is an independently-installable bundle — a Compose service (or set of services), a manifest, and the integration assets needed to wire it into a running deployment — with no shared code between add-ons.

Current catalog:

| Add-on | What it adds |
|---|---|
| `mediawiki` | A MediaWiki knowledge base with Keycloak login and group sync, namespace-level access tiers, and the MediaWiki MCP server, through which LibreChat works in the wiki as the signed-in user within the same tiers |
| `n8n` | n8n workflow automation behind an oauth2-proxy Keycloak SSO gate — the engine is never published, and only the webhook paths bypass the gate |
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
│   ├── CHANGELOG.md           # One section per add-on version, with upgrade notes
│   └── integration/
│       ├── infra/keycloak/*.json        # Client export(s) + audience mapper(s), imported on install
│       ├── ai/librechat/librechat.yaml  # MCP server registration for LibreChat
│       └── infra/nginx/*.conf           # Optional vhost fragment (public-facing add-ons only)
├── tools/
│   ├── components.py          # Keeps component versions, .env.example and README tables equal to Compose; checks manifests and changelogs
│   └── test_components.py     # Unit tests for it
└── README.md
```

`qdrant-ingest` additionally ships `jobs.example.yaml`, a template for the ingest job definitions the operator web interface manages. `mediawiki` additionally ships `image/` (the build context of its wiki image, published as `ghcr.io/fidonis/papaia-mediawiki:<MediaWiki version>-<add-on version>` by the `mediawiki-image` workflow; a change there needs a new add-on version), `runtime/` (the env-driven `LocalSettings.php`, entrypoint and OAuth bootstrap, mounted read-only into the wiki container), `mcp/` (configuration and start script of the MCP server), `settings.d/` (site-specific settings) and `tests/` (the access-tier leak checks, `tests/run.sh`). `n8n` additionally ships `nginx.conf`, the config template of its logout shim (swappable via `N8N_PROXY_CONF`).

---

## The manifest contract (`papaia-app.yaml`)

Every add-on manifest carries the same shape:

| Field | Meaning |
|---|---|
| `name`, `version`, `addon_repo` | Identity — `addon_repo` is the add-on's own upstream repository name |
| `requires.addon_api` | The contract generation this add-on is built against; checked against the core's supported window by cores that read it |
| `papaia_compat` | SemVer-range fallback (`>=x.y.z`) for cores that don't read `requires` yet |
| `description` | One-line summary, shown in installer UIs |
| `components[]` | The software this add-on packages, next to `version`, which counts the add-on bundle itself. Per entry: `id`, `name`, `role` (`primary`\|`sidecar`), `kind` (`upstream`\|`fidonis`), `image_var` (the Compose variable whose default names the image), `version` (that default's tag, quoted), `source` (https URL) and, for a floating tag only, `pinned: false`. The Compose default is the source of truth; `tools/components.py` keeps `version`, the image lines of `.env.example` and the README table equal to it. Consumers such as papaia-manager read it to show what an add-on runs |
| `networks.app_network` | The Docker network name this add-on's own services join. May be a literal, or (with `requires.addon_api: 2`) a `${PAPAIA_PROJECT:-papaia}-<name>-net` template that `papaia-ctl` resolves against the core `.env` so the bridge is scoped per deployment — use the identical string in the add-on's own compose `networks.<key>.name` |
| `networks.attach` | Core services this add-on's containers additionally attach to. Validated against the core's own Compose services — an add-on cannot list another add-on here |
| `local_ca_env` | Per-service list of env vars pointing at the bundled Keycloak CA cert (mounted from `$PAPAIA_CONFIG_DIR/certs`); `papaia-ctl` clears them via a generated override when `auth_provider=external_oidc`, so the add-on falls back to the system CA bundle instead of failing on a missing cert |
| `env_prompts.<VAR>` | Per-variable metadata: `label` (prompt text), `hint` (secondary explanation), `default` / `default_from_core` (pulls a value from the core's own `.env`), `type` (`text`\|`integer`\|`url`\|`decimal`), `secret` (forces masking, or opts out of the name heuristic), `min`/`max` (numeric bounds), `pattern` (regex). All fields are optional; unset ones fall back to the raw env var name |
| `env_replace_secrets.<VAR>` | Vars that cannot be generated automatically and must be copied in by hand after install — typically a Keycloak client secret, with a `hint` pointing at where to find it in the Keycloak Admin UI |
| `integration.keycloak.clients` | Paths to Keycloak client export JSON files imported on install |
| `integration.keycloak.client_mappers` | Paths to mapper JSON applied to an *existing* client (e.g. the LibreChat client's audience mapper) rather than a new client of this add-on's own |
| `integration.librechat` | Path to the MCP server registration consumed by LibreChat |
| `integration.nginx` | Path to an optional Nginx vhost fragment, for add-ons that publish a browser-facing UI |

Readers ignore unknown manifest keys, so new fields can be introduced without breaking older add-ons.

`python tools/components.py check` also holds a manifest to the files next to it: `name` equals the directory name, every `env_prompts` and `env_replace_secrets` key is in `.env.example` (active or commented out), every path under `integration` exists, `networks.app_network` is the same string as a network `name:` in `docker-compose.yml`, and every `${VAR}` that Compose substitutes is in `.env.example`, except the variables the core `.env` hands to every add-on (`COMPOSE_PROJECT_NAME`, `GID`, `HOST_IP`, `PAPAIA_PROJECT`, `UID`).

### The add-on changelog (`<addon>/CHANGELOG.md`)

Each add-on keeps its own changelog next to its manifest. Tooling such as papaia-manager shows the section of a new version before an operator updates, so the headings are a contract:

- [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style: one `## [X.Y.Z] - YYYY-MM-DD` heading per add-on version, newest first, each version once. An optional `## [Unreleased]` section may sit on top.
- The newest dated section is the `version` in `papaia-app.yaml`.
- A section may carry a `### Upgrade notes` subsection for steps the operator has to do by hand (new required variables, data migrations, files to move). Say what to do, not what the code does.
- The section is written in the release-cut PR, together with the version bump, like the repository `CHANGELOG.md`. Feature and fix PRs do not touch it.

`python tools/components.py check` fails for a missing file, a heading that does not parse, versions out of order or repeated, and a newest section that is not the manifest version. `release-check` fails when an add-on's version moved without a section for the new version.

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
- **PHP** (`mediawiki/runtime/`): tabs, following the MediaWiki coding conventions the code extends
- **Manifests and changelogs**: `python tools/components.py check` also validates each manifest against its add-on directory and each `CHANGELOG.md` (see the contracts above)
- **Component versions**: `python tools/components.py check` must pass; after changing an image tag in a Compose file, run `python tools/components.py fix` instead of editing the manifest, `.env.example` and README by hand. The unit tests run with `python -m unittest discover -s tools`
- Once a Python `src/` is introduced, `ruff check .` and the license-check workflow (see below) start applying

---

## Versioning & releases

- Semantic Versioning; tags follow `vX.Y.Z` and mark a snapshot of the whole catalog.
- Each add-on carries its own SemVer `version` in its manifest, independent of the catalog tag. It says *that* the add-on changed; the `components` list says *what* it runs. Versions are bumped in the release-cut PR, not in feature PRs, by these rules:

  | Change since the last release | Bump |
  |---|---|
  | Component patch or minor update without a configuration change | patch |
  | New env variables, manifest or integration changes, new user-facing behaviour | minor |
  | Component major update, data migration, or any breaking change to env, manifest or integration | major |
  | Documentation only | none |

  An add-on whose image tag embeds the add-on version (`mediawiki`) takes the new tag from the same bump. The bump comes with the add-on's `CHANGELOG.md` section for the new version.
- At release-cut time, `python tools/components.py release-check --from <last tag>` fails for an add-on that changed without a version bump, whose version moved without a change, or whose version moved without a `CHANGELOG.md` section for it; `python tools/components.py report --from <last tag>` prints the add-on and component matrix for `CHANGELOG.md` and the release notes.
- Pin policy: primary and Fidonis components use an exact tag (`3.0.5`, not `3` or `latest`). A sidecar may use a floating tag (`postgres:18`) only when its component entry says `pinned: false`, so the table shows that the deployed version depends on the pull date.
- `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); it is updated by hand in a dedicated `docs:` PR at release-cut time, not by every feature/fix PR.
- [release-drafter](https://github.com/release-drafter/release-drafter) auto-drafts GitHub Release notes from merged PR titles on every push to `main`; publishing that draft is what creates the release tag.

---

## Security boundaries

- **Never commit `.env` files.** Every add-on ships only `.env.example`, with placeholder values (`.gitignore` enforces this).
- **Never commit generated secrets** — Keycloak realm exports, client secrets, or any other credential produced by an actual install.
- Variables marked `secret: true` in a manifest are either auto-generated on install or, for `env_replace_secrets` entries, filled in by hand from the Keycloak Admin UI after the client import — never hard-coded.
- **No copyleft dependencies** (GPL/LGPL/AGPL/EUPL/SSPL and similar) — enforced by the license-check workflow once a Python `src/` project exists.
