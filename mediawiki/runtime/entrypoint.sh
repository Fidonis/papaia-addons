#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# papaia-addon-mediawiki — container entrypoint
# by Fidonis GmbH · https://fidonis.de
#
# Brings the wiki to a usable state before Apache starts, idempotently:
#   1. trust the bundled Keycloak CA for the OIDC calls (MW_OIDC_CA_FILE),
#      and provide the certificate of the published https port
#   2. wait for the database
#   3. empty database -> install.php (schema + local admin)
#      otherwise, once per container -> update.php (schema after an upgrade)
#   4. local admin: groups, password from MW_ADMIN_PASSWORD
#   5. OAuth: token signing keys + the MCP server's consumer, whose
#      credentials go to the volume the MCP container reads
#
# Every value comes from the add-on .env, so nothing has to be copied by hand
# and a restart re-applies a rotated secret.
#
# Mounted read-only from the add-on's runtime/ directory together with
# LocalSettings.php and EnsureOAuthConsumer.php, so an add-on update reaches
# an existing install on the next start, without a new image.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

export MW_INSTALL_PATH=/var/www/html
cd "$MW_INSTALL_PATH"

STATE_DIR=/var/lib/papaia-mediawiki
RUNTIME_DIR=/usr/local/share/papaia-mediawiki
TLS_DIR=/var/lib/papaia-mediawiki-tls
LOCAL_CA_CRT=/certs/local-ca.crt
LOCAL_CA_KEY=/certs/local-ca.key
ALL_TIERS_GROUP=all-tiers
# What the MCP server may do as the signed-in user; see README, "Through the
# MCP server".
DEFAULT_MCP_GRANTS=basic,editpage,createeditmovepage,uploadfile,uploadeditmovefile

log() { printf 'papaia-mediawiki: %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

for var in MW_SERVER MW_SECRET_KEY MW_UPGRADE_KEY MW_DB_PASSWORD MW_ADMIN_PASSWORD \
           OIDC_ISSUER KC_MEDIAWIKI_CLIENT_SECRET; do
    [[ -n "${!var:-}" ]] || die "$var is not set (see .env.example)"
done

MW_ADMIN_USER="${MW_ADMIN_USER:-Admin}"
OAUTH_DIR=/var/lib/papaia-mediawiki-oauth
MCP_DIR=/var/lib/papaia-mediawiki-mcp
MW_DB_HOST="${MW_DB_HOST:-mediawiki-db}"
MW_DB_NAME="${MW_DB_NAME:-mediawiki}"
MW_DB_USER="${MW_DB_USER:-mediawiki}"
export MW_DB_HOST MW_DB_NAME MW_DB_USER

# ── 1. CA trust for the OIDC calls ────────────────────────────────────────
# OpenIDConnect offers no CA-path setting, so the bundled Keycloak CA is
# appended to the system bundle and handed to curl/openssl via php.ini.
# papaia-ctl clears MW_OIDC_CA_FILE for an external, publicly trusted
# identity provider; the system bundle is then used unchanged.
ca_ini=/usr/local/etc/php/conf.d/zz-papaia-ca.ini
rm -f "$ca_ini"
if [[ -n "${MW_OIDC_CA_FILE:-}" ]]; then
    if [[ -r "$MW_OIDC_CA_FILE" ]]; then
        cat /etc/ssl/certs/ca-certificates.crt "$MW_OIDC_CA_FILE" > "$STATE_DIR/ca-bundle.pem"
        printf 'curl.cainfo = %s\nopenssl.cafile = %s\n' \
            "$STATE_DIR/ca-bundle.pem" "$STATE_DIR/ca-bundle.pem" > "$ca_ini"
    else
        log "WARNING: MW_OIDC_CA_FILE=$MW_OIDC_CA_FILE is not readable; using the system CA bundle"
    fi
fi

# ── 1b. TLS for the published port ────────────────────────────────────────
# MCP clients refuse a plain-http MCP server, and LibreChat reaches the wiki
# under the same URL as the browser. The published port therefore speaks
# https: with a certificate from the papaia local CA when one is mounted --
# which LibreChat and the browsers that use the bundled Keycloak already
# trust -- and a self-signed one otherwise. A reverse proxy in front of the
# wiki uses plain-http port 80 on the add-on network and its own certificate.
tls_host="${MW_SERVER#*://}"
tls_host="${tls_host%%/*}"
tls_host="${tls_host%:*}"
tls_host="${tls_host#[}"
tls_host="${tls_host%]}"
if [[ "$tls_host" =~ ^[0-9.]+$ || "$tls_host" == *:* ]]; then
    tls_san="IP:$tls_host"
    tls_check=(-checkip "$tls_host")
else
    tls_san="DNS:$tls_host"
    tls_check=(-checkhost "$tls_host")
fi
tls_ca=""
if [[ -r "$LOCAL_CA_CRT" && -r "$LOCAL_CA_KEY" ]]; then
    tls_ca="$LOCAL_CA_CRT"
fi
tls_current() {
    # The existing certificate fits: right host, valid for another 30 days,
    # and issued by the CA in use now (or self-signed when there is none).
    [[ -s "$TLS_DIR/tls.crt" && -s "$TLS_DIR/tls.key" ]] || return 1
    openssl x509 -in "$TLS_DIR/tls.crt" -noout -checkend 2592000 >/dev/null 2>&1 || return 1
    openssl x509 -in "$TLS_DIR/tls.crt" -noout "${tls_check[@]}" 2>/dev/null | grep -q 'does match' || return 1
    if [[ -n "$tls_ca" ]]; then
        openssl verify -CAfile "$tls_ca" "$TLS_DIR/tls.crt" >/dev/null 2>&1
    else
        openssl verify -CAfile "$TLS_DIR/tls.crt" "$TLS_DIR/tls.crt" >/dev/null 2>&1
    fi
}
if ! tls_current; then
    tls_tmp="$(mktemp -d)"
    printf 'subjectAltName=%s,DNS:localhost\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' \
        "$tls_san" > "$tls_tmp/ext"
    openssl genrsa -out "$tls_tmp/tls.key" 2048 2>/dev/null
    openssl req -new -key "$tls_tmp/tls.key" -subj "/CN=$tls_host/O=papAIa" -out "$tls_tmp/tls.csr" 2>/dev/null
    # 825 days: the longest validity Apple platforms accept for a server
    # certificate from a private CA. A restart within 30 days of expiry
    # issues a new one.
    if [[ -n "$tls_ca" ]]; then
        log "issuing the TLS certificate for $tls_host from the papaia local CA"
        openssl x509 -req -in "$tls_tmp/tls.csr" -CA "$LOCAL_CA_CRT" -CAkey "$LOCAL_CA_KEY" \
            -CAcreateserial -CAserial "$tls_tmp/ca.srl" -days 825 -sha256 \
            -extfile "$tls_tmp/ext" -out "$tls_tmp/tls.crt" 2>/dev/null
    else
        log "no papaia local CA mounted: issuing a self-signed TLS certificate for $tls_host"
        openssl x509 -req -in "$tls_tmp/tls.csr" -signkey "$tls_tmp/tls.key" -days 825 -sha256 \
            -extfile "$tls_tmp/ext" -out "$tls_tmp/tls.crt" 2>/dev/null
    fi
    install -m 0600 "$tls_tmp/tls.key" "$TLS_DIR/tls.key"
    install -m 0644 "$tls_tmp/tls.crt" "$TLS_DIR/tls.crt"
    rm -rf "$tls_tmp"
fi

# ── 2. Database ───────────────────────────────────────────────────────────
db_query() {
    # Prints the number of rows the query returns; exit 1 if the server is
    # unreachable. Credentials stay in the environment, out of argv.
    php -r '
        mysqli_report(MYSQLI_REPORT_OFF);
        $db = @new mysqli(getenv("MW_DB_HOST"), getenv("MW_DB_USER"), getenv("MW_DB_PASSWORD"), getenv("MW_DB_NAME"));
        if ($db->connect_errno) { exit(1); }
        $res = $db->query($argv[1]);
        if ($res === false) { exit(1); }
        echo $res->num_rows;
    ' "$1"
}

for _ in $(seq 1 90); do
    db_query 'SELECT 1' >/dev/null 2>&1 && break
    sleep 2
done
db_query 'SELECT 1' >/dev/null 2>&1 || die "database $MW_DB_NAME on $MW_DB_HOST is not reachable"

# ── 3. Schema ─────────────────────────────────────────────────────────────
# install.php refuses to run while a LocalSettings.php exists, so the
# add-on's copy is kept aside (RUNTIME_DIR) and put in place after the install.
rm -f "$MW_INSTALL_PATH/LocalSettings.php"
if [[ "$(db_query "SHOW TABLES LIKE 'page'")" == "0" ]]; then
    log "empty database: installing MediaWiki"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    printf '%s' "$MW_DB_PASSWORD" > "$tmp/dbpass"
    printf '%s' "$MW_ADMIN_PASSWORD" > "$tmp/pass"
    # install.php writes a LocalSettings.php of its own; it goes to the
    # scratch dir and is discarded -- the image's env-driven one is used.
    php maintenance/install.php \
        --dbtype=mysql --dbserver="$MW_DB_HOST" --dbname="$MW_DB_NAME" \
        --dbuser="$MW_DB_USER" --dbpassfile="$tmp/dbpass" \
        --server="$MW_SERVER" --scriptpath="" --lang="${MW_LANG:-en}" \
        --passfile="$tmp/pass" --confpath="$tmp" --skins=Vector \
        "${MW_SITE_NAME:-Wiki}" "$MW_ADMIN_USER" >&2
    rm -rf "$tmp"
    trap - EXIT
fi
# A copy, not a symlink: the base image's web root is sticky and world-
# writable, and the kernel's protected_symlinks then stops Apache from
# following a root-owned link -- the wiki would serve the installer.
install -m 0644 "$RUNTIME_DIR/LocalSettings.php" "$MW_INSTALL_PATH/LocalSettings.php"

# update.php creates the extensions' tables on a fresh install and migrates
# the schema after an image upgrade. A new image means a new container, so
# once per container covers both without paying for it on every restart.
if [[ ! -e "$STATE_DIR/updated" ]]; then
    log "running update.php"
    php maintenance/run.php update --quick >&2
    touch "$STATE_DIR/updated"
fi

# ── 4. Local admin (break-glass) ──────────────────────────────────────────
# --force re-applies groups and the password from the .env on every start,
# so a rotated MW_ADMIN_PASSWORD takes effect after a restart.
# PluggableAuth removes the local password provider while local login is
# off, which makes setting any password fail; LocalSettings.php reads the
# switch per process, so it is turned on for these two CLI calls only and
# the web UI is unaffected.
MW_ENABLE_LOCAL_LOGIN=true php maintenance/run.php createAndPromote --force --sysop --bureaucrat \
    --custom-groups="$ALL_TIERS_GROUP" "$MW_ADMIN_USER" "$MW_ADMIN_PASSWORD" >&2

# ── 5. OAuth ──────────────────────────────────────────────────────────────
# The key pair signs the OAuth 2.0 access tokens. It is created once and
# kept on a volume: a new pair would invalidate every token handed out.
if [[ ! -s "$OAUTH_DIR/oauth2.key" ]]; then
    log "generating the OAuth 2.0 signing key pair"
    openssl genrsa -out "$OAUTH_DIR/oauth2.key.tmp" 2048 2>/dev/null
    openssl rsa -in "$OAUTH_DIR/oauth2.key.tmp" -pubout -out "$OAUTH_DIR/oauth2.pub" 2>/dev/null
    mv "$OAUTH_DIR/oauth2.key.tmp" "$OAUTH_DIR/oauth2.key"
fi
chown www-data:www-data "$OAUTH_DIR/oauth2.key" "$OAUTH_DIR/oauth2.pub"
chmod 0600 "$OAUTH_DIR/oauth2.key"

# The consumer the MCP server signs users in through. Its callback and grants
# are fixed when it is created; a changed public URL or grant list gets a new
# consumer, and users consent once more.
mcp_url="${MEDIAWIKI_MCP_PUBLIC_URL:-${MW_SERVER%/}/mcp}"
php "$RUNTIME_DIR/EnsureOAuthConsumer.php" \
    --user="$MW_ADMIN_USER" \
    --callback="${mcp_url%/}/oauth/callback" \
    --grants="${MEDIAWIKI_MCP_GRANTS:-$DEFAULT_MCP_GRANTS}" \
    --email="${MW_EMAIL_FROM:-wiki@localhost}" \
    --output="$MCP_DIR/oauth.env" >&2

chown -R www-data:www-data "$MW_INSTALL_PATH/images"

log "ready"
exec docker-php-entrypoint "$@"
