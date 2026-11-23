# Developing papaia-addons

Structure, contracts and conventions for everyone who changes an add-on or the tooling in this repository.

---

## What this project does

`papaia-addons` is a catalog of add-ons for the [papaia](https://github.com/Fidonis/papaia) stack. Each add-on is an independently-installable bundle — a Compose service (or set of services), a manifest, and the integration assets needed to wire it into a running deployment — with no shared code between add-ons.

Current catalog:

| Add-on | What it adds |
|---|---|
| `mediawiki` | A MediaWiki knowledge base with Keycloak login and group sync, namespace-level access tiers, and the MediaWiki MCP server, through which LibreChat works in the wiki as the signed-in user within the same tiers |
| `n8n` | n8n workflow automation behind an oauth2-proxy Keycloak SSO gate — the engine is never published, and only the webhook paths bypass the gate |
| `paperless` | Paperless-ngx document management, an OIDC/RBAC MCP server through which LibreChat reads documents as the signed-in user, and Keycloak SSO login for Paperless users |
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
│   ├── papaia-app.yaml        # Manifest: identity, compatibility, networks, env_prompts, integration, exposes
│   ├── docker-compose.yml     # Compose fragment(s) for this add-on
│   ├── .env.example           # Every declared env var with a placeholder value
│   ├── README.md              # What it does, a services table, the minimum papaia version
│   ├── CHANGELOG.md           # One section per add-on version, with upgrade notes
│   └── integration/
│       ├── infra/keycloak/*.json        # Client export(s) + audience mapper(s), imported on install (not for what `exposes:` declares)
│       ├── ai/librechat/librechat.yaml  # MCP server registration for LibreChat (only add-ons without an `exposes:` tool server)
│       └── infra/nginx/*.conf           # Optional vhost fragment (public-facing add-ons only)
├── catalogue.yaml             # Module catalogue: add-ons from their own repositories, pinned by version, commit and digest
├── docs/
│   └── development.md         # This document
├── tools/
│   ├── catalogue.py           # Checks, pins, verifies and fetches the entries of catalogue.yaml
│   ├── components.py          # Keeps component versions, .env.example and README tables equal to Compose; checks manifests and changelogs
│   ├── exposes.py             # Holds the `exposes:` section to the add-on directory; compares generation comments with the core
│   ├── root_markdown.py       # Allows only README.md and CHANGELOG.md as Markdown files at the repository root
│   ├── image_refs.py          # Every image default pinned by tag and digest
│   ├── scan_gate.py           # Decides a Trivy report against security/exceptions.yaml
│   ├── word_check.py          # Denied terms in the lines a revision range adds (papaia-kit term list)
│   ├── ci/                    # pyproject.toml + uv.lock: the pinned Python tool set of CI, including papaia-kit
│   ├── fixtures/              # Test inputs, e.g. the seeded critical scan report
│   └── test_*.py              # Unit tests for the tools
├── security/exceptions.yaml   # Accepted vulnerability findings, each with reason and expiry (≤ 90 days)
├── vendor/papaia-kit/         # The papaia-kit release wheel CI uses, with its LICENSE (MIT); kit.lock pins it
├── license-records.json       # License records of the base images the mediawiki image is built from
├── license-exceptions.toml    # License exceptions (papaia-kit format), see "Licenses" below
├── CHANGELOG.md
└── README.md
```

`qdrant-ingest` additionally ships `jobs.example.yaml`, a template for the ingest job definitions the operator web interface manages. `mediawiki` additionally ships `image/` (the build context of its wiki image, published as `ghcr.io/fidonis/papaia-mediawiki:<MediaWiki version>-<add-on version>` by the `mediawiki-image` workflow; a change there needs a new add-on version), `runtime/` (the env-driven `LocalSettings.php`, entrypoint and OAuth bootstrap, mounted read-only into the wiki container), `mcp/` (configuration and start script of the MCP server), `settings.d/` (site-specific settings) and `tests/` (the access-tier leak checks, `tests/run.sh`). `n8n` additionally ships `nginx.conf`, the config template of its logout shim (swappable via `N8N_PROXY_CONF`).

---

## The manifest contract (`papaia-app.yaml`)

Every add-on manifest carries the same shape:

| Field | Meaning |
|---|---|
| `name`, `version`, `addon_repo` | Identity — `addon_repo` is the add-on's own upstream repository name |
| `requires.addon_api` | The contract generation(s) this add-on is built against; checked against the core's supported window by cores that read it. A manifest with `exposes:` declares a list of generations from 7 on (`[12]` for this release), and the comments of the `requires:` block give the meaning of each generation they name, worded exactly as the core's `ADDON_API` table |
| `papaia_compat` | SemVer-range fallback (`>=x.y.z`) for cores that don't read `requires` yet |
| `description` | One-line summary, shown in installer UIs |
| `components[]` | The software this add-on packages, next to `version`, which counts the add-on bundle itself. Per entry: `id`, `name`, `role` (`primary`\|`sidecar`), `kind` (`upstream`\|`fidonis`), `image_var` (the Compose variable whose default names the image), `version` (that default's tag, quoted), `source` (https URL) and, for a floating tag only, `pinned: false`. The Compose default is the source of truth; `tools/components.py` keeps `version`, the image lines of `.env.example` and the README table equal to it. Consumers such as papaia-manager read it to show what an add-on runs |
| `networks.app_network` | The Docker network name this add-on's own services join. May be a literal, or (with `requires.addon_api: 2`) a `${PAPAIA_PROJECT:-papaia}-<name>-net` template that `papaia-ctl` resolves against the core `.env` so the bridge is scoped per deployment — use the identical string in the add-on's own compose `networks.<key>.name` |
| `networks.attach` | Core services this add-on's containers additionally attach to. Validated against the core's own Compose services — an add-on cannot list another add-on here |
| `local_ca_env` | Per-service list of env vars pointing at the bundled Keycloak CA cert (mounted from `$PAPAIA_CONFIG_DIR/certs`); `papaia-ctl` clears them via a generated override when `auth_provider=external_oidc`, so the add-on falls back to the system CA bundle instead of failing on a missing cert |
| `env_prompts.<VAR>` | Per-variable metadata: `label` (prompt text), `hint` (secondary explanation), `default` / `default_from_core` (pulls a value from the core's own `.env`), `type` (`text`\|`integer`\|`url`\|`decimal`), `secret` (forces masking, or opts out of the name heuristic), `min`/`max` (numeric bounds), `pattern` (regex). All fields are optional; unset ones fall back to the raw env var name |
| `env_replace_secrets.<VAR>` | Vars that cannot be generated automatically and must be copied in by hand after install — typically a Keycloak client secret, with a `hint` pointing at where to find it in the Keycloak Admin UI |
| `integration.keycloak.clients` | Paths to Keycloak client export JSON files imported on install. Not for the client of an `exposes:` audience: the core creates that resource-server client |
| `integration.keycloak.client_mappers` | Paths to mapper JSON applied to an *existing* client (e.g. the LibreChat client's audience mapper) rather than a new client of this add-on's own. Not for the audience of an `exposes:` tool server (`kind: mcp`, identity `person`): the core generates its mapper on the `librechat` client |
| `integration.librechat` | Path to the MCP server registration consumed by LibreChat. Only for add-ons without an `exposes:` tool server for persons: the core renders the chat entry of such a server (named after its audience) from the catalog |
| `integration.nginx` | Path to an optional Nginx vhost fragment, for add-ons that publish a browser-facing UI |
| `exposes` | (`addon_api` 7) The one description of what the add-on offers, validated against the `exposes.v1` schema of papaia-kit: per surface its `id`, `kind`, internal `address`, `audience`, accepted `identity`, `header_identity` (`true` when the service takes a person's identity from a request header; the core then never publishes its port), `operations[]` (every tool of the MCP server with effect, retry class, input and output schema, highest data class), the standard operations `describe_access` and `check_access`, `requires.addon_api`, `volumes[]` (every named Compose volume as one backup item), `export.exit` and `storage` (personal data categories with retention and erasure path). The core renders the platform catalog from it. Data classes, names and plain-text rules come from papaia-kit; an add-on never keeps its own list |

Readers ignore unknown manifest keys, so new fields can be introduced without breaking older add-ons.

`python tools/components.py check` also holds a manifest to the files next to it: `name` equals the directory name, every `env_prompts` and `env_replace_secrets` key is in `.env.example` (active or commented out), every path under `integration` exists, `networks.app_network` is the same string as a network `name:` in `docker-compose.yml`, and every `${VAR}` that Compose substitutes is in `.env.example`, except the variables the core `.env` hands to every add-on (`COMPOSE_PROJECT_NAME`, `GID`, `HOST_IP`, `PAPAIA_PROJECT`, `UID`).

Through `tools/exposes.py` it also checks that no manifest carries `@UNDECIDED@` and, for a manifest with `exposes:`, what the section claims about the add-on directory: `requires.addon_api` as above and repeated unchanged in every entry, unique entry ids and operation names, `address.service` a Compose service, no shipped copy of what the core generates from an entry (a Keycloak client whose id is the entry's `audience`; next to a `kind: mcp` entry for persons an `integration.librechat` fragment or a `librechat` mapper for its audience), `resource.id_field` a property of the operation's input schema, every named Compose volume exactly one item of `volumes[]`, `storage.secrets[].location` one of those volumes, and `export.exit.schema` a JSON file in the add-on directory. Schema, names and data classes are left to the manifest conformance check of papaia-kit. `python tools/exposes.py generations --core <core checkout>` compares the generation comments of every manifest with the core's `ADDON_API` table.

### The module catalogue (`catalogue.yaml`)

Studio (`papaia-studio`), Data (`papaia-data`), Flow (`papaia-flow`), the assistant (`papaia-assist`) and the SQL Server source (`mssql-mcp`) ship their add-on bundle in their own repository under `deploy/papaia-addon/`. `catalogue.yaml` (schema `papaia.addon-catalogue/1`) lists them; an entry never names an add-on directory of this repository.

| Field | Meaning |
|---|---|
| `name` | The add-on name, equal to `name` in the bundle's manifest |
| `title` | Display name (at most 40 characters) |
| `version`, `description` | Copied from the bundle's manifest at the pinned commit |
| `requires` | `addon_api` and `papaia_compat` of the manifest; `addons` as `{name, optional}` (plus `version` where the manifest names a range), whatever spelling the manifest uses |
| `source` | `repo` (https URL without credentials, port or query), `commit` (full commit id), `path` (bundle path, relative, no `..`) |
| `digest` | Bundle digest `sha256:<hex>`: SHA-256 over one line `<mode> <sha256 of the file> <path>` per file of the bundle at the commit, sorted by path; mode `100644` or `100755`. Symbolic links and submodules are refused. The compose file is part of the bundle, so the image pins of the module are covered too |

Unknown fields are refused. The file is generated: `python tools/catalogue.py pin <name> --repo <clone>` writes an entry from a clone of the module repository (its `HEAD`, or `--commit`; a new entry also needs `--url` and `--title`). `python tools/components.py check` runs `python tools/catalogue.py check`, which fails for an invalid entry and for a file that is not in the form `pin` writes.

| Command | What it does |
|---|---|
| `python tools/catalogue.py list [--json]` | Name, version, description and prerequisites of every entry |
| `python tools/catalogue.py verify --repo <name>=<clone> ...` | Compares entries with their bundle at the pinned commit; fails on any difference |
| `python tools/catalogue.py fetch <name> --repo <clone> --into <dir>` | Writes the pinned bundle into a new or empty directory after its digest matched; writes nothing otherwise |
| `python tools/catalogue.py matrix --core <core checkout> [--suite X.Y.Z]` | Each `version` inside its cell of the core's `compat/suite.yaml` and each `addon_api` generation inside the suite's window |

At release-cut time the entries are pinned to the tagged commits of the module repositories, and `verify` and `matrix` run against them.

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
- **Repository root**: the only Markdown files at the root are `README.md` and `CHANGELOG.md`; `python tools/root_markdown.py` fails for any other, and CI runs it. Further documentation goes under `docs/` or into the add-on directory it belongs to
- **Image pins**: `python tools/image_refs.py check` (see the pin policy below)
- The CI tool set is pinned in `tools/ci/uv.lock`; run any of the above through it with `UV_PROJECT=tools/ci uv run --locked <command>` to use the exact CI versions (ruff, yamllint, PyYAML, papaia-kit)
- **papaia-kit**: the repository vendors one published kit release, `vendor/papaia-kit/<version>/` with the release wheel and its `LICENSE`. `kit.lock` names the version, the kit commit, its `source_date_epoch` and the wheel's sha256 exactly as the kit's release record (`SHA256SUMS`) states them; `tools/ci/pyproject.toml` pins the same version with the wheel as path source and `tools/ci/uv.lock` carries its sha256, so CI installs nothing else. To move to a new kit release, replace the folder with the release files, update `kit.lock`, the pin and the path in `tools/ci/pyproject.toml`, run `uv lock` in `tools/ci` and the expected record in `tools/test_kit_lock.py`, which also fails when a file outside the changelogs names another kit version. The mediawiki image label `de.fidonis.platform-kit` is read from `kit.lock`
- Once a Python `src/` is introduced, the license-check workflow for `src/` starts applying as well

---

## Continuous integration

`ci.yml` runs on every pull request, every push to `main` or `releases/**`, every merge queue run and nightly against the default branch; `mediawiki-image.yml` runs when the image, its scan inputs or the workflow change.

| Job | What it checks |
|---|---|
| Yamllint, Shellcheck, Ruff, Hadolint | Lint; hadolint runs as a container pinned by digest over every tracked `Dockerfile` |
| Component versions | `components.py check`, `image_refs.py check`, the unit tests of `tools/` |
| Manifest conformance | `papaia-kit conformance manifest` for every manifest with `exposes:`, with its compose file |
| License check | `papaia-kit licenses check` over the CI tool set (`tools/ci/uv.lock`, build scope) and the base images in `license-records.json` |
| Word check and commit metadata | Denied terms of papaia-kit in the lines the change adds (`word_check.py`), counted from the merge base; author, committer, sign-off and wording of its commits (`papaia-kit repo commits`) |
| Vulnerability scan | Trivy over every pinned image default of the delivered add-ons (`image_refs.py list`), each for `linux/amd64` and `linux/arm64`, and over the CI tool set (`tools/ci/uv.lock`), each through `scan_gate.py`; nightly too, so a new CRITICAL finding with a fix fails the scheduled run |
| Secret scan (full history) | gitleaks, pinned by version and sha256, over every commit; `.gitleaks.toml` allows only the pinned extension commits of the mediawiki image build |
| Conventional Commit PR title | The PR title |
| mediawiki image: tag and pin | The compose default is a published tag with the digest the registry serves; a newer derived tag may be unpinned only while it is not published |
| mediawiki image: build and scan | Build per platform (`linux/amd64`, `linux/arm64`), each scanned with Trivy through `scan_gate.py`; access-tier checks against the amd64 build; a seeded critical report must fail the gate |
| mediawiki image: publish | Push by digest without a tag; scan and gate of each platform manifest; one CycloneDX SBOM per platform attached as a signed attestation; cosign signature of the index and its manifests with the release key into `ghcr.io/fidonis/papaia-signatures`; verification; only then the tag. A published tag is verified instead and fails the job if it lacks signature or SBOM. Images carry the label `de.fidonis.platform-kit=<kit.lock version>` |

Scan threshold (`scan_gate.py`): a CRITICAL or HIGH finding with a fixed version fails unless `security/exceptions.yaml` lists it (id, package, image, severity, reason, added, expiry at most 90 days later, approval); findings without a fix are listed and do not fail. An entry whose `approved_by` starts with `pending` waives nothing until it is countersigned. An expired entry fails every scan.

Signing needs the secrets `COSIGN_PRIVATE_KEY` and `COSIGN_PASSWORD` and the variable `COSIGN_PUBLIC_KEY` (the release key the core verifies against), and write access of this repository to the package `papaia-signatures`. Without the secrets and the variable the publish job stops before anything is pushed. Missing write access cannot be tested without writing, so it fails the signing step; the build then exists only as an untagged digest, and no tag is created.

The static conformance suite of papaia-kit reads Python and JavaScript/TypeScript server code; this repository ships none (servers come as images from their own repositories), so it has no input here.

### Licenses

The policy is the one of papaia-kit. This repository builds one image, `papaia-mediawiki`, which is MediaWiki itself: the upstream image `docker.io/library/mediawiki` (GPL-2.0-or-later) with extensions fetched at pinned commits and configuration. It runs as its own container and is reached over HTTP only, so the policy's service scope applies, where GPL needs an exception whose obligations are met: upstream code stays unmodified and `mediawiki/image/Dockerfile` names the upstream image and every extension commit, so the corresponding source stays available. `license-exceptions.toml` records this decision; `license-records.json` lists the base images (Composer runs in the build stage only). Exceptions expire and are renewed with a new review.

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
- Pin policy: every image default names its release by tag **and** digest, `name:tag@sha256:<digest>` (`docker.io/library/postgres:18.7@sha256:…`). The tag names the patch level (`3.0.5`, `8.34.0`); major.minor (`18.7`) only for an upstream that has no patch levels, listed in `MINOR_RELEASE_REPOSITORIES` of `tools/components.py` (PostgreSQL). It never names a moving name (`latest`, `dev`, `main`, `nightly`, `18`, `9-alpine`); the digest is what Docker pulls, so a re-pushed tag changes nothing. `python tools/image_refs.py check` enforces it for every delivered add-on (`n8n` is outside the delivered set and not checked). To move an image, take the digest the registry serves for the new tag (`docker buildx imagetools inspect --raw <name>:<tag> | sha256sum`), write `tag@sha256:<digest>` into the Compose default and run `python tools/components.py fix`. `pinned: false` remains only for a tag without digest that floats, which the pin check rejects for delivered add-ons.
- `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); it is updated by hand in a dedicated `docs:` PR at release-cut time, not by every feature/fix PR.
- [release-drafter](https://github.com/release-drafter/release-drafter) auto-drafts GitHub Release notes from merged PR titles on every push to `main`; publishing that draft is what creates the release tag.

---

## Security boundaries

- **Never commit `.env` files.** Every add-on ships only `.env.example`, with placeholder values (`.gitignore` enforces this).
- **Never commit generated secrets** — Keycloak realm exports, client secrets, or any other credential produced by an actual install.
- Variables marked `secret: true` in a manifest are either auto-generated on install or, for `env_replace_secrets` entries, filled in by hand from the Keycloak Admin UI after the client import — never hard-coded.
- **No copyleft dependencies** (GPL/LGPL/AGPL/EUPL/SSPL and similar) in own code — enforced by the license-check workflow once a Python `src/` project exists. Separately running third-party services follow the service scope of the papaia-kit policy (see "Licenses").
- **Vulnerabilities**: images are scanned on every change and nightly; accepted findings live in `security/exceptions.yaml` with an expiry (see "Continuous integration").
