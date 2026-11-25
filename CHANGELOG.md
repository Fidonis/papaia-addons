# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are generated automatically by [release-drafter](https://github.com/release-drafter/release-drafter)
based on merged pull requests; this file mirrors the published releases.

## [Unreleased]

### Security
- **Third-party images re-pinned on the scan of 2026-11-25** (both
  platforms): Paperless-ngx 3.5.1 (`paperless` 2.2.0, adds
  `PAPERLESS_ALLOWED_HOSTS`), Apache Tika 3.4.0.0 (`paperless` 2.2.0,
  `qdrant-ingest` 2.1.1; no CRITICAL or HIGH finding left), the current
  digest of PostgreSQL 18.7, and MediaWiki MCP Server 0.21.0 (`mediawiki`
  1.0.1, unreleased). Gotenberg 8.40.0, MariaDB 11.8.9 and Qdrant v1.20.2 stay
  on the newest release of their line. `security/exceptions.yaml` is rewritten
  for these pins: one pending entry per remaining finding, none for a finding
  that is gone; `tools/test_scan_gate.py` checks that every entry names the
  pinned tag it was reviewed for and that no id, package and image appears
  twice.
- **Wiki image `1.43.11-1.0.2`** (`mediawiki` 1.0.2, unreleased): rebuilt on
  the current digest of MediaWiki 1.43.11 with `libzstd1` raised to the
  Debian security version (CVE-2026-124851); no CRITICAL or HIGH Debian
  finding with a fix is left in the image. `tools/test_mediawiki_image.py`
  holds the image's Debian security pins to exact `--only-upgrade` versions,
  each named next to its advisory.

### Changed
- **CI tool set on the final papaia-kit 0.1.0**, the kit's first release
  (MIT license). `vendor/papaia-kit/0.1.0/` holds the release wheel and its
  `LICENSE`; `kit.lock` names the release record (version, kit commit
  `849e344`, `source_date_epoch`, wheel sha256). An earlier build carried the
  same version number, so only commit and checksum tell them apart;
  `tools/test_kit_lock.py` pins them. The checks and the manifest contract are
  unchanged; the mediawiki image label `de.fidonis.platform-kit` reads `0.1.0`.
- **Start boundary of the commit metadata check.** `commit-boundary.toml`
  names `e0de06f` by its full SHA with the reason for the boundary;
  `papaia-kit repo commits` leaves that commit and its ancestors out and
  checks every later commit for author, committer, sign-off and wording.

## [1.2.0] - 2026-10-24

### Added
- **mediawiki add-on** (`1.0.0`): a MediaWiki 1.43 knowledge base with Keycloak
  login and group sync, namespace-level access tiers, and the MediaWiki MCP
  Server, through which LibreChat works in the wiki as the signed-in user
  within the same tiers. Tier content, including uploaded files, stays out of
  search, listings, feeds and watchlist mails for everyone outside the tier,
  which the add-on's own `tests/run.sh` checks. The wiki image is published as
  `ghcr.io/fidonis/papaia-mediawiki:<MediaWiki version>-<add-on version>` by
  the `mediawiki-image` workflow. Requires papaia ≥ 1.3.0.
- **n8n add-on** (`1.0.0`): n8n workflow automation behind an oauth2-proxy
  Keycloak SSO gate. The engine is never published; only the webhook paths
  bypass the gate. The add-on ships no MCP server. Requires papaia ≥ 1.3.0.
- **`components` list in every add-on manifest**, naming the software an
  add-on bundles next to its own `version`. `tools/components.py` keeps the
  list equal to the image tags in `docker-compose.yml`, mirrors it into the
  commented image lines of `.env.example` and a generated table in each
  add-on README, and prints the add-on and component matrix and the
  version-bump check used at release-cut time. CI runs its check and tests.
  `AGENTS.md` documents the manifest key, the per-add-on version bump rules
  and the pin policy.
- **qdrant-ingest** prompts for `QI_EXT_PORT`, the published host port.

### Changed
- **Breaking, existing add-ons:** `paperless`, `paperless-connect`, `qdrant`,
  `qdrant-connect` and `qdrant-ingest` (all now `2.0.0`) scope their Docker
  bridge to the deployment, `${PAPAIA_PROJECT:-papaia}-<add-on>-net`, so two
  papAIa deployments on one host no longer share it. They declare
  `requires.addon_api: 2` and need papaia ≥ 1.3.0, the first core serving
  that generation (`papaia_compat` and each README now say so). A default
  single-stack host keeps the name `papaia-<add-on>-net`, so nothing has to
  be migrated there. The new `mediawiki` and `n8n` add-ons are built the same
  way.
- **qdrant-ingest** publishes its control-plane port by default, like the
  other add-ons: `HOST_IP:QI_EXT_PORT:8300` (`QI_EXT_PORT` defaults to `8300`,
  the host falls back to loopback when `HOST_IP` is unset). REST, MCP and the
  web interface share the port, and `QI_UI_PUBLIC_URL` may name it directly
  instead of a proxy host. Existing installs need no `.env` change; where
  `HOST_IP` is `0.0.0.0` the port is now reachable from the network, so keep
  it on a trusted interface or behind a firewall.

### Fixed
- The `qdrant-ingest` README no longer claims that `nomic-embed-text` is
  defined in the core LiteLLM configuration. The core ships no models, so an
  embedding model has to be added in the LiteLLM Admin UI first.
- The image lines of `paperless-connect/.env.example` and
  `qdrant-ingest/.env.example` named older images than Compose runs
  (`paperless-mcp-rbac` `0.1.0`, `qdrant-ingest` `0.1.1`).

### Add-on versions

| Add-on | Version | Components |
|---|---|---|
| mediawiki | 1.0.0 (new) | MediaWiki 1.43.11-1.0.0; MediaWiki MCP Server 0.19.0; MariaDB 11.8.9 |
| n8n | 1.0.0 (new) | n8n 2.36.6; oauth2-proxy v7.15.3; nginx 1.31-alpine; PostgreSQL 18 |
| paperless | 1.1.0 → 2.0.0 | Paperless-ngx 3.0.5; paperless-mcp-rbac 0.1.1; Valkey 9-alpine; PostgreSQL 18; Gotenberg 8.34; Apache Tika 3.2.3.0 |
| paperless-connect | 1.1.0 → 2.0.0 | paperless-mcp-rbac 0.1.1 |
| qdrant | 1.1.0 → 2.0.0 | Qdrant v1.19.0; qdrant-mcp-rbac 0.3.0 |
| qdrant-connect | 1.1.0 → 2.0.0 | qdrant-mcp-rbac 0.3.0 |
| qdrant-ingest | 1.1.0 → 2.0.0 | qdrant-ingest 0.3.0; Apache Tika 3.2.3.0 |

## [1.1.0] - 2026-09-04

### Added
- **qdrant-ingest add-on**, raised to image `0.3.0`, replacing the single
  hard-wired Qdrant instance and default embedding model with named
  connections (`connections.yaml`, API keys encrypted at rest) and a required
  `target.connection` per job. In-place upgrades need manual migration: set
  `QI_CONNECTIONS_SECRET`, drop the old `QI_QDRANT_*`/`QI_EMBEDDING_MODEL`
  variables, create `connections.yaml`, and add `target.connection` to every
  job (breaking change).

### Fixed
- **paperless-connect and qdrant-connect add-ons** no longer hardcode their
  MCP server's `SSL_CERT_FILE` to the local CA, which broke OIDC token
  verification behind a publicly-trusted Keycloak certificate. The cert path
  is now configurable via `PAPERLESS_MCP_SSL_CERT_FILE` /
  `QDRANT_MCP_SSL_CERT_FILE` (default empty).

## [1.0.0] - 2026-08-18

### Added
- **paperless add-on**, raised to Paperless-ngx 3.0.5, with Keycloak OIDC
  integration, local-CA certificate support for the add-on's OIDC calls,
  installer hints for the Keycloak client secret, and generated `addon_api`
  contract metadata.
- **paperless-connect add-on**, wiring an existing standalone Paperless-ngx
  instance into papaia instead of running a bundled one.
- **qdrant and qdrant-connect add-ons**, adding a self-hosted Qdrant vector
  database and a connector for an existing standalone instance, each with an
  OIDC/RBAC-secured MCP server for LibreChat.
- **qdrant-ingest add-on**, scheduled multi-source document ingestion into an
  existing Qdrant, including its operator web interface for managing ingest
  jobs.
- Add-on manifests declare a variable's type and whether it is a secret, so
  consuming tooling can render the right input control.

### Changed
- Renamed the "extension" concept to "add-on" across manifests, docs and
  directory names (breaking change).
- Dropped the homepage integration seam from add-on manifests.
