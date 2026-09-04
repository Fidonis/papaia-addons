# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are generated automatically by [release-drafter](https://github.com/release-drafter/release-drafter)
based on merged pull requests; this file mirrors the published releases.

## [Unreleased]

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
