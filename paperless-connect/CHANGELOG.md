# Changelog

All notable changes to the `paperless-connect` add-on are documented in this
file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [Unreleased]

### Added
- `exposes:` entry `paperless.documents`: the same tool server, audience and
  operations as the `paperless` add-on, with data classes, `header_identity:
  false` and `storage: {personal_data: false}`. The core creates the
  resource-server client `mcp-paperless` from it.

### Changed
- **Breaking:** the add-on requires papaia 1.10.0 (`requires.addon_api: [12]`).
- **Breaking:** the add-on stays outside the suite (`trust: addon`). It ships no
  LibreChat entry, no `librechat` audience mapper and no Keycloak client, and no
  longer attaches `librechat` to its network. The chat reaches it only after
  the operator allows it with `papaia-ctl addon chat allow paperless-connect`.

### Removed
- `integration/ai/librechat/librechat.yaml`,
  `integration/infra/keycloak/librechat-audience-mapper.json` and
  `integration/infra/keycloak/mcp-paperless.json`, and `integration:` in the
  manifest.

### Upgrade notes
- After the upgrade the chat lists no Paperless tools until the operator runs
  `papaia-ctl addon chat allow paperless-connect` (README, "Chat access").
- With an external identity provider: remove the `mcp-paperless-audience`
  mapper from the `librechat` client, and the `Paperless` entry from a
  hand-maintained `librechat.yaml`, if you added them for an earlier version.

## [2.0.1] - 2026-11-24

### Security
- paperless-mcp-rbac 0.1.1 -> 0.1.2, a patch release of the same line on a
  fixed base image; it removes every CRITICAL and HIGH finding with a fix of
  0.1.1. Pinned by tag and digest; until 0.1.2 is published, the default is its
  signed release candidate from a local test registry.

### Upgrade notes
- Nothing to do: configuration, environment and behaviour are unchanged.

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
