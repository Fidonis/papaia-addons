# Changelog

All notable changes to the `mediawiki` add-on are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [1.0.1] - 2026-11-24

### Security
- The wiki image `1.43.11-1.0.1` is rebuilt on the same MediaWiki 1.43.11 (the
  newest 1.43 release) and Composer 2.8.12 (the newest 2.8 release), both now
  pinned by tag and image index digest, and raises the Debian packages with a
  published security fix that the base digest does not carry yet (Kerberos
  libraries, `linux-libc-dev`) to exactly the fixed version. The remaining
  finding with a fix, symfony/yaml in the vendor directory of MediaWiki core,
  is fixed only by a MediaWiki release; it stays listed in
  `security/exceptions.yaml`.
- Until `1.43.11-1.0.1` is published, the default of `MEDIAWIKI_IMAGE` is its
  signed release candidate from a local test registry.
- MediaWiki MCP Server 0.20.0 -> 0.20.1. Its remaining findings (undici,
  brace-expansion, follow-redirects) are fixed in no 0.20 release; 0.21.0
  still has the first two and changes the behaviour of `update-page`, so the
  add-on stays on the 0.20 line.
- MariaDB 11.8.9 stays: it is the newest 11.8 release. Its findings are in the
  Go runtime of `gosu`, fixed in no MariaDB 11.8 image.

### Upgrade notes
- Nothing to do: configuration, environment, the database volume and the
  behaviour of the wiki and the MCP server are unchanged.

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
