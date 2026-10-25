# Changelog

All notable changes to the `paperless` add-on are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [2.0.0] - 2026-10-24

### Added
- `components` in the manifest, naming the software the add-on bundles
  (Paperless-ngx 3.0.5, paperless-mcp-rbac 0.1.1, Valkey 9-alpine,
  PostgreSQL 18, Gotenberg 8.34, Apache Tika 3.2.3.0).
- `TIMEZONE` and `PAPERLESS_MCP_HOST` are documented in `.env.example`.

### Changed
- **Breaking:** the Docker bridge is scoped to the deployment,
  `${PAPAIA_PROJECT:-papaia}-paperless-net`, so two papAIa deployments on one
  host no longer share it. The add-on declares `requires.addon_api: 2`.

### Upgrade notes
- Needs papaia 1.3.0 or newer, the first core serving add-on API 2.
- A default single-stack host keeps the network name `papaia-paperless-net`,
  so nothing has to be migrated there. Where `PAPAIA_PROJECT` is not
  `papaia`, the network is renamed; an attachment wired by hand has to use the
  new name (`papaia-ctl` writes it for you).

## [1.1.0] - 2026-09-04

### Changed
- No change to the add-on itself; the version moved with catalog release 1.1.0.

## [1.0.0] - 2026-08-18

### Added
- Paperless-ngx 3.0.5 with Keycloak OIDC login, local-CA certificate support
  for the add-on's OIDC calls, installer hints for the Keycloak client secret
  and the OIDC/RBAC MCP server for LibreChat.
- Manifest metadata declaring each variable's type and whether it is a secret,
  so installer UIs render the right input control.

### Changed
- The homepage integration seam is gone from the manifest.
