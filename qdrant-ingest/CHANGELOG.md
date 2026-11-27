# Changelog

All notable changes to the `qdrant-ingest` add-on are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [2.0.0] - 2026-10-24

### Added
- `components` in the manifest, naming the software the add-on bundles
  (qdrant-ingest 0.3.0, Apache Tika 3.2.3.0).
- `QI_EXT_PORT`, the published host port, is asked for on install.

### Changed
- **Breaking:** the Docker bridge is scoped to the deployment,
  `${PAPAIA_PROJECT:-papaia}-qdrant-ingest-net`, so two papAIa deployments on
  one host no longer share it. The add-on declares `requires.addon_api: 2`.
- The control-plane port is published by default, like the other add-ons:
  `HOST_IP:QI_EXT_PORT:8300` (`QI_EXT_PORT` defaults to `8300`; the host falls
  back to loopback when `HOST_IP` is unset). REST, MCP and the web interface
  share the port, and `QI_UI_PUBLIC_URL` may name it directly instead of a
  proxy host.

### Fixed
- The image line in `.env.example` named an older image than Compose runs.

### Upgrade notes
- Needs papaia 1.3.0 or newer, the first core serving add-on API 2.
- A default single-stack host keeps the network name
  `papaia-qdrant-ingest-net`, so nothing has to be migrated there. Where
  `PAPAIA_PROJECT` is not `papaia`, the network is renamed.
- Existing installs need no `.env` change. Where `HOST_IP` is `0.0.0.0` the
  port is now reachable from the network, so keep `HOST_IP` on a trusted
  interface or restrict the port with a firewall.

## [1.1.0] - 2026-09-04

### Changed
- **Breaking:** the image is raised to `0.3.0`, which replaces the single
  hard-wired Qdrant instance and default embedding model with named
  connections (`connections.yaml`, API keys encrypted at rest) and a required
  `target.connection` per job.

### Upgrade notes
- The image no longer reads `QI_QDRANT_URL`, `QI_QDRANT_API_KEY` or
  `QI_EMBEDDING_MODEL`. Before restarting: set `QI_CONNECTIONS_SECRET` in the
  bundle `.env` (generated on a fresh install), create
  `catalog/connections.yaml` with a connection named, for example, `primary`
  (set its API key from the web interface), add `target.connection: primary`
  to every job in `jobs.yaml`, and move the embedding model into
  `defaults.embedding.model` if it was only in the environment.
- A catalog without `target.connection` fails to load, and the previous one
  keeps serving until it is fixed.

## [1.0.0] - 2026-08-18

### Added
- Scheduled, multi-source document ingestion into an existing Qdrant,
  including the operator web interface for managing ingest jobs.
