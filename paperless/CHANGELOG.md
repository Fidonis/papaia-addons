# Changelog

All notable changes to the `paperless` add-on are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the versions are the add-on's own `version` in `papaia-app.yaml`. Steps an
operator has to do by hand are listed under `### Upgrade notes`.

## [2.2.0] - 2026-11-25

### Security
Every moved image was scanned on linux/amd64 and linux/arm64 (Trivy
vulnerability database of 2026-11-25); what remains is listed one by one in
`security/exceptions.yaml`.
- Paperless-ngx 3.4.2 -> 3.5.1, the newest release. It removes the zlib
  finding of 3.4.2; 206 CRITICAL and HIGH findings with a fix remain (the
  Debian and Python packages of the upstream build), none of which a newer
  build ships yet.
- Apache Tika 3.3.3.1 -> 3.4.0.0, the same reference as in the add-on
  `qdrant-ingest`. 3.4.0.0 has no CRITICAL or HIGH finding (3.3.3.1: cxf-core,
  jetty-http, jetty-security).
- PostgreSQL 18.7 moves to the current digest of the same tag (rebuilt
  2026-11-24): it removes the Kerberos and zlib findings; the Go runtime of
  `gosu` and zstd remain.
- Gotenberg 8.40.0 stays: it is the newest 8.x release and its digest is
  unchanged. Gotenberg 9 is a major step and is not taken here.

### Added
- `PAPERLESS_ALLOWED_HOSTS` (default `paperless`): the host names Paperless
  accepts besides the one in `PAPERLESS_PUBLIC_URL` and `localhost`.

### Upgrade notes
- From 3.5.0 on, with `PAPERLESS_URL` set, Paperless accepts only that host
  name and `localhost`. The add-on adds `paperless`, the name under which
  `paperless-mcp` reaches it, so the MCP server keeps working. If you set
  `PAPERLESS_ALLOWED_HOSTS` in `.env`, keep `paperless` in the list; never
  use `*`.
- Paperless-ngx applies its database migrations from 3.4.2 to 3.5.1 on the
  first start (3.5.0 replaces the tag tree storage with an adjacency list);
  the existing `paperless-data` and `paperless-pgdata` volumes are used as
  they are. A downgrade to 3.4.2 afterwards is not supported, so take a
  backup (`document_exporter`) first if a rollback must stay possible.
- Tested upgrade path: 3.4.2 to 3.5.1 on the same volumes with the add-on's
  compose file (`tools/test_paperless_upgrade_live.py`). Documents with their
  original and archive files, notes, custom field values, the tag tree,
  full-text search, users, groups, object permissions and the gate came
  through unchanged. Upgrades from older releases were not tested; the backup
  with `document_exporter` before the step stays recommended.
- Other changes of Paperless-ngx 3.5 (upstream release notes): bulk actions
  run in the background, expired sessions are cleared automatically, OCR
  arguments that Paperless sets itself are ignored in `PAPERLESS_OCR_USER_ARGS`,
  and the file name filter of scheduled workflows uses a PostgreSQL-compatible
  pattern.
- Apache Tika 3.4: tika-server runs on Java 17 (in the image), caps
  `multipart/form-data` uploads at 50 MB by default, limits embedded
  documents to a depth of 20 and measures the zip-bomb ratio (1000) across
  the whole document. Image user, port and start command are unchanged; Tika
  keeps no data.
- Ports, networks, volumes, users and capabilities are unchanged.

## [2.1.0] - 2026-11-24

### Security
Every image moves to a build with fewer CRITICAL and HIGH findings that have a
fix; what remains is listed one by one in `security/exceptions.yaml`.
- Paperless-ngx 3.0.5 -> 3.4.2. 3.0.5 is the newest 3.0 release and is built
  on Debian 13.5 (624 findings with a fix). No later release is free of them:
  3.4.2 leaves 204 and 3.5.1, the newest, 203, so the add-on takes 3.4.2, the
  newest release before 3.5.0 narrowed the accepted host names (see below).
- Gotenberg 8.34.0 -> 8.40.0, the newest release; 8.34 has no patch release,
  and Chromium fixes arrive only with new Gotenberg releases.
- Apache Tika 3.2.3.0 -> 3.3.3.1, the same reference as in the add-on
  `qdrant-ingest` (15 findings with a fix in 3.2.3.0, 4 in 3.3.3.1).
- nginx 1.31.2-alpine -> 1.31.10-alpine (paperless-gate), without findings.
- PostgreSQL 18.7 moves to the current digest of the same tag (Debian 13.7);
  no newer 18.x release exists.
- paperless-mcp-rbac 0.2.0-rc.1 -> 0.2.0-rc.2, the candidate rebuilt on a
  fixed base image, from the local test registry until 0.2.0 is published.
- Valkey 9.2.0-alpine stays: it has no finding.

### Upgrade notes
- Configuration, environment, ports, networks and volumes are unchanged.
- Paperless-ngx applies its database migrations from 3.0.5 to 3.4.2 on the
  first start; the existing `paperless-data` and `paperless-pgdata` volumes are
  used as they are. A downgrade to 3.0.5 afterwards is not
  supported, so take a backup (`document_exporter`) first if a rollback must
  stay possible.
- Paperless-ngx 3.2 changed the automatic matching of correspondents, storage
  paths and labels (a minimum match threshold); suggestions can differ for
  new documents.
- The add-on stays below Paperless-ngx 3.5: from 3.5.0 on, with
  `PAPERLESS_URL` set, Paperless accepts only that host name and `localhost`,
  and would refuse the requests of `paperless-mcp` to `paperless:8001`.
- PostgreSQL and Valkey keep their major versions, so their volumes start
  unchanged.

## [2.0.0] - 2026-10-24

### Added
- `components` in the manifest, naming the software the add-on bundles
  (Paperless-ngx 3.0.5, paperless-mcp-rbac 0.1.1, Valkey 9-alpine,
  PostgreSQL 18, Gotenberg 8.34, Apache Tika 3.2.3.0).
- `TIMEZONE` and `PAPERLESS_MCP_HOST` are documented in `.env.example`.

### Changed
- **Breaking:** the Docker bridge is scoped to the deployment,
  `${PAPAIA_PROJECT:-papaia}-paperless-net`, so two papAIa deployments on one
  host no longer share it. The add-on declares `requires.addon_api: 2`.

### Upgrade notes
- Needs papaia 1.3.0 or newer, the first core serving add-on API 2.
- A default single-stack host keeps the network name `papaia-paperless-net`,
  so nothing has to be migrated there. Where `PAPAIA_PROJECT` is not
  `papaia`, the network is renamed; an attachment wired by hand has to use the
  new name (`papaia-ctl` writes it for you).

## [1.1.0] - 2026-09-04

### Changed
- No change to the add-on itself; the version moved with catalog release 1.1.0.

## [1.0.0] - 2026-08-18

### Added
- Paperless-ngx 3.0.5 with Keycloak OIDC login, local-CA certificate support
  for the add-on's OIDC calls, installer hints for the Keycloak client secret
  and the OIDC/RBAC MCP server for LibreChat.
- Manifest metadata declaring each variable's type and whether it is a secret,
  so installer UIs render the right input control.

### Changed
- The homepage integration seam is gone from the manifest.
