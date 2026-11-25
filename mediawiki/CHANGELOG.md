# Changelog

All notable changes to the `mediawiki` add-on are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [1.0.2] - 2026-11-25

### Security
- The wiki image `1.43.11-1.0.2` is rebuilt on the current digest of
  MediaWiki 1.43.11 (Debian 13.8, rebuilt upstream on 2026-11-24), which
  already carries the fixed Kerberos libraries, `zlib1g` and
  `linux-libc-dev`; their exact upgrades are dropped. `libzstd1` is raised to
  exactly 1.5.7+dfsg-1+deb13u1 (CVE-2026-124851, HIGH), the only Debian
  package of the image with a published fix that the base digest does not
  carry. No other package is added or upgraded. The remaining finding with a
  fix, symfony/yaml in the vendor directory of MediaWiki core, is fixed only
  by a MediaWiki release; it stays listed in `security/exceptions.yaml`.
- Until `1.43.11-1.0.2` is published, the default of `MEDIAWIKI_IMAGE` is its
  signed release candidate from a local test registry.

### Upgrade notes
- Configuration, environment, ports, networks, user and the database volume
  are unchanged; only the image changes.

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
- MediaWiki MCP Server 0.20.0 -> 0.21.0, pinned by tag and image index
  digest. 0.20.1 is the newest 0.20 release; 0.21.0 removes the
  follow-redirects finding. Its remaining findings (undici, brace-expansion
  in the bundled Node.js dependencies) are fixed upstream, but no MediaWiki
  MCP Server release ships the fixes yet; they stay listed in
  `security/exceptions.yaml`.
- MariaDB 11.8.9 stays: it is the newest 11.8 release. Its findings are in the
  Go runtime of `gosu`, fixed in no MariaDB 11.8 image.

### Upgrade notes
- Configuration, environment, ports, networks and the database volume are
  unchanged.
- MediaWiki MCP Server 0.21.0 runs on Node.js 26 (0.20 on Node.js 24); the
  start command, user, port and health check of the image are unchanged.
- `update-page` with `section=N` in replace mode now refuses a source that
  does not begin with a heading and quotes the heading to keep; appends,
  prepends, find-replace and whole-page replaces are unchanged. A client that
  replaced a section without its heading has to send the heading too.
- New read-only tool `get-random-pages`, subject to the same wiki rights of
  the signed-in user as every other tool. The new per-wiki `bot` setting is
  not set, so edits are not marked as bot edits.

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
