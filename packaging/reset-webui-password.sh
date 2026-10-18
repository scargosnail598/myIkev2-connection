#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

INSTALL_DIR="/opt/ikev2"
CONFIG_FILE="/etc/ikev2-web/config.json"
WEB_SERVICE="ikev2-web.service"

usage() {
  cat <<'HELP'
Usage: sudo /opt/ikev2/reset-webui-password.sh [--password-stdin]

Reset the installed Web UI password, disable 2FA and sign out panel sessions.
The current username, cookie settings and session lifetime are preserved.
Without --password-stdin, enter the new password twice without echo.
--password-stdin reads the exact password from stdin, without a trailing newline.
HELP
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

reset_webui_password() {
  local password_stdin="no"
  local load_state
  local -a password_args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --password-stdin) password_stdin="yes" ;;
      -h|--help) usage; return 0 ;;
      *) usage >&2; return 2 ;;
    esac
    shift
  done

  [[ ${EUID} -eq 0 ]] || die "Run this command with sudo or as root."
  [[ -f "$CONFIG_FILE" && ! -L "$CONFIG_FILE" ]] || die "No regular installed Web UI config was found. Install the Web UI first."
  [[ -f "${INSTALL_DIR}/webui/manage.py" ]] || die "Installed Web UI tools are missing. Update the Web UI first."
  command -v /usr/bin/python3 >/dev/null 2>&1 || die "/usr/bin/python3 is required."
  command -v systemctl >/dev/null 2>&1 || die "systemd is required."
  load_state=$(systemctl show --property=LoadState --value "$WEB_SERVICE")
  [[ "$load_state" == "loaded" ]] || die "The Web UI service is not installed. No password was changed."
  if [[ "$password_stdin" == "yes" ]]; then
    password_args+=(--password-stdin)
  else
    [[ -t 0 && -t 2 ]] || die "Interactive reset requires a terminal. Use --password-stdin for automation."
  fi

  printf 'Resetting the Web UI password also disables two-factor authentication.\n'

  # The existing utility hashes and atomically replaces the config while
  # retaining ownership and all settings except the password hash.
  (
    cd "$INSTALL_DIR"
    /usr/bin/python3 -B -m webui.manage reset-password --config "$CONFIG_FILE" "${password_args[@]}"
  )
  if ! systemctl restart "$WEB_SERVICE"; then
    die "The new password was saved, but the Web UI restart failed. Start ikev2-web.service to apply it and invalidate old sessions."
  fi
  if ! systemctl is-active --quiet "$WEB_SERVICE"; then
    die "The new password was saved, but the Web UI is not active. Check ikev2-web.service before signing in."
  fi
  printf 'Web UI password reset successfully. Existing panel sessions were signed out.\n'
  printf 'Two-factor authentication was disabled. Enable it again from Security after signing in.\n'
  printf 'The existing username, cookie settings and session lifetime were preserved.\n'
}

reset_webui_password "$@"
