#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# papaia-addon-mediawiki — access-tier test run
# by Fidonis GmbH · https://fidonis.de
#
# Starts the wiki and its database from the add-on's own docker-compose.yml
# under a throw-away project, with local login on and a placeholder identity
# provider, seeds three users and runs tier_check.py against it. Everything
# is removed afterwards, volumes included, unless --keep is given.
#
#   tests/run.sh [--port=18020] [--keep]
#
# The image is the one docker-compose.yml names (MEDIAWIKI_IMAGE), built from
# image/ when it is not present locally. Run it after every change to the
# tier hooks in runtime/LocalSettings.php and after a MediaWiki or extension
# bump.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

port=18020
keep=false
for arg in "$@"; do
    case "$arg" in
        --port=*) port="${arg#--port=}" ;;
        --keep) keep=true ;;
        *) echo "usage: $0 [--port=N] [--keep]" >&2; exit 2 ;;
    esac
done

addon_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="mwtiertest$$"
work="$(mktemp -d)"
mkdir -p "$work/config/certs"
password="$(openssl rand -hex 12)"
admin_password="$(openssl rand -hex 12)"

cat > "$work/.env" <<EOF
PAPAIA_PROJECT=$project
PAPAIA_CONFIG_DIR=$work/config
HOST_IP=127.0.0.1
MEDIAWIKI_EXT_PORT=$port
MW_SERVER=https://127.0.0.1:$port
OIDC_ISSUER=https://idp.invalid/realms/test
KC_MEDIAWIKI_CLIENT_SECRET=unused
MW_SECRET_KEY=$(openssl rand -hex 32)
MW_UPGRADE_KEY=$(openssl rand -hex 8)
MW_DB_PASSWORD=$(openssl rand -hex 12)
MW_ADMIN_PASSWORD=$admin_password
MW_ENABLE_LOCAL_LOGIN=true
MW_AUTO_LOGIN=false
MW_NAMESPACES=Internal:3000,Management:3002,Operations:3004
MW_NAMESPACE_READ_GROUPS=Internal=internal,management,papaia-admin;Management=management,papaia-admin;Operations=papaia-admin
MW_SYSOP_GROUPS=papaia-admin
MEDIAWIKI_MCP_SIGNING_KEY=$(openssl rand -hex 32)
EOF

compose() {
    docker compose -p "$project" --env-file "$work/.env" -f "$addon_dir/docker-compose.yml" "$@"
}

cleanup() {
    if [[ "$keep" == true ]]; then
        echo "kept: project $project, env $work/.env (docker compose -p $project ... down -v)"
        return
    fi
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

echo "starting the wiki (project $project, https://127.0.0.1:$port)"
compose up -d --wait mediawiki

mw() { compose exec -T mediawiki "$@"; }
mw env MW_ENABLE_LOCAL_LOGIN=true php maintenance/run.php createAndPromote \
    --custom-groups=management Member "$password" >/dev/null
mw env MW_ENABLE_LOCAL_LOGIN=true php maintenance/run.php createAndPromote \
    Outsider "$password" >/dev/null

python3 "$addon_dir/tests/tier_check.py" --base "https://127.0.0.1:$port" \
    --password "$password" --admin-password "$admin_password"
