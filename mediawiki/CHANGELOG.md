# Changelog

All notable changes to the `mediawiki` add-on are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

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
