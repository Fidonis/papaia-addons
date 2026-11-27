# Changelog

All notable changes to the `n8n` add-on are documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions are the add-on's own `version` in `papaia-app.yaml`. Steps an operator
has to do by hand are listed under `### Upgrade notes`.

## [1.0.0] - 2026-10-24

### Added
- n8n workflow automation behind an oauth2-proxy Keycloak SSO gate. The engine
  is never published; only the webhook paths bypass the gate. The add-on ships
  no MCP server.
- `components` in the manifest (n8n 2.36.6, oauth2-proxy v7.15.3, nginx
  1.31-alpine, PostgreSQL 18).
- Requires papaia 1.3.0 or newer.
