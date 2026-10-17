#!/usr/bin/env bash
# Called by the root-owned Web UI installer, after backend health succeeds.
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROXY_DIR=/etc/ikev2-web-proxy
PROXY_BINARY=/usr/local/lib/ikev2-web/caddy

[[ ${EUID} -eq 0 ]] || { printf 'Run via the root Web UI installer.\n' >&2; exit 1; }
[[ -f "$PROXY_DIR/origin.json" ]] || exit 0

# This directory is separate from the frontend-owned authentication directory.
[[ $(stat -c '%u:%a' "$PROXY_DIR") == '0:755' ]] || {
  printf 'Proxy configuration directory must be root-owned, mode 0755.\n' >&2; exit 1;
}
[[ $(stat -c '%u:%a' "$PROXY_DIR/origin.json") == '0:644' ]] || {
  printf 'Origin settings must be root-owned, mode 0644.\n' >&2; exit 1;
}

stage=$(mktemp -d /tmp/ikev2-proxy.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 10 --max-time 60 --retry 2 \
  https://www.arvancloud.ir/fa/ips.txt --output "$stage/arvan-ips.txt"
python3 -B "$SCRIPT_DIR/render-arvan-proxy.py" "$PROXY_DIR/origin.json" \
  "$stage/arvan-ips.txt" > "$stage/Caddyfile"
readarray -t origin < <(python3 -c 'import json; c=json.load(open("/etc/ikev2-web-proxy/origin.json")); print(c["hostname"]); print(c["port"])')
if ! systemctl is-active --quiet ikev2-web-proxy.service && \
    [[ -n $(ss -H -lnt "sport = :${origin[1]}") ]]; then
  printf 'Origin port %s is already occupied; refusing to replace another service.\n' "${origin[1]}" >&2
  exit 1
fi

# Extract only the binary from the distribution's authenticated APT package.
# Do not install its package service: that could start an unwanted listener on 80.
apt-get update -qq
chmod 0755 "$stage"
(
  cd "$stage"
  apt-get download caddy
)
packages=("$stage"/caddy_*.deb)
[[ ${#packages[@]} -eq 1 && -f ${packages[0]} ]]
dpkg-deb --extract "${packages[0]}" "$stage/package"
"$stage/package/usr/bin/caddy" validate --config "$stage/Caddyfile" --adapter caddyfile

if ! getent group ikev2-proxy >/dev/null; then
  groupadd --system ikev2-proxy
fi
if ! getent passwd ikev2-proxy >/dev/null; then
  useradd --system --gid ikev2-proxy --home-dir /nonexistent --shell /usr/sbin/nologin ikev2-proxy
fi

install -d -o root -g root -m 0755 /usr/local/lib/ikev2-web
install -o root -g root -m 0755 "$stage/package/usr/bin/caddy" "$PROXY_BINARY.new"
mv -f "$PROXY_BINARY.new" "$PROXY_BINARY"
install -o root -g root -m 0644 "$stage/Caddyfile" "$PROXY_DIR/Caddyfile.new"
mv -f "$PROXY_DIR/Caddyfile.new" "$PROXY_DIR/Caddyfile"
install -o root -g root -m 0644 "$stage/arvan-ips.txt" "$PROXY_DIR/arvan-ips.txt"
install -o root -g root -m 0644 "$SCRIPT_DIR/systemd/ikev2-web-proxy.service" \
  /etc/systemd/system/ikev2-web-proxy.service
runuser -u ikev2-proxy -- "$PROXY_BINARY" validate --config "$PROXY_DIR/Caddyfile" --adapter caddyfile
(
  cd /opt/ikev2
  python3 -B -m webui.manage configure-cookie --config /etc/ikev2-web/config.json
)
systemctl restart ikev2-web.service
systemctl daemon-reload
systemctl enable ikev2-web-proxy.service
systemctl restart ikev2-web-proxy.service

curl --fail --silent --show-error --retry 15 --retry-connrefused --retry-delay 1 --max-time 3 \
  --header "Host: ${origin[0]}" --header 'X-Forwarded-Proto: https' \
  "http://127.0.0.1:${origin[1]}/api/health" >/dev/null
systemctl is-active --quiet ikev2-web-proxy.service
printf 'Unprivileged Caddy origin ready on HTTP port %s for %s; only CDN peers and loopback accepted.\n' "${origin[1]}" "${origin[0]}"
printf 'No firewall, VPN, DNS, port 80, or port 443 settings were changed.\n'
