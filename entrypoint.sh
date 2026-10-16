#!/usr/bin/env bash
#
# Container entrypoint. Order matters here:
#
#   1. Configure the panel through its CLI *before* it ever starts. The CLI
#      writes straight to /etc/x-ui/x-ui.db, so there is no reload to wait for.
#   2. Start the panel. It spawns Xray from bin/config.json.
#   3. Create the inbound through the HTTP API only when it does not exist.
#      Existing inbounds are never replaced with a canned payload, because that
#      would erase operator-created clients and transport options.
#   4. A newly-created inbound does not reliably regenerate bin/config.json, so
#      restart the panel once only in that case; Xray then sees the new inbound.
#   5. Render nginx and supervise both processes.
#
# If either process dies the container exits and Railway restarts it.

set -euo pipefail

XUI_DIR=/usr/local/x-ui
XUI_BIN="${XUI_DIR}/x-ui"
DB_FILE=/etc/x-ui/x-ui.db

# Scratch space for the rendered nginx config and the bootstrap handoff.
# /run is correct inside the container (it runs as root); fall back so the
# scripts are also runnable outside it, e.g. in CI.
RUNTIME_DIR="${RUNTIME_DIR:-}"
if [ -z "$RUNTIME_DIR" ]; then
  if mkdir -p /run/xui-railway 2>/dev/null && [ -w /run/xui-railway ]; then
    RUNTIME_DIR=/run/xui-railway
  else
    RUNTIME_DIR="$(mktemp -d)"
  fi
fi
export RUNTIME_DIR

# --- configuration -----------------------------------------------------------
# The panel renders dates in whatever zone the container is in, and Railway
# containers come up on UTC. An operator in Tehran picking "tomorrow" then reads
# a date three and a half hours off, which is enough for a client to look
# expired when it is not. Default to the operator's zone; override with TZ.
export TZ="${TZ:-Asia/Tehran}"

HTTP_PORT="${PORT:-${HTTP_PORT:-8080}}"     # Railway's routed PORT always wins
PANEL_PORT="${PANEL_PORT:-2053}"
SUB_PORT="${SUB_PORT:-2096}"
INBOUND_PORT="${INBOUND_PORT:-10085}"
PANEL_PATH="${PANEL_PATH:-/panel/}"
SUB_PATH="${SUB_PATH:-/sub/}"
SUB_JSON_PATH="${SUB_JSON_PATH:-/sub-json/}"
SUB_CLASH_PATH="${SUB_CLASH_PATH:-/sub-clash/}"
TUNNEL_PATH="${TUNNEL_PATH:-/ws}"
TRANSPORT="${TRANSPORT:-ws}"
CLIENT_EMAIL="${CLIENT_EMAIL:-user1}"
CLIENT_SUBID="${CLIENT_SUBID:-CATWOMAN}"
SUBPAGE_PORT="${SUBPAGE_PORT:-2097}"
BRAND="${BRAND:-CATWOMAN}"
# Names the configs exactly as they show up in the client app. {{EMAIL}} is the
# per-user email typed in the panel, so every client gets its own line.
REMARK_TEMPLATE="${REMARK_TEMPLATE:-${BRAND} • {{EMAIL}}}"
# The built-in subscription page has two slots of its own: a title rendered at
# the top of the card, and an "announce" box rendered below it. Both are plain
# panel settings, so the branding lives there rather than in a custom page.
SUB_TITLE="${SUB_TITLE:-$BRAND}"
SUB_ANNOUNCE="${SUB_ANNOUNCE:-این پنل توسط Catwoman ساخته شده}"
# subSupportUrl is the one field the built-in page puts in its <footer>, so it
# is the slot at the very bottom of the subscription page.
SUB_SUPPORT_URL="${SUB_SUPPORT_URL:-t.me/Catwoman_ZaHra}"

# Normalise PANEL_PATH: 3x-ui wants a leading and trailing slash, and nginx
# prefix matching needs exactly that form too.
case "$PANEL_PATH" in
  /) ;;
  /*/) ;;
  /*) PANEL_PATH="${PANEL_PATH}/" ;;
  *)  PANEL_PATH="/${PANEL_PATH}/" ;;
esac
case "$SUB_PATH" in
  /*/) ;;
  /*) SUB_PATH="${SUB_PATH}/" ;;
  *)  SUB_PATH="/${SUB_PATH}/" ;;
esac
case "$SUB_JSON_PATH" in
  /*/) ;;
  /*) SUB_JSON_PATH="${SUB_JSON_PATH}/" ;;
  *)  SUB_JSON_PATH="/${SUB_JSON_PATH}/" ;;
esac
case "$SUB_CLASH_PATH" in
  /*/) ;;
  /*) SUB_CLASH_PATH="${SUB_CLASH_PATH}/" ;;
  *)  SUB_CLASH_PATH="/${SUB_CLASH_PATH}/" ;;
esac

  export PANEL_PATH SUB_PATH SUB_JSON_PATH SUB_CLASH_PATH TUNNEL_PATH
  export HTTP_PORT PANEL_PORT SUB_PORT INBOUND_PORT
  export TRANSPORT CLIENT_EMAIL CLIENT_SUBID
  # BRAND is read inside bootstrap.sh (the inbound gets named after it), and
  # bootstrap.sh runs under `set -u`, so it has to be exported -- an unexported
  # variable there is an "unbound variable" error, not a silent empty string.
  export BRAND REMARK_TEMPLATE
  export PANEL_PORT INBOUND_PORT

# Every public config must describe Railway's edge, never Xray's internal
# listener. The platform domain wins over a stale manually-entered value, and
# the public endpoint is always HTTPS on 443 even though the container itself
# receives plain HTTP from Railway.
PUBLIC_HOST="${RAILWAY_PUBLIC_DOMAIN:-${PUBLIC_HOST:-}}"
if [ -n "$PUBLIC_HOST" ]; then
  RAW_PUBLIC_HOST="$PUBLIC_HOST"
  PUBLIC_HOST="$(PYTHONPATH=/opt python3 - "$RAW_PUBLIC_HOST" <<'PY'
import sys
from public_endpoint import normalize_public_host
print(normalize_public_host(sys.argv[1]) or "")
PY
)"
  if [ -z "$PUBLIC_HOST" ]; then
    printf '[entrypoint] WARNING: ignoring invalid public hostname %q; a real request can repair it\n' \
      "$RAW_PUBLIC_HOST"
  fi
fi
PUBLIC_PORT=443
PUBLIC_TLS=tls
export PUBLIC_HOST PUBLIC_PORT PUBLIC_TLS

log() { printf '[entrypoint] %s\n' "$*"; }

# Reads the live panel username out of the database, so a credential changed
# from the panel UI survives a reboot instead of being overwritten by the
# defaults. The column name cannot be a bound parameter (SQLite would treat it
# as a string literal), so it is matched against a whitelist instead.
read_credential() {
  case "$1" in
    username) COL=username ;;
    password) COL=password ;;
    *) return 1 ;;
  esac
  python3 - "$COL" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    row = con.execute(f"SELECT {sys.argv[1]} FROM users ORDER BY id LIMIT 1").fetchone()
    print(row[0] if row else "")
except Exception:
    print("")
PY
}

# --- embedded bootstrap -----------------------------------------------------
# Kept in this one file so the repository stays small enough to create from
# a phone. It is written out verbatim at boot (quoted heredoc: no expansion)
# so it stays one reviewable bash script rather than a quoting maze.
RUNTIME_DIR_BOOT="${RUNTIME_DIR}"
write_bootstrap() {
  cat > "${RUNTIME_DIR_BOOT}/bootstrap.sh" <<'BOOTSTRAP_EOF'
#
# Bootstraps the panel and one ready-to-use inbound, talking to the panel's own
# HTTP API. Runs once per boot; it is idempotent, so a second boot is a no-op.
#
# The API flow below is integration-tested against 3x-ui v3.10.2 (and retained
# compatibility with a v3.9.0 database), because two behaviours are easy to
# misread from older templates:
#
#   1. The API base is "<webBasePath>panel/api/". With PANEL_PATH=/panel/ that
#      reads /panel/panel/api/... and looks like a typo. With /abc/ it is
#      /abc/panel/api/. Guessing "<webBasePath>api/" gives 404 on every call.
#   2. Unsafe requests need an X-CSRF-Token header. Without it POST /login
#      answers 403 with an empty body, which reads like bad credentials.
#
# Usage: bootstrap.sh   (reads configuration from the environment)

set -euo pipefail

PANEL_PATH="${PANEL_PATH:-/panel/}"
PANEL_PORT="${PANEL_PORT:-2053}"
INBOUND_PORT="${INBOUND_PORT:-10085}"
SUB_PATH="${SUB_PATH:-/sub/}"
SUB_JSON_PATH="${SUB_JSON_PATH:-/sub-json/}"
SUB_CLASH_PATH="${SUB_CLASH_PATH:-/sub-clash/}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
PUBLIC_PORT=443
PUBLIC_TLS=tls                            # Railway's edge terminates TLS
TRANSPORT="${TRANSPORT:-ws}"             # ws | xhttp  (ws works in every client)
TUNNEL_PATH="${TUNNEL_PATH:-/ws}"                 # public path nginx exposes; also the internal path
CLIENT_EMAIL="${CLIENT_EMAIL:-user1}"
CLIENT_SUBID="${CLIENT_SUBID:-CATWOMAN}"
INBOUND_REMARK="${INBOUND_REMARK:-$BRAND}"
ADMIN_USERNAME="${ADMIN_USERNAME:?ADMIN_USERNAME is required}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
BOOTSTRAP_TIMEOUT="${BOOTSTRAP_TIMEOUT:-90}"

BASE="http://127.0.0.1:${PANEL_PORT}${PANEL_PATH}"
API="${BASE}panel/api"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

log()  { printf '[bootstrap] %s\n' "$*"; }
die()  { printf '[bootstrap] ERROR: %s\n' "$*" >&2; return 1; }

# APPLY_CREDS=0 means the entrypoint deliberately left the stored credentials
# alone, so what we hold is a bcrypt hash rather than a password and there is
# nothing to log in with. The inbound already exists from the first boot, so
# leaving it untouched is the correct no-op.
if [ "${APPLY_CREDS:-1}" = "0" ]; then
  log "credentials come from the panel database; skipping inbound bootstrap"
  log "to re-bootstrap the inbound, set ADMIN_PASSWORD in Railway Variables and redeploy"
  exit 0
fi

# --- wait for the panel ------------------------------------------------------
deadline=$(( $(date +%s) + BOOTSTRAP_TIMEOUT ))
until curl -fsS -o /dev/null --max-time 5 "${BASE}"; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    die "panel did not answer on ${BASE} within ${BOOTSTRAP_TIMEOUT}s"
  fi
  sleep 1
done
log "panel is up at ${BASE}"

# --- session -----------------------------------------------------------------
# The CSRF token is bound to the session cookie, so fetch it with the same jar
# that the login POST uses.
csrf_token() {
  curl -fsS -b "$JAR" -c "$JAR" --max-time 10 "${BASE}csrf-token" \
    | jq -er '.obj'
}

CSRF="$(csrf_token)" || die "could not mint a CSRF token"
LOGIN_BODY=$(curl -sS -b "$JAR" -c "$JAR" --max-time 10 \
  -X POST "${BASE}login" \
  -H "X-CSRF-Token: ${CSRF}" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=${ADMIN_USERNAME}" \
  --data-urlencode "password=${ADMIN_PASSWORD}")
echo "$LOGIN_BODY" | jq -e '.success == true' >/dev/null \
  || die "panel login failed: ${LOGIN_BODY}"
log "logged in as ${ADMIN_USERNAME}"

api_get() {
  curl -fsS -b "$JAR" --max-time 20 "${API}$1"
}
api_post() {
  curl -fsS -b "$JAR" --max-time 30 -X POST "${API}$1" \
    -H "X-CSRF-Token: ${CSRF}" -H 'Content-Type: application/json' --data @"$2"
}

# --- inbound -----------------------------------------------------------------
EXISTING="$(api_get /inbounds/list | jq -c '[.obj[]? | select(.port == '"${INBOUND_PORT}"')]')"
COUNT="$(echo "$EXISTING" | jq 'length')"
if [ "$COUNT" -gt 1 ]; then
  die "more than one inbound uses internal port ${INBOUND_PORT}; refusing to guess which one is managed"
fi
if [ "$COUNT" -eq 1 ]; then
  EXISTING_REMARK="$(echo "$EXISTING" | jq -r '.[0].remark // empty')"
  if [ "$EXISTING_REMARK" != "$INBOUND_REMARK" ]; then
    die "port ${INBOUND_PORT} belongs to inbound '${EXISTING_REMARK}', not '${INBOUND_REMARK}'; preserving it without changes"
  fi
fi

UUID="$(cat /proc/sys/kernel/random/uuid)"
if [ "$COUNT" -gt 0 ]; then
  UUID="$(echo "$EXISTING" | jq -r '.[0].settings.clients[0].id // empty')"
  [ -n "$UUID" ] || UUID="$(cat /proc/sys/kernel/random/uuid)"
  log "inbound on port ${INBOUND_PORT} already exists; reusing uuid ${UUID}"
fi

# Build the payload in Python so the nested JSON strings stay valid without a
# quoting maze. 3x-ui stores settings/streamSettings as JSON *strings*.
PAYLOAD="$(mktemp)"
CLIENT_UUID="$UUID" \
INBOUND_PORT="$INBOUND_PORT" \
TRANSPORT="$TRANSPORT" \
TUNNEL_PATH="$TUNNEL_PATH" \
PUBLIC_HOST="$PUBLIC_HOST" \
PUBLIC_PORT="$PUBLIC_PORT" \
PUBLIC_TLS="$PUBLIC_TLS" \
CLIENT_EMAIL="$CLIENT_EMAIL" \
CLIENT_SUBID="$CLIENT_SUBID" \
INBOUND_REMARK="$INBOUND_REMARK" \
python3 - > "$PAYLOAD" <<'PY'
import json, os

transport  = os.environ["TRANSPORT"]
path       = os.environ["TUNNEL_PATH"]
host       = os.environ["PUBLIC_HOST"]
port       = int(os.environ["PUBLIC_PORT"])
force_tls  = os.environ["PUBLIC_TLS"]
in_port    = int(os.environ["INBOUND_PORT"])

stream = {"network": transport, "security": "none"}

# externalProxy is what rewrites the address in generated links and in the
# subscription. Without it the link says localhost:<internal port>.
if host:
    stream["externalProxy"] = [{
        "forceTls": force_tls, "dest": host, "port": port,
        "remark": os.environ["INBOUND_REMARK"], "sni": host,
    }]

if transport == "ws":
    stream["wsSettings"] = {
        "acceptProxyProtocol": False, "path": path, "host": host, "headers": {},
    }
else:
    stream["xhttpSettings"] = {
        "path": path, "host": host, "mode": "auto", "extra": {},
        "maxConnections": 0, "maxConnectionSecs": 0,
        "scMaxEachPostBytes": 0, "scMinPostsIntervalMs": 0,
        "scKeepAliveIntervalSecs": 0,
        "xPaddingBytes": "100-1000", "noGRPCHeader": False,
        "grpcFirstPacketWait": 0,
        "xmux": {
            "maxConcurrency": "16-32", "maxConnections": 0,
            "cMaxRequestsSec": 0, "cMaxRequestSize": 0,
            "minVersion": "2", "maxVersion": "3",
            "hMaxRequestSecs": 30, "hKeepAlivePeriod": 20,
        },
    }

settings = {
    "clients": [{
        "id": os.environ["CLIENT_UUID"], "flow": "",
        "email": os.environ["CLIENT_EMAIL"], "limitIp": 0, "totalGB": 0,
        "expiryTime": 0, "enable": True, "tgId": "",
        "subId": os.environ["CLIENT_SUBID"], "reset": 0,
    }],
    "decryption": "none", "fallbacks": [],
}

print(json.dumps({
    "up": 0, "down": 0, "total": 0,
    "remark": os.environ["INBOUND_REMARK"],
    "enable": True, "expiryTime": 0,
    "listen": "", "port": in_port, "protocol": "vless",
    "settings": json.dumps(settings),
    "streamSettings": json.dumps(stream),
    "tag": f"in-{in_port}-tcp",
    "sniffing": json.dumps({
        "enabled": True,
        "destOverride": ["http", "tls", "quic", "fakedns"],
        "metadataOnly": False, "routeOnly": False,
    }),
}))
PY

if [ "$COUNT" -gt 0 ]; then
  # Never PUT a canned one-client payload over an existing inbound. Doing that
  # would silently delete every client and transport option the operator added
  # in the panel. Public endpoint repair is handled selectively before startup.
  VERB="reused"
  rm -f "$PAYLOAD"
  log "existing inbound preserved on internal port ${INBOUND_PORT}"
else
  RESP="$(api_post /inbounds/add "$PAYLOAD")"
  echo "$RESP" | jq -e '.success == true' >/dev/null \
    || die "inbound creation failed: ${RESP}"
  rm -f "$PAYLOAD"
  VERB="created"
  log "inbound created: vless/${TRANSPORT} on internal port ${INBOUND_PORT}, path ${TUNNEL_PATH}"
fi

# --- subscription path -------------------------------------------------------
# The panel generates a random subPath and serves it on its own port. nginx
# needs it to map the stable public /sub/ onto it, so hand it back to the
# caller on stdout as a single line the entrypoint can read.
SUB_REAL_PATH="$(python3 - <<'PY'
import sqlite3, os
db = "/etc/x-ui/x-ui.db"
try:
    con = sqlite3.connect(db)
    row = con.execute("SELECT value FROM settings WHERE key='subPath'").fetchone()
    print(row[0] if row and row[0] else "/subs/")
except Exception:
    print("/subs/")
PY
)"
log "panel subscription path is ${SUB_REAL_PATH}"

# --- subURI ------------------------------------------------------------------
# Left empty, the panel builds the subscription link it shows on the Clients
# page from subDomain and subPort, which gives
# http://<domain>:2096<subPath><subId>. Railway exposes exactly one public port
# over HTTPS, so that link can never be reached from a browser or an app.
# The three public URI overrides cover raw, JSON and Clash subscriptions. Never
# retain a previous domain or expose any internal random path and :2096.
if [ -n "$PUBLIC_HOST" ]; then
  python3 - "$PUBLIC_HOST" "$SUB_PATH" "$SUB_JSON_PATH" "$SUB_CLASH_PATH" <<'PY'
import sqlite3, sys
host = sys.argv[1]
values = {
    "subURI": f"https://{host}{sys.argv[2]}",
    "subJsonURI": f"https://{host}{sys.argv[3]}",
    "subClashURI": f"https://{host}{sys.argv[4]}",
}
db = "/etc/x-ui/x-ui.db"
try:
    con = sqlite3.connect(db)
    changed = []
    for key, want in values.items():
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        cur = row[0] if row else ""
        if cur == want:
            continue
        if row:
            con.execute("UPDATE settings SET value=? WHERE key=?", (want, key))
        else:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, want))
        changed.append(f"{key}={want}")
    if changed:
        con.commit()
        print("[bootstrap] public subscription URIs set: " + ", ".join(changed))
except Exception as e:
    print(f"[bootstrap] WARNING: could not set public subscription URIs: {e}")
PY
else
  log "public host is not available during bootstrap; the first public panel/sub request will set subURI"
fi

# --- report ------------------------------------------------------------------
# The entrypoint sources this file, so every value has to be shell-quoted.
# A vless:// link ends in "#remark", and an unquoted '#' starts a comment:
# CLIENT_LINK=vless://...#remark silently assigns the part before the '#'.
shell_quote() {
  # Wrap in single quotes, escaping any single quote inside the value.
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

LINKS="$(api_get /inbounds/allLinks | jq -r '.obj[]?' | head -1 || true)"
{
  printf 'PANEL_API_OK=1\n'
  printf 'SUB_REAL_PATH=%s\n'  "$(shell_quote "$SUB_REAL_PATH")"
  printf 'CLIENT_UUID=%s\n'    "$(shell_quote "$UUID")"
  printf 'INBOUND_VERB=%s\n'   "$(shell_quote "$VERB")"
  if [ "$VERB" = "created" ]; then
    printf 'INBOUND_CHANGED=1\n'
  else
    printf 'INBOUND_CHANGED=0\n'
  fi
  # With no domain yet, 3x-ui's temporary direct link is localhost:10085.
  # Never print or hand that internal bootstrap value to the operator; the
  # first public request will create a correct HTTPS/443 link instead.
  if [ -n "$PUBLIC_HOST" ] && [ -n "$LINKS" ]; then
    printf 'CLIENT_LINK=%s\n'  "$(shell_quote "$LINKS")"
  fi
} > "${RUNTIME_DIR}/bootstrap.env"

log "wrote ${RUNTIME_DIR}/bootstrap.env"
BOOTSTRAP_EOF
  chmod +x "${RUNTIME_DIR_BOOT}/bootstrap.sh"
}

# --- 1. panel settings -------------------------------------------------------
mkdir -p /etc/x-ui /var/log/x-ui

# Credentials. The trap here is that writing them on every boot would undo a
# password you changed from the panel UI -- the CLI overwrites the database
# unconditionally. So:
#
#   * no database yet (first boot, empty Volume): seed admin/admin so the
#     service is usable with zero Variables.
#   * a valid database user exists: leave it alone, so what you set in Panel
#     Settings survives restarts and redeploys.
#   * ADMIN_PASSWORD set explicitly in Railway Variables: wins on every boot
#     (ADMIN_USERNAME is optional and defaults to the stored username). This is
#     the way back in if you ever lock yourself out.
#   * an empty/partial database file is treated like a first boot instead of
#     trapping the service with an empty username and no usable password.
STORED_USERNAME=""
STORED_PASSWORD=""
if [ -s "$DB_FILE" ]; then
  STORED_USERNAME="$(read_credential username)"
  STORED_PASSWORD="$(read_credential password)"
fi
if [ -n "$STORED_USERNAME" ] && [ -n "$STORED_PASSWORD" ]; then
  if [ -n "${ADMIN_PASSWORD:-}" ]; then
    ADMIN_USERNAME="${ADMIN_USERNAME:-$STORED_USERNAME}"
    APPLY_CREDS=1
    CREDS_NOTE="from Railway Variables"
  else
    if [ -n "${ADMIN_USERNAME:-}" ] && [ "$ADMIN_USERNAME" != "$STORED_USERNAME" ]; then
      log "WARNING: ADMIN_USERNAME without ADMIN_PASSWORD cannot update credentials; keeping the stored username"
    fi
    APPLY_CREDS=0
    ADMIN_USERNAME="$STORED_USERNAME"
    ADMIN_PASSWORD="$STORED_PASSWORD"
    CREDS_NOTE="kept from the panel database"
  fi
else
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
  APPLY_CREDS=1
  if [ "$ADMIN_USERNAME" = admin ] && [ "$ADMIN_PASSWORD" = admin ]; then
    DEFAULT_CREDS=1
  fi
  CREDS_NOTE="seeded on first boot"
fi
export ADMIN_USERNAME ADMIN_PASSWORD APPLY_CREDS
"$XUI_BIN" setting -port "$PANEL_PORT"          >/dev/null
"$XUI_BIN" setting -webBasePath "$PANEL_PATH"   >/dev/null
"$XUI_BIN" setting -listenIP 127.0.0.1          >/dev/null
if [ "$APPLY_CREDS" = "1" ]; then
  "$XUI_BIN" setting -username "$ADMIN_USERNAME" -password "$ADMIN_PASSWORD" >/dev/null
fi

# remarkTemplate has no CLI flag, so it goes straight into the settings table.
# It is what every share link and subscription entry is named after, which is
# where the branding the clients actually see comes from. Written before the
# panel starts so the first render already uses it. Only overwrite when the
# stored value is still the upstream default or empty, so a template someone
# tuned by hand in Panel Settings is not clobbered on every restart.
REMARK_DEFAULT='{{INBOUND}}-{{EMAIL}}|📊{{TRAFFIC_LEFT}}|⏳{{DAYS_LEFT}}D'
python3 - "$REMARK_TEMPLATE" "$REMARK_DEFAULT" <<'PY'
import sqlite3, sys
want, upstream = sys.argv[1], sys.argv[2]
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    row = con.execute("SELECT value FROM settings WHERE key='remarkTemplate'").fetchone()
    cur = row[0] if row else ""
    if cur and cur != upstream:
        print(f"[entrypoint] remarkTemplate already customised; leaving {cur!r}")
    else:
        if row:
            con.execute("UPDATE settings SET value=? WHERE key='remarkTemplate'", (want,))
        else:
            con.execute("INSERT INTO settings(key,value) VALUES('remarkTemplate',?)", (want,))
        con.commit()
        print(f"[entrypoint] remarkTemplate set to {want!r}")
except Exception as e:
    print(f"[entrypoint] WARNING: could not set remarkTemplate: {e}")
PY

# subTitle and subAnnounce are the two slots on 3x-ui's own subscription page:
# the title at the top of the card and the announce box under it. Same rule as
# remarkTemplate -- write them when unset or still empty, never over something
# you typed into Panel Settings yourself.
python3 - "$SUB_TITLE" "$SUB_ANNOUNCE" "$SUB_SUPPORT_URL" <<'PY'
import sqlite3, sys
values = {"subTitle": sys.argv[1], "subAnnounce": sys.argv[2],
          "subSupportUrl": sys.argv[3]}
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    for key, want in values.items():
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        cur = row[0] if row else ""
        if cur:
            print(f"[entrypoint] {key} already set; leaving {cur!r}")
            continue
        if row:
            con.execute("UPDATE settings SET value=? WHERE key=?", (want, key))
        else:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, want))
        print(f"[entrypoint] {key} set to {want!r}")
    con.commit()
except Exception as e:
    print(f"[entrypoint] WARNING: could not set the subscription page text: {e}")
PY

# Existing deployments carry the old default name. Bootstrap only runs when
# credentials are being applied, so anyone who changed their password in the
# panel and dropped ADMIN_PASSWORD from Variables would never get the rename --
# their inbound would keep the old label for ever. So rename it here instead,
# unconditionally and idempotently, before the panel reads the table.
#
# Only the managed internal port and a value exactly equal to the old default
# are touched. Other inbounds and names typed in the panel survive every boot.
python3 - "$BRAND" "$INBOUND_PORT" <<'PY'
import json, sqlite3, sys
brand, inbound_port = sys.argv[1], int(sys.argv[2])
OLD = "railway"
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    rows = con.execute(
        "SELECT id, remark, stream_settings FROM inbounds WHERE port=?",
        (inbound_port,),
    ).fetchall()
    renamed = 0
    for iid, remark, ss in rows:
        if remark == OLD:
            con.execute("UPDATE inbounds SET remark=? WHERE id=?", (brand, iid))
            renamed += 1
        # The same old label is also stored inside streamSettings, under
        # externalProxy -- that is the row the panel shows in its Hosts view.
        if ss:
            try:
                obj = json.loads(ss)
            except Exception:
                continue
            proxies = obj.get("externalProxy")
            if isinstance(proxies, list):
                dirty = False
                for p in proxies:
                    if isinstance(p, dict) and p.get("remark") == OLD:
                        p["remark"] = brand
                        dirty = True
                if dirty:
                    con.execute("UPDATE inbounds SET stream_settings=? WHERE id=?",
                                (json.dumps(obj, ensure_ascii=False), iid))
                    renamed += 1
    # v3.10 migrated each legacy externalProxy into its own Hosts row. Keep the
    # managed row recognisable so the public-endpoint synchronizer can update it
    # without ever touching Hosts the operator added by hand.
    changed = con.execute(
        "UPDATE hosts SET remark=? WHERE remark=? AND inbound_id IN "
        "(SELECT id FROM inbounds WHERE port=?)",
        (brand, OLD, inbound_port),
    ).rowcount
    renamed += max(changed, 0)
    if renamed:
        con.commit()
        print(f"[entrypoint] renamed the old '{OLD}' label to '{brand}' "
              f"in {renamed} place(s)")
except Exception as e:
    print(f"[entrypoint] WARNING: could not rename the old inbound label: {e}")
PY

# Keep the managed Railway endpoint complete even when the stored admin password
# is a bcrypt hash and the bootstrap cannot log in. v3.10 generates subscription
# links from the Hosts table, while direct share links can still read the legacy
# externalProxy/stream settings; the helper updates both plus all public
# subscription URIs atomically. Other inbounds and custom Hosts stay untouched.
if [ -n "$PUBLIC_HOST" ]; then
  if ! python3 /opt/public_endpoint.py "$PUBLIC_HOST" \
      --db "$DB_FILE" \
      --inbound-port "$INBOUND_PORT" \
      --brand "$BRAND" \
      --sub-path "$SUB_PATH" \
      --sub-json-path "$SUB_JSON_PATH" \
      --sub-clash-path "$SUB_CLASH_PATH" \
      --tunnel-path "$TUNNEL_PATH"; then
    log "WARNING: could not synchronize the public 443/TLS endpoint"
  fi
else
  log "public domain not available at boot; the first public panel request will synchronize it"
fi

# The shared subscription id used to default to "mobile", which is also what the
# link on everyone's phone says (/sub/mobile). Renaming the default alone would
# strand every client created before this change: their sub_id would still read
# "mobile" while the link handed out said "CATWOMAN", so the page would come up
# empty and the app would report no servers.
#
# So move the existing clients across too. Only a sub_id that is exactly the old
# default is touched -- anyone who put their own subId on a client keeps it.
python3 - "$CLIENT_SUBID" "$INBOUND_PORT" <<'PY'
import json, sqlite3, sys
want, inbound_port = sys.argv[1], int(sys.argv[2])
OLD = "mobile"
if want == OLD:
    sys.exit(0)
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    rows = con.execute(
        "SELECT DISTINCT c.id, c.email FROM clients c "
        "JOIN client_inbounds ci ON ci.client_id=c.id "
        "JOIN inbounds i ON i.id=ci.inbound_id "
        "WHERE c.sub_id=? AND i.port=?",
        (OLD, inbound_port),
    ).fetchall()
    if rows:
        ids = [row[0] for row in rows]
        marks = ",".join("?" for _ in ids)
        con.execute(
            f"UPDATE clients SET sub_id=? WHERE id IN ({marks})",
            (want, *ids),
        )
        # Keep the legacy JSON projection consistent as well; older code paths
        # and backups still inspect this copy.
        for inbound_id, raw in con.execute(
                "SELECT id, settings FROM inbounds WHERE port=?",
                (inbound_port,)).fetchall():
            try:
                obj = json.loads(raw)
            except (TypeError, ValueError):
                continue
            changed = False
            for client in obj.get("clients", []):
                if isinstance(client, dict) and client.get("subId") == OLD:
                    client["subId"] = want
                    changed = True
            if changed:
                con.execute(
                    "UPDATE inbounds SET settings=? WHERE id=?",
                    (json.dumps(obj, ensure_ascii=False), inbound_id),
                )
        con.commit()
        names = ", ".join(r[1] for r in rows)
        print(f"[entrypoint] moved {len(rows)} client(s) from subId '{OLD}' to "
              f"'{want}': {names}. The subscription link is now /sub/{want}; "
              f"the old /sub/{OLD} no longer resolves, so re-add the link on "
              f"any phone still using it.")
except Exception as e:
    print(f"[entrypoint] WARNING: could not move the old subId: {e}")
PY

# A client whose expiryTime is stored in seconds rather than milliseconds reads
# as a date in 1970, so it shows as expired the moment it is created. The panel
# form sends milliseconds, so this should never happen -- but if it ever does,
# say so in the log rather than leaving a client that silently looks dead.
python3 - <<'PYCHK'
import sqlite3, time
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    for email, sub_id, exp in con.execute(
            "SELECT email, sub_id, expiry_time FROM clients"):
        # expiry_time is milliseconds. Anything positive but far too small to be
        # a millisecond timestamp is a date entered as seconds, which the panel
        # reads as January 1970 and therefore shows as long expired.
        if exp and 0 < exp < 10**11:
            print("[entrypoint] WARNING: client %r (subId %r) has expiry_time=%s."
                  " That is a date in seconds, but this field is milliseconds,"
                  " so the panel reads it as %s and shows the client as expired."
                  " Open the client in the panel and set the expiry again, or"
                  " set it to 0 for unlimited."
                  % (email, sub_id, exp,
                     time.strftime("%Y-%m-%d", time.gmtime(exp / 1000.0))))
except Exception as e:
    print("[entrypoint] WARNING: could not check client expiry values: %s" % e)
PYCHK

log "panel configured: port ${PANEL_PORT}, base path ${PANEL_PATH}; credentials ${CREDS_NOTE}"

# --- 2/3/4. panel + inbound --------------------------------------------------
start_panel() {
  ( cd "$XUI_DIR" && exec "$XUI_BIN" run ) &
  PANEL_PID=$!
  log "panel started (pid ${PANEL_PID})"
}

stop_panel() {
  if [ -n "${PANEL_PID:-}" ] && kill -0 "$PANEL_PID" 2>/dev/null; then
    log "stopping panel"
    kill -TERM "$PANEL_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$PANEL_PID" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$PANEL_PID" 2>/dev/null || true
    wait "$PANEL_PID" 2>/dev/null || true
  fi
  PANEL_PID=""
}

# Clean up children on every exit path, including an nginx bind/config failure.
# Railway kills the whole container, but doing this explicitly prevents a
# half-started sidecar or daemonised nginx from surviving during local tests.
SUBPAGE_PID=""
NGINX_STARTED=0
RENDERED=""
cleanup() {
  status=$?
  trap - EXIT TERM INT
  if [ "$NGINX_STARTED" = "1" ] && [ -n "$RENDERED" ]; then
    nginx -s quit -c "$RENDERED" 2>/dev/null || true
  fi
  if [ -n "${SUBPAGE_PID:-}" ]; then
    kill "$SUBPAGE_PID" 2>/dev/null || true
    wait "$SUBPAGE_PID" 2>/dev/null || true
    SUBPAGE_PID=""
  fi
  stop_panel
  exit "$status"
}
trap cleanup EXIT
trap 'exit 0' TERM INT

start_panel
write_bootstrap
rm -f "${RUNTIME_DIR}/bootstrap.env"
"${RUNTIME_DIR}/bootstrap.sh" || log "WARNING: bootstrap did not complete; the panel is still usable"

if [ -s "${RUNTIME_DIR}/bootstrap.env" ]; then
  # shellcheck disable=SC1091
  . "${RUNTIME_DIR}/bootstrap.env"
  # Only a newly-created inbound needs an Xray relaunch. Existing inbounds are
  # deliberately never overwritten by bootstrap.
  if [ "${INBOUND_CHANGED:-0}" = "1" ]; then
    stop_panel
    start_panel
    sleep 3
  fi
fi

# --- 5. nginx ----------------------------------------------------------------
# Bootstrap is skipped on later boots (no stored password to log in with), so
# read all three random subscription paths from the database.
read_db_setting() {
  python3 - "$1" "$2" <<'PY'
import sqlite3, sys
key, fallback = sys.argv[1:3]
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    print(row[0] if row and row[0] else fallback)
except Exception:
    print(fallback)
PY
}
SUB_REAL_PATH="${SUB_REAL_PATH:-$(read_db_setting subPath /subs/)}"
SUB_JSON_REAL_PATH="${SUB_JSON_REAL_PATH:-$(read_db_setting subJsonPath /json/)}"
SUB_CLASH_REAL_PATH="${SUB_CLASH_REAL_PATH:-$(read_db_setting subClashPath /clash/)}"
# Bare paths map to the default Sub ID; prefix paths carry an explicit Sub ID.
SUB_PATH_NOSLASH="${SUB_PATH%/}"
SUB_JSON_PATH_NOSLASH="${SUB_JSON_PATH%/}"
SUB_CLASH_PATH_NOSLASH="${SUB_CLASH_PATH%/}"
[ -n "$SUB_PATH_NOSLASH" ] || SUB_PATH_NOSLASH="/"
[ -n "$SUB_JSON_PATH_NOSLASH" ] || SUB_JSON_PATH_NOSLASH="/sub-json"
[ -n "$SUB_CLASH_PATH_NOSLASH" ] || SUB_CLASH_PATH_NOSLASH="/sub-clash"
RENDERED="${RUNTIME_DIR}/nginx.conf"
sed -e "s|@@HTTP_PORT@@|${HTTP_PORT}|g" \
    -e "s|@@PANEL_PORT@@|${PANEL_PORT}|g" \
    -e "s|@@PANEL_PATH@@|${PANEL_PATH}|g" \
    -e "s|@@SUB_PORT@@|${SUB_PORT}|g" \
    -e "s|@@SUB_PATH@@|${SUB_PATH}|g" \
    -e "s|@@SUB_PATH_NOSLASH@@|${SUB_PATH_NOSLASH}|g" \
    -e "s|@@SUB_REAL_PATH@@|${SUB_REAL_PATH}|g" \
    -e "s|@@SUB_JSON_PATH@@|${SUB_JSON_PATH}|g" \
    -e "s|@@SUB_JSON_PATH_NOSLASH@@|${SUB_JSON_PATH_NOSLASH}|g" \
    -e "s|@@SUB_JSON_REAL_PATH@@|${SUB_JSON_REAL_PATH}|g" \
    -e "s|@@SUB_CLASH_PATH@@|${SUB_CLASH_PATH}|g" \
    -e "s|@@SUB_CLASH_PATH_NOSLASH@@|${SUB_CLASH_PATH_NOSLASH}|g" \
    -e "s|@@SUB_CLASH_REAL_PATH@@|${SUB_CLASH_REAL_PATH}|g" \
    -e "s|@@INBOUND_PORT@@|${INBOUND_PORT}|g" \
    -e "s|@@TUNNEL_PATH@@|${TUNNEL_PATH}|g" \
    -e "s|@@SUBPAGE_PORT@@|${SUBPAGE_PORT}|g" \
    -e "s|@@PUBLIC_BRAND@@|${BRAND}|g" \
    /opt/nginx.conf.template > "$RENDERED"

# --- 4b. subscription page ----------------------------------------------------
# Renders /sub. Needs the panel's real subPath to reach the sub server, so it
# starts after bootstrap has handed it back.
if [ -f /opt/subserver.py ]; then
  SUBPAGE_PORT="$SUBPAGE_PORT" \
  SUB_PORT="$SUB_PORT" \
  SUB_REAL_PATH="$SUB_REAL_PATH" \
  SUB_JSON_REAL_PATH="$SUB_JSON_REAL_PATH" \
  SUB_CLASH_REAL_PATH="$SUB_CLASH_REAL_PATH" \
  SUB_PATH="$SUB_PATH" \
  SUB_JSON_PATH="$SUB_JSON_PATH" \
  SUB_CLASH_PATH="$SUB_CLASH_PATH" \
  CLIENT_SUBID="$CLIENT_SUBID" \
  DB_FILE="$DB_FILE" \
  INBOUND_PORT="$INBOUND_PORT" \
  BRAND="$BRAND" \
  TUNNEL_PATH="$TUNNEL_PATH" \
    python3 /opt/subserver.py &
  SUBPAGE_PID=$!
  log "subscription page started (pid ${SUBPAGE_PID}) on :${SUBPAGE_PORT}"
  # nginx blocks panel/sub requests on a tiny local auth subrequest, so wait for
  # Python to bind instead of letting the first phone request race its startup.
  for _ in $(seq 1 30); do
    curl -fsS -o /dev/null --max-time 1 \
      "http://127.0.0.1:${SUBPAGE_PORT}/__catwoman_sync_host" && break
    sleep 0.2
  done
fi

nginx -t -c "$RENDERED" || { log "FATAL: rendered nginx config is invalid"; exit 1; }
nginx -c "$RENDERED" || { log "FATAL: nginx would not start on :${HTTP_PORT}"; exit 1; }
NGINX_STARTED=1
# nginx daemonises; give it a moment and confirm it is really serving before
# announcing success, so a bind failure surfaces here instead of as a 502.
for _ in $(seq 1 10); do
  curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:${HTTP_PORT}/healthz" && break
  sleep 1
done
log "nginx listening on :${HTTP_PORT}"

# --- report ------------------------------------------------------------------
HOST_FOR_LINKS="${PUBLIC_HOST:-<your-domain>}"
PANEL_URL="https://${HOST_FOR_LINKS}${PANEL_PATH}"
SUB_URL="https://${HOST_FOR_LINKS}${SUB_PATH}${CLIENT_SUBID}"

echo
echo "=============================================================="
echo "  3x-ui on Railway — ready"
echo "=============================================================="
echo "  Panel          ${PANEL_URL}"
echo "  Username       ${ADMIN_USERNAME}"
if [ "$APPLY_CREDS" = "1" ]; then
  if [ -n "${DEFAULT_CREDS:-}" ]; then
    echo "  Password       admin   (${CREDS_NOTE})"
  else
    echo "  Password       (set from Railway Variables; hidden from logs)"
  fi
fi
if [ -n "${DEFAULT_CREDS:-}" ]; then
  echo
  echo "  !! Still on admin/admin. Change it in Panel Settings -> the change"
  echo "     sticks across restarts, nothing here overwrites it."
fi
echo "  Subscription   ${SUB_URL}"
echo "                 ^ Open it in a browser: 3x-ui's own subscription page."
echo "                   Paste it into a VPN app: the same address hands the"
echo "                   app the raw feed. The panel decides by who is asking."
echo "                   Title: ${SUB_TITLE}"
echo "                 ^ give every new client subId \"${CLIENT_SUBID}\" and it"
echo "                   appears in this same link automatically. No new link,"
echo "                   no new inbound, nothing to hand out per person."
if [ -n "${CLIENT_LINK:-}" ]; then
  echo
  echo "  Client config:"
  echo "  ${CLIENT_LINK}"
fi
echo
echo "  Transport      vless + ${TRANSPORT} on path ${TUNNEL_PATH}"
echo "  Internal       panel :${PANEL_PORT}, sub :${SUB_PORT}, inbound :${INBOUND_PORT}"
if [ -z "$PUBLIC_HOST" ]; then
  echo
  echo "  !! No public domain was available during startup. Generate a"
  echo "     Railway Domain, then open the public panel or subscription once;"
  echo "     the request hostname will repair and persist every public link."
fi
echo "=============================================================="
echo

# --- supervise ---------------------------------------------------------------
while true; do
  if ! kill -0 "$PANEL_PID" 2>/dev/null; then
    log "panel exited; shutting down so Railway restarts the container"
    exit 1
  fi
  if ! pgrep -x nginx >/dev/null 2>&1; then
    log "nginx exited; shutting down so Railway restarts the container"
    exit 1
  fi
  # The subscription page is not fatal: without it the raw /sub/<id> endpoint
  # still works, so restart it rather than taking the whole service down.
  if [ -n "$SUBPAGE_PID" ] && ! kill -0 "$SUBPAGE_PID" 2>/dev/null; then
    log "subscription page exited; restarting it"
    SUBPAGE_PORT="$SUBPAGE_PORT" SUB_PORT="$SUB_PORT" \
    SUB_REAL_PATH="$SUB_REAL_PATH" \
    SUB_JSON_REAL_PATH="$SUB_JSON_REAL_PATH" \
    SUB_CLASH_REAL_PATH="$SUB_CLASH_REAL_PATH" \
    SUB_PATH="$SUB_PATH" SUB_JSON_PATH="$SUB_JSON_PATH" \
    SUB_CLASH_PATH="$SUB_CLASH_PATH" \
    CLIENT_SUBID="$CLIENT_SUBID" DB_FILE="$DB_FILE" \
    INBOUND_PORT="$INBOUND_PORT" BRAND="$BRAND" \
    TUNNEL_PATH="$TUNNEL_PATH" \
      python3 /opt/subserver.py &
    SUBPAGE_PID=$!
  fi
  sleep 5
done
