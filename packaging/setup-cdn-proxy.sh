#!/usr/bin/env bash
# Interactive CDN origin setup; deployment-specific values stay on the server.
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROXY_DIR=/etc/ikev2-web-proxy
WEB_CONFIG=/etc/ikev2-web/config.json

usage() {
  printf 'Usage: sudo bash %s\n' "$0"
  printf 'Interactively configure the installed Web UI for an HTTP CDN origin.\n'
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  '') ;;
  *) usage >&2; exit 2 ;;
esac
[[ $# -eq 0 ]] || { usage >&2; exit 2; }
[[ ${EUID} -eq 0 ]] || die "Run with sudo or as root."
command -v python3 >/dev/null || die "Python 3 is required."
[[ -f "$WEB_CONFIG" ]] || die "Install the Web UI first: sudo bash ikev2-strongswan-ubuntu.sh webui"
[[ -f "$SCRIPT_DIR/render-cdn-proxy.py" && -f "$SCRIPT_DIR/install-cdn-proxy.sh" && -f "$SCRIPT_DIR/cdn-networks.py" ]] ||
  die "The CDN helper files are missing. Update the Web UI or use a full checkout."
curl --fail --silent --max-time 3 http://127.0.0.1:8787/api/health >/dev/null ||
  die "The local Web UI is unavailable. Start ikev2-web.service and check its health first."

# Refuse unexpected ownership/permissions before reading or replacing existing settings.
if [[ -e "$PROXY_DIR" || -L "$PROXY_DIR" ]]; then
  [[ -d "$PROXY_DIR" && ! -L "$PROXY_DIR" && $(stat -c '%u:%a' "$PROXY_DIR") == '0:755' ]] ||
    die "The proxy directory must be a root-owned directory with mode 0755."
fi
for name in origin.json cdn-ips.txt Caddyfile; do
  if [[ -e "$PROXY_DIR/$name" || -L "$PROXY_DIR/$name" ]]; then
    [[ -f "$PROXY_DIR/$name" && ! -L "$PROXY_DIR/$name" && $(stat -c '%u:%a' "$PROXY_DIR/$name") == '0:644' ]] ||
      die "Existing proxy files must be root-owned regular files with mode 0644."
  fi
done

stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
hostname_default=''
port_default=8080
if [[ -f "$PROXY_DIR/origin.json" ]]; then
  if python3 - "$PROXY_DIR/origin.json" > "$stage/defaults" <<'PYTHON'
import json
import sys
try:
    settings = json.load(open(sys.argv[1]))
    hostname = settings["hostname"]
    port = settings["port"]
    if not isinstance(hostname, str) or any(c in hostname for c in "\r\n"):
        raise ValueError()
    if type(port) is not int:
        raise ValueError()
    print(hostname)
    print(port)
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit(1)
PYTHON
  then
    mapfile -t defaults < "$stage/defaults"
    hostname_default=${defaults[0]}
    port_default=${defaults[1]}
  else
    printf 'Existing origin settings are invalid; enter replacement values.\n' >&2
  fi
fi

printf '\nConfigure Web UI for a CDN\n'
printf 'The CDN must support HTTP origins. The CDN-to-server leg is unencrypted.\n'
printf 'This installs/restarts the root-managed proxy and enables Secure panel cookies.\n'
printf 'VPN services, DNS and firewall rules are not changed.\n\n'
read -r -p "Panel hostname${hostname_default:+ [$hostname_default]}: " hostname || exit 1
hostname=${hostname:-$hostname_default}
read -r -p "HTTP origin port [$port_default]: " port || exit 1
port=${port:-$port_default}

python3 - "$hostname" "$port" "$stage/origin.json" <<'PYTHON'
import json
from pathlib import Path
import sys
try:
    settings = {"hostname": sys.argv[1].lower(), "port": int(sys.argv[2])}
except ValueError:
    raise SystemExit("Origin port must be an integer")
Path(sys.argv[3]).write_text(json.dumps(settings) + "\n")
PYTHON

printf '\nUse current origin-facing CIDRs from your CDN provider, one per line.\n'
printf '  1) Read a local network-list file\n'
printf '  2) Paste network ranges\n'
printf '  4) Automatic: ArvanCloud official feed\n'
printf '  5) Automatic: Cloudflare official API (IPv4 + IPv6)\n'
printf '  6) Automatic: another CDN public HTTPS text feed\n'
network_default=''
if [[ -f "$PROXY_DIR/cdn-ips.txt" ]]; then
  printf '  3) Keep the existing network list\n'
  network_default=3
fi
read -r -p "Network input${network_default:+ [$network_default]}: " network_choice || exit 1
network_choice=${network_choice:-$network_default}
case "$network_choice" in
  1)
    read -r -p 'Path to the CDN CIDR file: ' network_path || exit 1
    [[ -f "$network_path" && -r "$network_path" ]] || die "The network-list file is not readable."
    cat -- "$network_path" > "$stage/cdn-ips.txt"
    ;;
  2)
    printf 'Paste CIDRs, one per line. Finish with an empty line:\n'
    while IFS= read -r network && [[ -n "$network" ]]; do
      printf '%s\n' "$network"
    done > "$stage/cdn-ips.txt"
    ;;
  3)
    [[ -f "$PROXY_DIR/cdn-ips.txt" ]] || die "There is no existing network list."
    cp -- "$PROXY_DIR/cdn-ips.txt" "$stage/cdn-ips.txt"
    if [[ -f "$PROXY_DIR/origin.json" ]]; then
      python3 -B "$SCRIPT_DIR/cdn-networks.py" keep-source "$PROXY_DIR/origin.json" "$stage/origin.json"
    fi
    ;;
  4|5|6)
    case "$network_choice" in
      4) provider=arvancloud ;;
      5) provider=cloudflare ;;
      6) provider=custom ;;
    esac
    source_args=(--provider "$provider" --settings "$stage/origin.json" --output "$stage/cdn-ips.txt")
    if [[ "$provider" == custom ]]; then
      read -r -p 'Public HTTPS CIDR feed URL (no credentials/query): ' feed_url || exit 1
      source_args+=(--url "$feed_url")
    fi
    python3 -B "$SCRIPT_DIR/cdn-networks.py" fetch "${source_args[@]}"
    ;;
  *) die "Choose a CDN or a manual network input option." ;;
esac

# Validate everything before confirmation or writing system configuration.
python3 -B "$SCRIPT_DIR/render-cdn-proxy.py" "$stage/origin.json" "$stage/cdn-ips.txt" > "$stage/Caddyfile"
python3 - "$stage/origin.json" > "$stage/validated" <<'PYTHON'
import json
import sys
settings = json.load(open(sys.argv[1]))
print(settings["hostname"])
print(settings["port"])
PYTHON
mapfile -t validated < "$stage/validated"
hostname=${validated[0]}
port=${validated[1]}
if ! systemctl is-active --quiet ikev2-web-proxy.service && [[ -n $(ss -H -lnt "sport = :${port}") ]]; then
  die "The selected origin port is occupied by another service."
fi
printf '\nConfiguration summary\n'
printf '  Public panel URL : https://%s\n' "$hostname"
printf '  Origin transport : HTTP, port %s (unencrypted)\n' "$port"
source_mode=$(python3 -B "$SCRIPT_DIR/cdn-networks.py" source "$stage/origin.json")
printf '  Allowed peers    : validated CDN networks and loopback\n'
printf '  Network source   : %s\n' "$source_mode"
if [[ "$source_mode" != manual ]]; then
  printf '  Automatic refresh: daily; keep previous valid list on failure\n'
fi
printf '  Changes          : proxy installation/restart, panel restart, Secure cookies\n'
read -r -p 'Apply these settings and install/restart the CDN proxy? [y/N]: ' answer || answer=''
case "$answer" in
  y|Y|yes|YES|Yes) ;;
  *) printf 'CDN setup canceled; existing settings were not changed.\n'; exit 0 ;;
esac

install -d -o root -g root -m 0755 "$PROXY_DIR"
# Share a lock with the periodic worker while committing the selected source/list.
[[ ! -L "$PROXY_DIR/.networks.lock" ]] || die "Unsafe CDN configuration lock."
umask 077
exec {cdn_lock_fd}>>"$PROXY_DIR/.networks.lock"
chmod 600 "$PROXY_DIR/.networks.lock"
[[ $(stat -c '%u:%a' "$PROXY_DIR/.networks.lock") == '0:600' ]] || die "Unsafe CDN configuration lock owner."
flock -x "$cdn_lock_fd"
backup_dir=''
if [[ -f "$PROXY_DIR/origin.json" || -f "$PROXY_DIR/cdn-ips.txt" || -f "$PROXY_DIR/Caddyfile" ]]; then
  [[ ! -L "$PROXY_DIR/backups" ]] || die "The backup directory must not be a symlink."
  install -d -o root -g root -m 0700 "$PROXY_DIR/backups"
  backup_dir=$(mktemp -d "$PROXY_DIR/backups/setup.XXXXXXXX")
  for name in origin.json cdn-ips.txt Caddyfile; do
    if [[ -f "$PROXY_DIR/$name" ]]; then
      cp -p -- "$PROXY_DIR/$name" "$backup_dir/$name"
    fi
  done
  printf 'Previous proxy settings saved in: %s\n' "$backup_dir"
fi
for name in origin.json cdn-ips.txt; do
  install -o root -g root -m 0644 "$stage/$name" "$PROXY_DIR/$name.new"
  mv -f -- "$PROXY_DIR/$name.new" "$PROXY_DIR/$name"
done
# The proxy installer takes the same lock and reads the committed configuration.
flock -u "$cdn_lock_fd"
if ! bash "$SCRIPT_DIR/install-cdn-proxy.sh"; then
  printf 'CDN proxy installation failed; new settings remain saved for retry. Service/files may be partially installed.\n' >&2
  [[ -z "$backup_dir" ]] || printf 'Previous settings are available in: %s\n' "$backup_dir" >&2
  printf 'Review the error and rerun the CDN setup.\n' >&2
  exit 1
fi
printf '\nServer prepared. Configure your CDN with this server as the HTTP origin on port %s.\n' "$port"
printf 'Use Host %s, browser HTTPS and X-Forwarded-Proto: https; disable caching.\n' "$hostname"
printf 'Allow the origin TCP port only from CDN ranges in provider/server firewalls. Keep 8787 private.\n'
printf 'Then open https://%s and verify login.\n' "$hostname"
