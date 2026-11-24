# Changelog

All notable changes to the `qdrant` add-on are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [2.1.0] - 2026-11-24

### Security
- Qdrant v1.19.0 -> v1.20.2. No v1.19 release removes the CRITICAL and HIGH
  findings with a fix of v1.19.0 (v1.19.2, the newest patch, still has 16 of
  them, 2 critical); v1.20.2 leaves one (zlib1g, see
  `security/exceptions.yaml`).
- qdrant-mcp-rbac 0.3.0 -> 0.3.1, a patch release of the same line on a fixed
  base image and dependency set. Until 0.3.1 is published, the default is its
  signed release candidate from a local test registry, the same reference as in
  the add-on `qdrant-connect`.

### Upgrade notes
- Configuration and environment are unchanged, and the existing storage volume
  is used as it is: Qdrant supports the upgrade from the previous minor version
  in place. A downgrade to v1.19 is not supported, so take a snapshot before
  the upgrade if a rollback must stay possible.
- Qdrant upgrades one minor version at a time: an installation still on a
  version older than v1.19 first moves to the previous add-on version.
- Qdrant v1.20 writes log timestamps in the time zone of `TZ` (UTC when unset).

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
