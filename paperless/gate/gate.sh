#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
# paperless-gate — entrypoint and secret handling
# by Fidonis GmbH · https://fidonis.de
#
# Usage: gate.sh run                       check secrets, render, start nginx
#                                          and follow the name list
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
#
# Names: the MCP server's X-Papaia-Remote-User passes only for a name on the
# list the realm sync writes (GATE_USERS_FILE, default
# /run/paperless-gate/users/users.json, format papaia.paperless-users/1; the
# directory is mounted read-only). A list that is missing, a symbolic link,
# larger than 1 MiB or malformed admits no name, while the proxy's path, which
# carries no identity, stays open. `run` checks the list every
# GATE_WATCH_SECONDS (default 5) and reloads nginx when it changed; a change
# it cannot load stops the gate. Names are never printed, only their number.
# ═══════════════════════════════════════════════════════════════════════════
set -eu

CALLERS_DIR="${GATE_CALLERS_DIR:-/run/paperless-gate/callers}"
RUN_DIR="${GATE_RUN_DIR:-/tmp/gate}"
TEMPLATE="${GATE_TEMPLATE:-/gate/nginx.conf}"
USERS_FILE="${GATE_USERS_FILE:-/run/paperless-gate/users/users.json}"
WATCH_SECONDS="${GATE_WATCH_SECONDS:-5}"
MARKER="        # @callers@"
USERS_MARKER="        # @users@"
USERS_FORMAT="papaia.paperless-users/1"
MAX_BYTES=4096
MAX_USERS_BYTES=1048576

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

# Strict reader of papaia.paperless-users/1: an object with exactly the
# members "format" and "users", the users an array of unique names. Strings
# hold printable ASCII without escapes. A name is a letter or digit followed by
# letters, digits and ._@+- (at most 255 characters, the realm sync's rule);
# two names that differ only in case are refused, since nginx compares map
# keys without regard to case. Prints one map line per name, or nothing and
# exits 1. A name equal to one of map's own parameters gets a leading
# backslash, which map removes; no other name can start with one.
# shellcheck disable=SC2016
USERS_AWK='
function fail() { bad = 1; exit 1 }
function token(t) {
    if (state == "start")        { if (t != "{") fail(); state = "key" }
    else if (state == "key" || state == "nextkey") {
        if (t == "}" && state == "key") fail()
        if (t != "\"") fail()
        if (str != "format" && str != "users") fail()
        if (str in seen) fail()
        seen[str] = 1; key = str; state = "colon"
    }
    else if (state == "colon")   { if (t != ":") fail(); state = (key == "format") ? "format" : "open" }
    else if (state == "format")  { if (t != "\"" || str != format) fail(); state = "after" }
    else if (state == "open")    { if (t != "[") fail(); state = "first" }
    else if (state == "first" || state == "name") {
        if (t == "]" && state == "first") { state = "after"; return }
        if (t != "\"") fail()
        if (length(str) > 255 || str !~ /^[A-Za-z0-9][A-Za-z0-9._@+-]*$/) fail()
        folded = tolower(str)
        if (folded in names) fail()
        names[folded] = 1; order[++count] = str; state = "more"
    }
    else if (state == "more")    { if (t == ",") state = "name"; else if (t == "]") state = "after"; else fail() }
    else if (state == "after")   { if (t == ",") state = "nextkey"; else if (t == "}") state = "end"; else fail() }
    else fail()
}
# Records end at a comma (RS=","), which no valid string holds, so a list
# written on one line is read in short pieces; each record after the first
# starts with the comma that ended the one before.
BEGIN { state = "start"; count = 0; bad = 0 }
{
    if (NR > 1) token(",")
    n = length($0)
    for (i = 1; i <= n; i++) {
        c = substr($0, i, 1)
        if (c == " " || c == "\t" || c == "\r" || c == "\n") continue
        if (c == "\"") {
            str = ""
            for (i++; i <= n; i++) {
                c = substr($0, i, 1)
                if (c == "\"") break
                if (c == "\\" || c < " " || c > "~") fail()
                str = str c
            }
            if (i > n) fail()
            token("\"")
        } else if (c == "{" || c == "}" || c == "[" || c == "]" || c == "," || c == ":") {
            token(c)
        } else {
            fail()
        }
    }
}
END {
    if (bad || state != "end" || !("format" in seen) || !("users" in seen)) exit 1
    for (j = 1; j <= count; j++) {
        key = order[j]
        if (key ~ /^(default|hostnames|include|volatile)$/) key = "\\" key
        printf "        \"%s\" \"%s\";\n", key, order[j]
    }
}
'

# users_fingerprint: what the list on disk is now, for the watch loop.
users_fingerprint() {
    if [ -L "$USERS_FILE" ]; then
        printf 'link'
    elif [ ! -e "$USERS_FILE" ]; then
        printf 'missing'
    else
        cksum < "$USERS_FILE" 2>/dev/null || printf 'unreadable'
    fi
}

# read_users: the map lines of the listed names on stdout; nothing when the
# list is missing or not valid (no name is admitted). Reads one snapshot, so a list
# replaced meanwhile is either read whole or seen as changed by the next check.
read_users() {
    snap="$RUN_DIR/users.snapshot"
    rm -f "$snap"
    if [ -L "$USERS_FILE" ]; then
        printf 'link' > "$RUN_DIR/users.sum"
        printf 'paperless-gate: %s is a symbolic link; no name is admitted\n' "$USERS_FILE" >&2
        return 0
    fi
    if [ ! -e "$USERS_FILE" ]; then
        printf 'missing' > "$RUN_DIR/users.sum"
        printf 'paperless-gate: %s is missing; no name is admitted\n' "$USERS_FILE" >&2
        return 0
    fi
    if [ ! -f "$USERS_FILE" ] || ! cat "$USERS_FILE" > "$snap" 2>/dev/null; then
        printf 'unreadable' > "$RUN_DIR/users.sum"
        printf 'paperless-gate: %s is not a readable file; no name is admitted\n' "$USERS_FILE" >&2
        return 0
    fi
    cksum < "$snap" > "$RUN_DIR/users.sum"
    size="$(wc -c < "$snap" | tr -d ' ')"
    if [ "$size" -gt "$MAX_USERS_BYTES" ]; then
        printf 'paperless-gate: %s is larger than %s bytes; no name is admitted\n' \
            "$USERS_FILE" "$MAX_USERS_BYTES" >&2
        rm -f "$snap"
        return 0
    fi
    plain="$(LC_ALL=C tr -d '\000' < "$snap" | wc -c | tr -d ' ')"
    if [ "$plain" != "$size" ] \
        || ! LC_ALL=C awk -v RS=, -v format="$USERS_FORMAT" "$USERS_AWK" "$snap" > "$RUN_DIR/users.list"; then
        printf 'paperless-gate: %s is not a valid name list; no name is admitted\n' "$USERS_FILE" >&2
        : > "$RUN_DIR/users.list"
    fi
    rm -f "$snap"
    cat "$RUN_DIR/users.list"
    rm -f "$RUN_DIR/users.list"
}

render() {
    # Explicit, so a caller that tests render's status cannot lose the stop.
    proxy="$(read_secret proxy)" || exit 1
    mcp="$(read_secret mcp)" || exit 1
    [ "$proxy" != "$mcp" ] || die "proxy and mcp must each have their own secret"
    [ -f "$TEMPLATE" ] || die "template $TEMPLATE is missing"
    mkdir -p "$RUN_DIR"
    umask 0077
    users="$(read_users)"
    count=0
    [ -z "$users" ] || count="$(printf '%s\n' "$users" | wc -l | tr -d ' ')"
    out="$RUN_DIR/nginx.conf"
    part="$RUN_DIR/.nginx.conf.new"
    found=0
    found_users=0
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$line" = "$MARKER" ]; then
            # The character set of a secret holds no quote, backslash, dollar,
            # semicolon or brace, so it cannot leave the quoted string.
            printf '        "%s" proxy;\n        "%s" mcp;\n' "$proxy" "$mcp"
            found=1
        elif [ "$line" = "$USERS_MARKER" ]; then
            # Neither can a name (letters, digits and ._@+-, read_users).
            [ -z "$users" ] || printf '%s\n' "$users"
            found_users=1
        else
            printf '%s\n' "$line"
        fi
    done < "$TEMPLATE" > "$part"
    [ "$found" = 1 ] || { rm -f "$part"; die "template $TEMPLATE has no callers marker"; }
    [ "$found_users" = 1 ] || { rm -f "$part"; die "template $TEMPLATE has no users marker"; }
    mv -f "$part" "$out"
    printf 'paperless-gate: name list: %s names\n' "$count" >&2
}

reload_nginx() {
    if [ -f "$RUN_DIR/nginx.pid" ]; then
        nginx -t -q -c "$RUN_DIR/nginx.conf" || die "rendered configuration does not load"
        nginx -s reload -c "$RUN_DIR/nginx.conf"
    fi
}

# watch_users: runs beside nginx and reloads it when the realm sync has
# replaced the list, through `gate.sh reload` in a process of its own (which
# keeps `set -e`). A change that cannot be loaded (a secret broke meanwhile, a
# configuration nginx refuses) stops the gate rather than keep the names of
# the old list admitted; the restart policy then runs `gate.sh run` again.
watch_users() {
    set +e
    while :; do
        sleep "$WATCH_SECONDS"
        [ "$(users_fingerprint)" = "$(cat "$RUN_DIR/users.sum" 2>/dev/null)" ] && continue
        if ! sh "$0" reload; then
            printf 'paperless-gate: the name list changed and could not be loaded; stopping\n' >&2
            kill -TERM "$1"
            exit 1
        fi
    done
}

command="${1:-}"
case "$command" in
    run)
        printf '%s' "$WATCH_SECONDS" | grep -Eqx '[1-9][0-9]{0,2}' \
            || die "GATE_WATCH_SECONDS must be a whole number of seconds from 1 to 999" 2
        ensure_mcp_secret
        render
        # After exec, this shell's process id is nginx' master.
        watch_users "$$" < /dev/null &
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
