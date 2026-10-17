#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)
INSTALL_DIR="/opt/ikev2"
CONFIG_DIR="/etc/ikev2-web"
CONFIG_FILE="${CONFIG_DIR}/config.json"
SYSTEMD_DIR="/etc/systemd/system"
WEB_USER="ikev2-web"
BROKER_SERVICE="ikev2-web-broker.service"
WEB_SERVICE="ikev2-web.service"

usage() {
  cat <<'EOF'
Usage: sudo ./packaging/install-webui.sh install [--secure-cookie] [--password-stdin]
       ./packaging/install-webui.sh status

Commands:
  install          Install/update the Web UI and start its systemd services.
  status           Show Web UI, broker, socket, and HTTP health status.

Options:
  --secure-cookie  Require HTTPS for the login cookie. Without this option,
                   loopback HTTP/SSH-tunnel mode is configured.
  --password-stdin Read the initial admin password from stdin without prompting.
                   Existing authentication configuration is always preserved.
EOF
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_root() {
  [[ ${EUID} -eq 0 ]] || die "Run install with sudo or as root."
}

require_source_files() {
  local path
  for path in \
    "webui/__init__.py" \
    "webui/app.py" \
    "webui/broker.py" \
    "webui/manage.py" \
    "webui/security.py" \
    "webui/users.py" \
    "webui/profiles.py" \
    "webui/limits.py" \
    "webui/sessions.py" \
    "webui/diagnostics.py" \
    "webui/metrics.py" \
    "packaging/systemd/ikev2-web-users-apply.service" \
    "packaging/systemd/ikev2-web-limits.service" \
    "packaging/systemd/ikev2-web-limits.timer" \
    "webui/static/index.html" \
    "webui/static/app.css" \
    "webui/static/app.js" \
    "webui/static/qrcodegen.js" \
    "packaging/systemd/${BROKER_SERVICE}" \
    "packaging/systemd/${WEB_SERVICE}" \
    "packaging/systemd/ikev2-web-proxy.service" \
    "packaging/install-arvan-proxy.sh" \
    "packaging/render-arvan-proxy.py"; do
    [[ -f "${SOURCE_DIR}/${path}" ]] || die "Required source file is missing: ${path}"
  done
}

install_source() {
  install -d -o root -g root -m 0755 \
    "$INSTALL_DIR" \
    "${INSTALL_DIR}/webui" \
    "${INSTALL_DIR}/webui/static"

  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/webui/__init__.py" \
    "${SOURCE_DIR}/webui/app.py" \
    "${SOURCE_DIR}/webui/broker.py" \
    "${SOURCE_DIR}/webui/manage.py" \
    "${SOURCE_DIR}/webui/security.py" \
    "${SOURCE_DIR}/webui/users.py" \
    "${SOURCE_DIR}/webui/profiles.py" \
    "${SOURCE_DIR}/webui/limits.py" \
    "${SOURCE_DIR}/webui/sessions.py" \
    "${SOURCE_DIR}/webui/diagnostics.py" \
    "${SOURCE_DIR}/webui/metrics.py" \
    "${INSTALL_DIR}/webui/"
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/webui/static/index.html" \
    "${SOURCE_DIR}/webui/static/app.css" \
    "${SOURCE_DIR}/webui/static/app.js" \
    "${SOURCE_DIR}/webui/static/qrcodegen.js" \
    "${INSTALL_DIR}/webui/static/"
}

ensure_service_account() {
  if ! getent group "$WEB_USER" >/dev/null; then
    groupadd --system "$WEB_USER"
  fi
  if ! getent passwd "$WEB_USER" >/dev/null; then
    useradd --system \
      --gid "$WEB_USER" \
      --home-dir /nonexistent \
      --shell /usr/sbin/nologin \
      "$WEB_USER"
  fi
}

ensure_config() {
  local secure_cookie="$1"
  local password_stdin="$2"
  install -d -o "$WEB_USER" -g "$WEB_USER" -m 0700 "$CONFIG_DIR"
  if [[ -f "$CONFIG_FILE" ]]; then
    chown "$WEB_USER:$WEB_USER" "$CONFIG_FILE"
    chmod 0600 "$CONFIG_FILE"
    printf 'Preserved existing authentication config: %s\n' "$CONFIG_FILE"
    return 0
  fi

  local -a config_args=(
    /usr/bin/python3 -m webui.manage create-config
    --output "$CONFIG_FILE"
    --username admin
  )
  if [[ "$secure_cookie" != "yes" ]]; then
    config_args+=(--allow-http)
  fi
  if [[ "$password_stdin" == "yes" ]]; then
    config_args+=(--password-stdin)
  fi
  (
    cd "$INSTALL_DIR"
    "${config_args[@]}"
  )
  chown "$WEB_USER:$WEB_USER" "$CONFIG_FILE"
  chmod 0600 "$CONFIG_FILE"
}

install_units() {
  install -d -o root -g root -m 0700 /var/lib/ikev2-web
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/packaging/systemd/ikev2-web-limits.service" \
    "${SOURCE_DIR}/packaging/systemd/ikev2-web-limits.timer" \
    "${SYSTEMD_DIR}/"
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/packaging/systemd/ikev2-web-users-apply.service" \
    "${SYSTEMD_DIR}/ikev2-web-users-apply.service"
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/packaging/systemd/${BROKER_SERVICE}" \
    "${SYSTEMD_DIR}/${BROKER_SERVICE}"
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/packaging/systemd/${WEB_SERVICE}" \
    "${SYSTEMD_DIR}/${WEB_SERVICE}"
  systemctl daemon-reload
  systemctl enable "$BROKER_SERVICE" "$WEB_SERVICE"
  systemctl restart "$BROKER_SERVICE"
  systemctl enable --now ikev2-web-limits.timer
  if ! systemctl restart "$WEB_SERVICE"; then
    if command -v ss >/dev/null 2>&1 && ss -H -lnt 'sport = :8787' | grep -q .; then
      printf 'Port 8787 is already in use. Stop the manually started Web UI and retry.\n' >&2
    fi
    return 1
  fi
}

show_status() {
  local failed="no"
  local service
  for service in "$BROKER_SERVICE" "$WEB_SERVICE"; do
    if systemctl is-active --quiet "$service"; then
      printf '%-32s active\n' "$service"
    else
      printf '%-32s inactive/failed\n' "$service"
      failed="yes"
    fi
  done

  if systemctl is-active --quiet ikev2-web-limits.timer; then
    printf '%-32s active\n' "VPN limits timer"
  else
    printf '%-32s inactive/failed\n' "VPN limits timer"
    failed="yes"
  fi

  if [[ -S /run/ikev2-web/broker.sock ]]; then
    printf '%-32s available\n' "broker socket"
  else
    printf '%-32s missing\n' "broker socket"
    failed="yes"
  fi

  if [[ ${EUID} -eq 0 ]]; then
    if runuser -u "$WEB_USER" -- /usr/bin/python3 -B -c '
import sys
sys.path.insert(0, "/opt/ikev2")
from webui.app import SystemStatusProvider
from webui.broker import BrokerClient
broker = BrokerClient()
status = SystemStatusProvider(broker=broker).dashboard()
report = broker.diagnostics()
checks = report.get("checks", [])
expected = {"installation", "vpn_service", "firewall_service", "proxy_service", "connection", "udp_ports", "ipv4_forwarding", "server_certificate"}
valid = (report.get("ok") is True and isinstance(checks, list) and len(checks) == 8
         and all(isinstance(item, dict) for item in checks)
         and {item.get("id") for item in checks} == expected
         and all(item.get("status") in {"pass", "warn", "fail", "unknown"} for item in checks))
if valid:
    print("Read-only diagnostics: " + ", ".join("{}={}".format(state, sum(item["status"] == state for item in checks))
                                            for state in ("pass", "warn", "fail", "unknown")))
metrics = broker.metrics()
metrics_valid = (metrics.get("ok") is True and isinstance(metrics.get("cpu"), dict)
                 and isinstance(metrics.get("memory"), dict) and isinstance(metrics.get("network"), dict)
                 and isinstance(metrics.get("history"), list) and 1 <= len(metrics["history"]) <= 61)
if metrics_valid:
    print("Read-only metrics: cpu={}, memory={}, network={}".format(
        metrics["cpu"].get("cores") is not None, metrics["memory"].get("total_bytes") is not None,
        metrics["network"].get("rx_bytes") is not None))
limits_valid = True
if status["installed"]:
    catalog = broker.list_users()
    limits_valid = (status["capabilities"].get("user_limits") is True and catalog.get("ok") is True
                    and isinstance(catalog.get("limits_revision"), str) and len(catalog["limits_revision"]) == 32
                    and isinstance(catalog.get("limits_sampled_at"), (int, float)))
    print("VPN quota accounting: sample={}, healthy={}".format(limits_valid, catalog.get("limits_healthy") is True))
sys.exit(0 if status["capabilities"]["service_actions"] and isinstance(status["installed"], bool) and valid and metrics_valid and limits_valid else 1)
'; then
      printf '%-32s verified\n' "unprivileged dashboard/broker"
    else
      printf '%-32s unavailable\n' "unprivileged dashboard/broker"
      failed="yes"
    fi
  fi

  if command -v curl >/dev/null 2>&1 && \
      curl --fail --silent --show-error --max-time 3 \
        http://127.0.0.1:8787/api/health >/dev/null; then
    printf '%-32s healthy\n' "HTTP API"
  else
    printf '%-32s unavailable\n' "HTTP API"
    failed="yes"
  fi

  [[ "$failed" == "no" ]]
}

verify_metrics_collection() {
  runuser -u "$WEB_USER" -- /usr/bin/python3 -B -c '
import sys
import time
sys.path.insert(0, "/opt/ikev2")
from webui.broker import BrokerClient
broker = BrokerClient()
first = broker.metrics()
if first.get("ok") is not True:
    raise SystemExit("Metrics collector is unavailable.")
# No metrics requests from this probe while the server-side collector works.
time.sleep(11)
latest = broker.metrics()
recent = [point for point in latest.get("history", []) if point.get("age_seconds", 300) < 10.5]
if latest.get("ok") is not True or len(recent) < 2:
    raise SystemExit("Metrics did not accumulate independently of requests.")
print("Continuous metrics buffer verified: at least two new samples without requests from the deployment probe.")
'
}

install_webui() {
  local secure_cookie="no"
  local password_stdin="no"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --secure-cookie) secure_cookie="yes" ;;
      --password-stdin) password_stdin="yes" ;;
      *) usage; exit 2 ;;
    esac
    shift
  done

  require_root
  require_source_files
  command -v /usr/bin/python3 >/dev/null 2>&1 || die "/usr/bin/python3 is required."
  command -v systemctl >/dev/null 2>&1 || die "systemd is required."

  ensure_service_account
  install_source
  ensure_config "$secure_cookie" "$password_stdin"
  install_units
  if [[ -f /var/lib/ikev2-easy-installer/state.env && -f /etc/ipsec.secrets ]]; then
    (
      cd "$INSTALL_DIR"
      /usr/bin/python3 -B -c 'from webui.users import UserManager; result=UserManager().check(); assert result.get("checked") is True, "Account helper read-only check failed"; print("Account helper namespace and private storage verified (no credential changes).")'
    )
  fi

  systemctl start ikev2-web-limits.service

  # Type=simple units may be active before the HTTP listener is ready.
  local attempt
  for ((attempt = 0; attempt < 20; attempt++)); do
    if curl --fail --silent --max-time 1 http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done

  if ! show_status; then
    systemctl --no-pager --full status "$BROKER_SERVICE" "$WEB_SERVICE" || true
    die "Web UI installation completed, but health verification failed."
  fi

  verify_metrics_collection

  if [[ -f /etc/ikev2-web-proxy/origin.json ]]; then
    bash "${SCRIPT_DIR}/install-arvan-proxy.sh"
  fi

  printf '\nWeb UI installed successfully.\n'
  if [[ -f /etc/ikev2-web-proxy/origin.json ]]; then
    /usr/bin/python3 -c 'import json; c=json.load(open("/etc/ikev2-web-proxy/origin.json")); print("Open https://" + c["hostname"] + " through your CDN.")'
    return 0
  fi
  printf 'Open it locally at http://127.0.0.1:8787\n'
  printf 'For a remote server, use an SSH tunnel:\n'
  printf '  ssh -L 8787:127.0.0.1:8787 user@server\n'
}

case "${1:-}" in
  install)
    shift
    install_webui "$@"
    ;;
  status)
    [[ $# -eq 1 ]] || { usage; exit 2; }
    show_status
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
