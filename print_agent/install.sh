#!/usr/bin/env bash
#
# PrintFlow kiosk installer.
#
# Turns a fresh Raspberry Pi into a working kiosk: installs what is needed,
# finds the printer, enrolls with the server for a key of its own, and starts
# printing. The only things it needs from you are the server address and a
# one-time enrollment code from Admin > Kiosks > Add kiosk.
#
#   curl -sSL https://your-server/install.sh | sudo bash
#
# Non-interactive:
#   sudo ./install.sh --server https://your-server --code K7P-3RD-92X
#
set -euo pipefail

SERVER_URL="${SERVER_URL:-__SERVER_URL__}"
ENROLL_CODE="${ENROLL_CODE:-}"
RUN_USER="${RUN_USER:-}"
FORCE_PRINTER="${DEFAULT_PRINTER:-}"
WITH_DISPLAY=1
KIOSK_PORT="${KIOSK_PORT:-5000}"

INSTALL_DIR=/opt/printflow
ENV_FILE=/etc/printflow-agent.env
AGENT_VERSION_MIN_PY=3.9

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[!]\033[0m  %s\n' "$*"; }
die()  { printf '  \033[1;31m[x]\033[0m  %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
PrintFlow kiosk installer

  --server URL     PrintFlow server, e.g. https://printflow.onrender.com
  --code CODE      Enrollment code from Admin > Kiosks > Add kiosk
  --user NAME      Account the services run as (default: the invoking user)
  --printer NAME   CUPS printer to use (default: auto-detect)
  --no-display     Print only; do not install the kiosk screen
  --help           This message

Run again at any time to re-enroll or upgrade — it is safe to repeat.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server)  SERVER_URL="${2:-}"; shift 2 ;;
        --code)    ENROLL_CODE="${2:-}"; shift 2 ;;
        --user)    RUN_USER="${2:-}"; shift 2 ;;
        --printer) FORCE_PRINTER="${2:-}"; shift 2 ;;
        --no-display) WITH_DISPLAY=0; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "Run this with sudo."

# --- who will own the services ------------------------------------------------
if [ -z "$RUN_USER" ]; then
    RUN_USER="${SUDO_USER:-}"
fi
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    # First ordinary account on the box — on a stock Pi image there is one.
    RUN_USER=$(awk -F: '$3 >= 1000 && $3 < 65534 && $1 != "nobody" { print $1; exit }' /etc/passwd)
fi
[ -n "$RUN_USER" ] || die "Could not work out which user to run as — pass --user NAME."
id "$RUN_USER" >/dev/null 2>&1 || die "No such user: $RUN_USER"
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)

say "Installing PrintFlow kiosk as user '$RUN_USER'"

# --- packages -----------------------------------------------------------------
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PACKAGES="python3 python3-requests python3-cups python3-qrcode python3-pil cups curl ca-certificates"
if [ "$WITH_DISPLAY" -eq 1 ]; then
    if apt-cache show chromium >/dev/null 2>&1; then
        PACKAGES="$PACKAGES chromium"
    elif apt-cache show chromium-browser >/dev/null 2>&1; then
        PACKAGES="$PACKAGES chromium-browser"
    else
        warn "No chromium package found — the screen will not start."
        WITH_DISPLAY=0
    fi
fi
# shellcheck disable=SC2086
apt-get install -y -qq $PACKAGES >/dev/null
ok "packages installed"

python3 - <<EOF || die "Python $AGENT_VERSION_MIN_PY or newer is required."
import sys
sys.exit(0 if sys.version_info >= tuple(int(p) for p in "$AGENT_VERSION_MIN_PY".split('.')) else 1)
EOF

# --- application files --------------------------------------------------------
say "Fetching the agent"
mkdir -p "$INSTALL_DIR"
TMP_TAR=$(mktemp /tmp/printflow-agent.XXXXXX.tar.gz)
trap 'rm -f "$TMP_TAR"' EXIT

if [ -f "$(dirname "$0")/print_agent.py" ]; then
    # Running from a checkout rather than curl — use what is next to us.
    cp -r "$(dirname "$0")/." "$INSTALL_DIR/"
    ok "copied from $(dirname "$0")"
else
    curl -fsSL "$SERVER_URL/install/agent.tar.gz" -o "$TMP_TAR" \
        || die "Could not download the agent from $SERVER_URL"
    tar xzf "$TMP_TAR" -C "$INSTALL_DIR"
    ok "downloaded from $SERVER_URL"
fi
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true
chown -R root:root "$INSTALL_DIR"

# --- printer ------------------------------------------------------------------
say "Looking for a printer"
systemctl enable --now cups >/dev/null 2>&1 || true
sleep 1

PRINTER="$FORCE_PRINTER"
if [ -z "$PRINTER" ]; then
    PRINTER=$(lpstat -d 2>/dev/null | sed -n 's/.*: *//p' | head -1 || true)
fi
if [ -z "$PRINTER" ]; then
    PRINTER=$(lpstat -p 2>/dev/null | awk '/^printer/ { print $2 }' | head -1 || true)
    if [ -n "$PRINTER" ]; then
        lpadmin -d "$PRINTER" >/dev/null 2>&1 || true
        ok "set '$PRINTER' as the default printer"
    fi
fi
if [ -n "$PRINTER" ]; then
    ok "printer: $PRINTER"
else
    warn "no printer found — plug it in, then run: sudo lpadmin -d <name>"
    warn "the agent will pick up the CUPS default whenever one appears"
fi

# --- details about this device ------------------------------------------------
MAC=$(for iface in /sys/class/net/*; do
        name=$(basename "$iface")
        case "$name" in lo|docker*|veth*|br-*|virbr*) continue ;; esac
        [ -r "$iface/address" ] || continue
        addr=$(cat "$iface/address")
        [ "$addr" = "00:00:00:00:00:00" ] && continue
        echo "$addr"; break
      done | head -1 | tr '[:lower:]' '[:upper:]')
HOSTNAME_=$(hostname)
IP=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1 || true)
PLATFORM="$(uname -s) $(uname -r) ($(uname -m))"

# --- enrollment ---------------------------------------------------------------
say "Enrolling with the server"

if [ -z "$SERVER_URL" ] || [ "$SERVER_URL" = "__SERVER_URL__" ]; then
    read -r -p "  Server URL (e.g. https://printflow.onrender.com): " SERVER_URL </dev/tty
fi
SERVER_URL="${SERVER_URL%/}"
[ -n "$SERVER_URL" ] || die "A server URL is required."

case "$SERVER_URL" in
    https://*) HTTPS_REQUIRED=true ;;
    *) HTTPS_REQUIRED=false
       warn "$SERVER_URL is not https — the enrollment code and key will cross"
       warn "the network in the clear. Only acceptable on a trusted LAN." ;;
esac

if [ -z "$ENROLL_CODE" ]; then
    echo "  Get a code from the server: Admin > Kiosks > Add kiosk"
    read -r -p "  Enrollment code (XXX-XXX-XXX): " ENROLL_CODE </dev/tty
fi
[ -n "$ENROLL_CODE" ] || die "An enrollment code is required."

REQUEST=$(python3 - <<EOF
import json
print(json.dumps({
    "code": "$ENROLL_CODE",
    "mac_address": "$MAC",
    "hostname": "$HOSTNAME_",
    "ip_address": "$IP",
    "platform": "$PLATFORM",
}))
EOF
)

RESPONSE=$(curl -sS -X POST "$SERVER_URL/api/agent/enroll" \
                -H 'Content-Type: application/json' \
                -d "$REQUEST" 2>&1) || die "Could not reach $SERVER_URL"

eval "$(python3 - "$RESPONSE" <<'EOF'
import json, shlex, sys
try:
    data = json.loads(sys.argv[1])
except ValueError:
    print("ENROLL_ERROR=" + shlex.quote("server did not return JSON: " + sys.argv[1][:200]))
    sys.exit(0)
if not data.get('ok'):
    detail = data.get('detail')
    msg = data.get('error', 'enrollment refused')
    print("ENROLL_ERROR=" + shlex.quote(msg + ((' ' + detail) if detail else '')))
    sys.exit(0)
for name, key in (('AGENT_KEY', 'agent_key'), ('PRINTER_ID', 'printer_id'),
                  ('SITE_URL', 'site_url'), ('KIOSK_NAME', 'kiosk_name')):
    print(f"{name}=" + shlex.quote(str(data.get(key, ''))))
EOF
)"

if [ -n "${ENROLL_ERROR:-}" ]; then
    die "Enrollment failed: $ENROLL_ERROR"
fi
[ -n "${AGENT_KEY:-}" ] || die "Server did not return a key."
ok "enrolled as '${KIOSK_NAME:-$PRINTER_ID}'"

# --- configuration ------------------------------------------------------------
say "Writing configuration"
umask 077
cat > "$ENV_FILE" <<EOF
# PrintFlow kiosk — written by install.sh. Contains this device's key.
SERVER_URL=$SERVER_URL
SITE_URL=${SITE_URL:-$SERVER_URL}
AGENT_KEY=$AGENT_KEY
AGENT_ID=$PRINTER_ID
PRINTER_ID=$PRINTER_ID
DEFAULT_PRINTER=$PRINTER
KIOSK_PORT=$KIOSK_PORT
HTTPS_REQUIRED=$HTTPS_REQUIRED
EOF
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "key saved to $ENV_FILE (0600, root only)"

# --- services -----------------------------------------------------------------
say "Installing services"

cat > /etc/systemd/system/printflow-agent.service <<EOF
[Unit]
Description=PrintFlow Print Agent
After=network-online.target cups.service
Wants=network-online.target cups.service

[Service]
Type=simple
User=$RUN_USER
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $INSTALL_DIR/print_agent.py
Restart=always
RestartSec=5
# The agent only ever reads its own files and talks to CUPS.
NoNewPrivileges=true
PrivateTmp=false
ProtectSystem=full
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now printflow-agent >/dev/null
ok "print agent running"

if [ "$WITH_DISPLAY" -eq 1 ]; then
    cat > /etc/systemd/system/printflow-kiosk.service <<EOF
[Unit]
Description=PrintFlow Kiosk Display Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $INSTALL_DIR/kiosk_server.py
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now printflow-kiosk >/dev/null
    ok "display server running on port $KIOSK_PORT"

    # The browser has to live in the desktop session, not in a system service.
    mkdir -p /etc/xdg/autostart
    cat > /etc/xdg/autostart/printflow-kiosk.desktop <<EOF
[Desktop Entry]
Type=Application
Name=PrintFlow Kiosk
Exec=$INSTALL_DIR/kiosk-browser.sh
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
    ok "screen will open on the next desktop login"
fi

# --- check it actually works --------------------------------------------------
say "Checking"
sleep 4

if systemctl is-active --quiet printflow-agent; then
    ok "agent service is up"
else
    warn "agent is not running — journalctl -u printflow-agent -n 40"
fi

if curl -fsS -o /dev/null --max-time 5 \
        -H "X-Agent-Key: $AGENT_KEY" "$SERVER_URL/api/agent/pending-jobs"; then
    ok "server accepted this kiosk's key"
else
    warn "could not reach the server with the new key — check the network"
fi

if [ "$WITH_DISPLAY" -eq 1 ]; then
    if curl -fsS -o /dev/null --max-time 5 "http://localhost:$KIOSK_PORT/kiosk"; then
        ok "display page is being served"
    else
        warn "display page not responding — journalctl -u printflow-kiosk -n 40"
    fi
fi

cat <<EOF

  Done. This kiosk is '${KIOSK_NAME:-$PRINTER_ID}'.

  It appears at $SERVER_URL/admin/kiosks
  Logs:    journalctl -u printflow-agent -f
  Restart: sudo systemctl restart printflow-agent printflow-kiosk
$([ "$WITH_DISPLAY" -eq 1 ] && echo "
  Reboot to bring the screen up, or log in to the desktop.")
EOF
