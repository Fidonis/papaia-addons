# papaia-addons

Add-ons for the [papaia](https://github.com/Fidonis/papaia) stack — each one a
self-contained bundle (Compose service, manifest, and Keycloak/LibreChat/Nginx
integration assets) that `papaia-ctl` can install into a running papAIa
deployment.

## Add-ons

| Add-on | Description |
|---|---|
| [`paperless`](paperless/) | Paperless-ngx document management + OIDC/RBAC MCP server |
| [`paperless-connect`](paperless-connect/) | OIDC/RBAC MCP server for an existing standalone Paperless-ngx instance |
| [`qdrant`](qdrant/) | Qdrant vector database + OIDC/RBAC MCP server |
| [`qdrant-connect`](qdrant-connect/) | OIDC/RBAC MCP server for an existing standalone Qdrant instance |
| [`qdrant-ingest`](qdrant-ingest/) | Scheduled multi-source document ingestion into an existing Qdrant |

Each add-on directory carries its own README, `.env.example`, and
`papaia-app.yaml` manifest. See [AGENTS.md](AGENTS.md) for the manifest
contract and the repository's engineering conventions.

Maintained by [Fidonis](https://fidonis.de).
