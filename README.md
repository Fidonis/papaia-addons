# papaia-addons

Add-ons for the [papaia](https://github.com/Fidonis/papaia) stack — each one a
self-contained bundle (Compose service, manifest, and Keycloak/LibreChat/Nginx
integration assets) that `papaia-ctl` can install into a running papAIa
deployment.

## Add-ons

| Add-on | Description |
|---|---|
| [`mediawiki`](mediawiki/) | MediaWiki knowledge base with access tiers + MCP server with per-user sign-in |
| [`n8n`](n8n/) | n8n workflow automation behind a Keycloak SSO gate |
| [`paperless`](paperless/) | Paperless-ngx document management + OIDC/RBAC MCP server |
| [`paperless-connect`](paperless-connect/) | OIDC/RBAC MCP server for an existing standalone Paperless-ngx instance |
| [`qdrant`](qdrant/) | Qdrant vector database + OIDC/RBAC MCP server |
| [`qdrant-connect`](qdrant-connect/) | OIDC/RBAC MCP server for an existing standalone Qdrant instance |
| [`qdrant-ingest`](qdrant-ingest/) | Scheduled multi-source document ingestion into an existing Qdrant |

## Modules in their own repositories

Studio, Data, Flow, the assistant and the SQL Server source keep their add-on
bundle in their own repository (`deploy/papaia-addon/`). They are listed in
[`catalogue.yaml`](catalogue.yaml), each pinned to one bundle by version, full
commit and bundle digest:

| Add-on | Title | Repository |
|---|---|---|
| `papaia-studio` | Studio | [`Fidonis/papaia-studio`](https://github.com/Fidonis/papaia-studio) |
| `papaia-data` | Data | [`Fidonis/papaia-data`](https://github.com/Fidonis/papaia-data) |
| `papaia-flow` | Flow | [`Fidonis/papaia-flow`](https://github.com/Fidonis/papaia-flow) |
| `papaia-assist` | Assistant | [`Fidonis/papaia-assist`](https://github.com/Fidonis/papaia-assist) |
| `mssql-mcp` | SQL Server source | [`Fidonis/mssql-mcp-rbac`](https://github.com/Fidonis/mssql-mcp-rbac) |

`python tools/catalogue.py list` shows version, description and prerequisites
of each entry. `python tools/catalogue.py fetch <name> --repo <clone> --into <dir>`
copies the pinned bundle out of a clone of the module repository and refuses it
when its digest differs from the catalogue; the copy is what `papaia-ctl addon
install` installs.

Each add-on directory carries its own README, `.env.example`, and
`papaia-app.yaml` manifest. The manifest `version` counts changes to the
add-on; the `components` list in the manifest, mirrored as a table in the
add-on's README, names the software and versions it runs. See
[docs/development.md](docs/development.md) for the manifest contract and the repository's
engineering conventions.

Maintained by [Fidonis](https://fidonis.de).
