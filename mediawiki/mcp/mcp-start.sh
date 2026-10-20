#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
# papaia-addon-mediawiki — MCP server start
# by Fidonis GmbH · https://fidonis.de
#
# Loads the OAuth consumer credentials the wiki's entrypoint wrote to the
# shared volume, restricts the accepted Host header to the public URL's
# host, and starts the server. config.json references the credentials as
# ${WIKI_OAUTH_CLIENT_ID} / ${WIKI_OAUTH_CLIENT_SECRET}.
# ═══════════════════════════════════════════════════════════════════════════
set -eu

die() { printf 'mediawiki-mcp: ERROR: %s\n' "$*" >&2; exit 1; }

[ -n "${MCP_PUBLIC_URL:-}" ] || die "MEDIAWIKI_MCP_PUBLIC_URL is not set (see .env.example)"
[ -r /papaia/credentials/oauth.env ] \
    || die "no OAuth consumer credentials yet; the wiki writes them on start"

set -a
# shellcheck source=/dev/null
. /papaia/credentials/oauth.env
set +a

# Host part of the public URL, without scheme, port and path.
host="${MCP_PUBLIC_URL#*://}"
host="${host%%/*}"
host="${host%:*}"
export MCP_ALLOWED_HOSTS="${MCP_ALLOWED_HOSTS:-$host}"

# An MCP client in a container -- LibreChat among them -- resolves localhost
# to itself and can neither fetch the sign-in metadata nor exchange tokens.
case "$host" in
    localhost|127.0.0.1|\[::1\]|*.localhost)
        printf 'mediawiki-mcp: WARNING: the public URL %s points to %s; MCP clients running in a container (LibreChat) cannot reach it. Use the wiki'"'"'s https URL that the browser and LibreChat both reach (see the add-on README, "LibreChat").\n' \
            "$MCP_PUBLIC_URL" "$host" >&2
        ;;
esac

exec node /app/dist/index.js
