# Changelog

All notable changes to the `mediawiki` add-on are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [1.1.0] - 2026-11-28

### Added
- A service account other add-ons use over the API. `MW_SERVICE_LOGIN`
  (`<user>@<app>`, default `PapaiaService@service`) names it and
  `MW_SERVICE_PASSWORD` (generated on install) is its bot password. On every
  start the entrypoint creates the account without a main password, keeps the
  bot password equal to the `.env` value and removes it when the value is
  empty. Add-ons that require `mediawiki` read both values through `exports`
  on cores serving add-on API 4 (papaia 1.9.0 or newer). See README, "Service
  account".
- `integration.keycloak.client_secrets`: a core serving add-on API 4 creates
  the wiki's Keycloak client `mediawiki` on `addon start` and `start`, with
  the secret from `KC_MEDIAWIKI_CLIENT_SECRET` and the root URL from
  `MW_SERVER`. The secret is generated on install instead of being copied from
  Keycloak.
- A knowledge source `mediawiki.wiki` for the core's knowledge-map profile
  (add-on API 6, papaia 1.9.0 or newer). Older cores ignore it.

### Changed
- MediaWiki MCP Server 0.19.0 → 0.21.0.
- The wiki image is published as `1.43.11-1.1.0`; its content is the same as
  `1.43.11-1.0.0`.

### Fixed
- The MCP server trusts the papaia local CA (`NODE_EXTRA_CA_CERTS`, cleared for
  an external identity provider like `MW_OIDC_CA_FILE`) and resolves
  `host.docker.internal` on Linux hosts, so `get-file-data` no longer stops at
  the certificate of the wiki's https port. The download itself still fails
  with HTTP 403; see "Known limitations" in the README.
- A certificate of the https port that fails verification only on the name
  constraints of a local CA from papaia 1.9.0 or later is kept instead of being
  reissued on every start. The entrypoint warns and names the host names that
  CA signs for; any other name goes through the reverse proxy. A mounted
  `local-ca.crt` without a readable `local-ca.key` is reported as such.

### Upgrade notes
- `papaia-ctl addon upgrade mediawiki` (papaia 1.9.0 or newer) generates
  `MW_SERVICE_PASSWORD`. Without a value the service account gets no bot
  password and other add-ons cannot sign in with it.
- An installation keeps its `KC_MEDIAWIKI_CLIENT_SECRET`. With a core older
  than 1.9.0 the client is not created for you: import
  `integration/infra/keycloak/mediawiki.json` by hand and copy the client's
  secret over the generated value.

## [1.0.0] - 2026-10-24

### Added
- A MediaWiki 1.43 knowledge base with Keycloak login and group sync,
  namespace-level access tiers, and the MediaWiki MCP Server, through which
  LibreChat works in the wiki as the signed-in user within the same tiers.
- Tier content, including uploaded files, stays out of search, listings, feeds
  and watchlist mails for everyone outside the tier. The add-on's own
  `tests/run.sh` checks this.
- The wiki image is published as
  `ghcr.io/fidonis/papaia-mediawiki:<MediaWiki version>-<add-on version>` by the
  `mediawiki-image` workflow.
- `components` in the manifest (MediaWiki 1.43.11-1.0.0, MediaWiki MCP Server
  0.19.0, MariaDB 11.8.9).
- Requires papaia 1.3.0 or newer.
