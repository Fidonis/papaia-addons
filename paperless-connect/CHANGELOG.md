# Changelog

All notable changes to the `paperless-connect` add-on are documented in this
file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [2.0.0] - 2026-10-24

### Added
- `components` in the manifest, naming the software the add-on bundles
  (paperless-mcp-rbac 0.1.1).
- `PAPERLESS_MCP_HOST` is documented in `.env.example`.

### Changed
- **Breaking:** the Docker bridge is scoped to the deployment,
  `${PAPAIA_PROJECT:-papaia}-paperless-connect-net`, so two papAIa deployments
  on one host no longer share it. The add-on declares `requires.addon_api: 2`.

### Fixed
- The image line in `.env.example` named an older image than Compose runs.

### Upgrade notes
- Needs papaia 1.3.0 or newer, the first core serving add-on API 2.
- A default single-stack host keeps the network name
  `papaia-paperless-connect-net`, so nothing has to be migrated there. Where
  `PAPAIA_PROJECT` is not `papaia`, the network is renamed; an attachment wired
  by hand has to use the new name (`papaia-ctl` writes it for you).

## [1.1.0] - 2026-09-04

### Fixed
- The MCP server's `SSL_CERT_FILE` is no longer hard-wired to the local CA,
  which broke OIDC token verification behind a publicly trusted Keycloak
  certificate. The path is set through `PAPERLESS_MCP_SSL_CERT_FILE`
  (default empty).

## [1.0.0] - 2026-08-18

### Added
- The MCP and Keycloak layer of `paperless`, pointed at an existing standalone
  Paperless-ngx instance instead of a bundled one.
