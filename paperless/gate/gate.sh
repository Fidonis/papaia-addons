#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
# paperless-gate — entrypoint and secret handling
# by Fidonis GmbH · https://fidonis.de
#
# Usage: gate.sh run                       check secrets, render, start nginx
#        gate.sh render                    check secrets and render only
#        gate.sh reload                    render again and reload nginx
#                                          (after the proxy's secret rotated)
#        gate.sh rotate mcp [correlation]  new secret for the MCP server
#
# Every caller has its own secret, in its own directory below
# GATE_CALLERS_DIR (default /run/paperless-gate/callers):
#   proxy/secret  written by papaia-ctl, mounted read-only
#   mcp/secret    generated here on first start (sticky), read-only for the
#                 MCP server through the paperless-gate-mcp volume
# A secret is 32 to 512 characters of A-Z a-z 0-9 . _ ~ + / = - on one line;
# a file that is missing, a symbolic link, larger than 4 KiB or of any other
# form stops the gate (fail closed). Values are never printed: messages name
# the file only.
# ═══════════════════════════════════════════════════════════════════════════
set -eu

CALLERS_DIR="${GATE_CALLERS_DIR:-/run/paperless-gate/callers}"
RUN_DIR="${GATE_RUN_DIR:-/tmp/gate}"
TEMPLATE="${GATE_TEMPLATE:-/gate/nginx.conf}"
MARKER="        # @callers@"
MAX_BYTES=4096

die() {
    printf 'paperless-gate: %s\n' "$1" >&2
    exit "${2:-1}"
}

# Prints one papaia.audit/1 row for a write of the gate (secret created or
# rotated). It names the secret, never its value.
audit() {
    now="$(date -u +%Y-%m-%dT%H:%M:%S)"
    printf '{"format":"papaia.audit/1","time":"%s.000Z","correlation_id":"%s",' "$now" "$2"
    printf '"writer":"paperless/paperless-gate","role":null,"sub":null,"identity_kind":null,'
    printf '"operation":"%s","effect":"write","result":"allowed","event":"config_changed",' "$1"
    printf '"object_ref":{"type":"secret","id":"paperless-gate/%s"}}\n' "$3"
}

correlation_id() {
    if printf '%s' "${1:-}" | grep -Eqx '[A-Za-z0-9-]{1,64}'; then
        printf '%s' "$1"
    else
        od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
    fi
}

# read_secret <caller>: the checked value of <caller>/secret on stdout.
read_secret() {
    file="$CALLERS_DIR/$1/secret"
    [ -L "$file" ] && die "$1/secret is a symbolic link; refused"
    [ -f "$file" ] || die "$1/secret is missing ($file)"
    size="$(wc -c < "$file" | tr -d ' ')"
    [ "$size" -le "$MAX_BYTES" ] || die "$1/secret is larger than $MAX_BYTES bytes; refused"
    lines="$(grep -c '' "$file" || true)"
    [ "$lines" = 1 ] || die "$1/secret must hold exactly one line"
    value="$(LC_ALL=C tr -d '\n' < "$file")"
    length="$(printf '%s' "$value" | LC_ALL=C wc -c | tr -d ' ')"
    if [ "$length" -lt 32 ] || [ "$length" -gt 512 ] \
        || ! printf '%s' "$value" | LC_ALL=C grep -Eqx '[A-Za-z0-9._~+/=-]+'; then
        die "$1/secret is not a valid secret (32 to 512 characters of A-Z a-z 0-9 . _ ~ + / = -)"
    fi
    printf '%s' "$value"
}

new_secret() {
    head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'
}

# write_mcp_secret: a new value, written beside the old one and renamed into
# place, so the MCP server reads the old or the new secret, never a part.
write_mcp_secret() {
    dir="$CALLERS_DIR/mcp"
    [ -d "$dir" ] || die "mcp/ is missing ($dir)"
    part="$dir/.secret.new"
    rm -f "$part"
    (umask 0222 && new_secret > "$part")
    chmod 0444 "$part"
    mv -f "$part" "$dir/secret"
}

ensure_mcp_secret() {
    file="$CALLERS_DIR/mcp/secret"
    if [ ! -e "$file" ] && [ ! -L "$file" ]; then
        write_mcp_secret
        audit gate.secret_created "$(correlation_id "")" mcp
    fi
}

render() {
    proxy="$(read_secret proxy)"
    mcp="$(read_secret mcp)"
    [ "$proxy" != "$mcp" ] || die "proxy and mcp must each have their own secret"
    [ -f "$TEMPLATE" ] || die "template $TEMPLATE is missing"
    mkdir -p "$RUN_DIR"
    umask 0077
    out="$RUN_DIR/nginx.conf"
    part="$RUN_DIR/.nginx.conf.new"
    found=0
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$line" = "$MARKER" ]; then
            # The character set of a secret holds no quote, backslash, dollar,
            # semicolon or brace, so it cannot leave the quoted string.
            printf '        "%s" proxy;\n        "%s" mcp;\n' "$proxy" "$mcp"
            found=1
        else
            printf '%s\n' "$line"
        fi
    done < "$TEMPLATE" > "$part"
    [ "$found" = 1 ] || { rm -f "$part"; die "template $TEMPLATE has no callers marker"; }
    mv -f "$part" "$out"
}

reload_nginx() {
    if [ -f "$RUN_DIR/nginx.pid" ]; then
        nginx -t -q -c "$RUN_DIR/nginx.conf" || die "rendered configuration does not load"
        nginx -s reload -c "$RUN_DIR/nginx.conf"
    fi
}

command="${1:-}"
case "$command" in
    run)
        ensure_mcp_secret
        render
        exec nginx -c "$RUN_DIR/nginx.conf" -g 'daemon off;'
        ;;
    render)
        ensure_mcp_secret
        render
        ;;
    reload)
        ensure_mcp_secret
        render
        reload_nginx
        ;;
    rotate)
        [ "${2:-}" = mcp ] || die "unknown secret '${2:-}'; this gate rotates: mcp (the proxy's secret: papaia-ctl secrets rotate paperless-gate/proxy, then gate.sh reload)" 2
        cid="$(correlation_id "${3:-}")"
        # Check the proxy's secret first, so a refused rotation changes nothing.
        read_secret proxy > /dev/null
        write_mcp_secret
        audit gate.secret_rotated "$cid" mcp
        render
        reload_nginx
        ;;
    *)
        die "usage: gate.sh run | render | reload | rotate mcp [correlation-id]" 2
        ;;
esac
