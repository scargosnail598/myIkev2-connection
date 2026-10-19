#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)
INSTALL_DIR="/opt/ikev2-telegram"
CONFIG_DIR="/etc/ikev2-telegram"
BOT_USER="ikev2-telegram"
UNITS=(ikev2-telegram.service ikev2-telegram-collect.service ikev2-telegram-collect.timer)

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: sudo ./packaging/install-telegram-bot.sh install
       sudo ./packaging/install-telegram-bot.sh prepare
       sudo ./packaging/install-telegram-bot.sh configure
       ./packaging/install-telegram-bot.sh identify
       ./packaging/install-telegram-bot.sh status
       sudo ./packaging/install-telegram-bot.sh uninstall

install     Install/update the optional read-only bot; prompt on first install.
prepare     Install/update without prompts; configure and enable in the Web UI.
configure   Replace token/allowed numeric user IDs and restart an installed bot.
identify    Discover private sender IDs with a hidden token prompt. Stop any
            existing bot using that token first; verify IDs before allowing them.
status      Show the bot and collector timer state; never display the token.
uninstall   Remove bot code/services/runtime data; preserve private configuration.

Requires Ubuntu 22.04/24.04, Python 3 and an existing StrongSwan installation.
Use a dedicated Telegram bot without a webhook or another polling process.
EOF
}

require_root() {
  [[ ${EUID} -eq 0 ]] || die "Run this command with sudo or as root."
}

private_directory() {
  [[ ! -L "$CONFIG_DIR" ]] || die "Config directory must not be a symlink."
  install -d -o root -g root -m 0700 "$CONFIG_DIR"
}

configure_bot() {
  private_directory
  (
    cd "$SOURCE_DIR"
    /usr/bin/python3 -B -m telegram_bot.app configure --config "${CONFIG_DIR}/config.json"
  )
  chown root:root "${CONFIG_DIR}/config.json"
  chmod 0600 "${CONFIG_DIR}/config.json"
  # The service receives a private copy through systemd LoadCredential.
}

install_bot() {
  local prepare="${1:-no}" bot_running="no" timer_running="no"
  local path
  for path in telegram_bot/__init__.py telegram_bot/app.py telegram_bot/status.py \
    webui/__init__.py webui/metrics.py webui/sessions.py; do
    [[ -f "${SOURCE_DIR}/${path}" ]] || die "Required source is missing: ${path}"
  done
  for path in "${UNITS[@]}"; do
    [[ -f "${SCRIPT_DIR}/systemd/${path}" ]] || die "Missing systemd unit: ${path}"
  done
  [[ -x /usr/bin/python3 ]] || die "Python 3 is required."
  if [[ "$prepare" == "no" && ! -f "${CONFIG_DIR}/config.json" ]]; then
    configure_bot
  fi
  private_directory
  [[ ! -L "${CONFIG_DIR}/config.json" ]] || die "Config file must not be a symlink."
  if [[ -f "${CONFIG_DIR}/config.json" ]]; then
    chown root:root "${CONFIG_DIR}/config.json"
    chmod 0600 "${CONFIG_DIR}/config.json"
    (
      cd "$SOURCE_DIR"
      /usr/bin/python3 -B -c 'from telegram_bot.app import BotConfig, CONFIG; BotConfig.from_file(CONFIG)'
    )
  fi
  if ! getent group "$BOT_USER" >/dev/null; then
    groupadd --system "$BOT_USER"
  fi
  if systemctl is-active --quiet ikev2-telegram.service || systemctl is-enabled --quiet ikev2-telegram.service; then bot_running="yes"; fi
  if systemctl is-active --quiet ikev2-telegram-collect.timer || systemctl is-enabled --quiet ikev2-telegram-collect.timer; then timer_running="yes"; fi
  if ! getent passwd "$BOT_USER" >/dev/null; then
    useradd --system --gid "$BOT_USER" --home-dir /nonexistent --shell /usr/sbin/nologin "$BOT_USER"
  fi
  # Stop readers before replacing modules, without touching VPN or Web UI units.
  systemctl stop ikev2-telegram.service ikev2-telegram-collect.timer ikev2-telegram-collect.service 2>/dev/null || true
  install -d -o root -g root -m 0755 "$INSTALL_DIR" "${INSTALL_DIR}/telegram_bot" "${INSTALL_DIR}/webui"
  install -o root -g root -m 0644 "${SOURCE_DIR}/telegram_bot/"*.py "${INSTALL_DIR}/telegram_bot/"
  install -o root -g root -m 0644 "${SOURCE_DIR}/webui/__init__.py" \
    "${SOURCE_DIR}/webui/metrics.py" "${SOURCE_DIR}/webui/sessions.py" "${INSTALL_DIR}/webui/"
  for path in "${UNITS[@]}"; do
    install -o root -g root -m 0644 "${SCRIPT_DIR}/systemd/${path}" "/etc/systemd/system/${path}"
  done
  systemctl daemon-reload
  if [[ "$prepare" == "yes" ]]; then
    if [[ "$timer_running" == "yes" ]]; then systemctl start ikev2-telegram-collect.timer; fi
    if [[ "$bot_running" == "yes" && -f "${CONFIG_DIR}/config.json" ]]; then systemctl start ikev2-telegram.service; fi
    printf 'Telegram services prepared. Configure the bot in the Web UI Telegram page.\n'
    return 0
  fi
  systemctl start ikev2-telegram-collect.service
  runuser -u "$BOT_USER" -- /usr/bin/python3 -B -c '
import sys
sys.path.insert(0, "/opt/ikev2-telegram")
from telegram_bot.status import read_snapshot
if read_snapshot() is None:
    raise SystemExit("Collector snapshot is missing, unreadable or stale.")
'
  systemctl enable --now ikev2-telegram-collect.timer ikev2-telegram.service
  printf 'Bot installed. Send /status in a private Telegram chat from an allowed account.\n'
  show_status
}

show_status() {
  local failed=0 unit
  for unit in ikev2-telegram.service ikev2-telegram-collect.timer; do
    if systemctl is-active --quiet "$unit"; then
      printf '%-40s active\n' "$unit"
    else
      printf '%-40s inactive/failed\n' "$unit"
      failed=1
    fi
  done
  return "$failed"
}

uninstall_bot() {
  systemctl disable --now ikev2-telegram.service ikev2-telegram-collect.timer 2>/dev/null || true
  systemctl stop ikev2-telegram-collect.service 2>/dev/null || true
  local unit
  for unit in "${UNITS[@]}"; do
    rm -f -- "/etc/systemd/system/${unit}"
  done
  systemctl daemon-reload
  rm -rf -- "$INSTALL_DIR" /run/ikev2-telegram /var/lib/ikev2-telegram
  printf 'Bot removed. Private configuration preserved in %s; service account retained.\n' "$CONFIG_DIR"
}

[[ $# -le 1 ]] || die "Expected one command."
case "${1:-help}" in
  install) require_root; install_bot ;;
  prepare) require_root; install_bot yes ;;
  configure)
    require_root
    configure_bot
    if systemctl cat ikev2-telegram.service >/dev/null 2>&1; then
      systemctl restart ikev2-telegram.service
    fi
    ;;
  identify) cd "$SOURCE_DIR"; /usr/bin/python3 -B -m telegram_bot.app identify ;;
  status) show_status ;;
  uninstall) require_root; uninstall_bot ;;
  help|--help|-h) usage ;;
  *) usage >&2; exit 1 ;;
esac
