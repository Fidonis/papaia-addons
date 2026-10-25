# Changelog

All notable changes to the `qdrant` add-on are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [2.0.0] - 2026-10-24

### Added
- `components` in the manifest, naming the software the add-on bundles
  (Qdrant v1.19.0, qdrant-mcp-rbac 0.3.0).
- `QDRANT_MCP_HOST` is documented in `.env.example`.

### Changed
- **Breaking:** the Docker bridge is scoped to the deployment,
  `${PAPAIA_PROJECT:-papaia}-qdrant-net`, so two papAIa deployments on one host
  no longer share it. The add-on declares `requires.addon_api: 2`.

### Upgrade notes
- Needs papaia 1.3.0 or newer, the first core serving add-on API 2.
- A default single-stack host keeps the network name `papaia-qdrant-net`, so
  nothing has to be migrated there. Where `PAPAIA_PROJECT` is not `papaia`, the
  network is renamed; an attachment wired by hand has to use the new name
  (`papaia-ctl` writes it for you).

## [1.1.0] - 2026-09-04

### Changed
- No change to the add-on itself; the version moved with catalog release 1.1.0.

## [1.0.0] - 2026-08-18

### Added
- A self-hosted Qdrant vector database with an OIDC/RBAC-secured MCP server,
  giving LibreChat per-user vector search.
