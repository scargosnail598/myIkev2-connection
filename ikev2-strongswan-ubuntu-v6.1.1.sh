#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

# IKEv2 VPN installer for Ubuntu 22.04 and 24.04.
# Uses StrongSwan, EAP-MSCHAPv2, a private CA, and IPv4 full-tunnel NAT.

INSTALLER_NAME="ikev2-easy-installer"
CURRENT_INSTALLER_VERSION="6.1.1-en"
INSTALLER_VERSION="$CURRENT_INSTALLER_VERSION"

STATE_DIR="/var/lib/${INSTALLER_NAME}"
BACKUP_DIR="${STATE_DIR}/backups"
STATE_FILE="${STATE_DIR}/state.env"

IPSEC_CONF="/etc/ipsec.conf"
IPSEC_SECRETS="/etc/ipsec.secrets"
SYSCTL_FILE="/etc/sysctl.d/99-ikev2-vpn.conf"
FW_SCRIPT="/usr/local/sbin/ikev2-vpn-firewall"
FW_SERVICE_FILE="/etc/systemd/system/ikev2-vpn-firewall.service"
FW_SERVICE="ikev2-vpn-firewall.service"
STRONGSWAN_SERVICE="strongswan-starter.service"

# v6 private SOCKS5 Proxy Mode. The listener is bound only to a dedicated
# private address reachable through the IKEv2 tunnel, never to the public IP.
PROXY_PACKAGE="dante-server"
DANTE_DEFAULT_SERVICE="danted.service"
PROXY_CONFIG_DIR="/etc/ikev2-vpn-proxy"
PROXY_CONF="${PROXY_CONFIG_DIR}/danted.conf"
PROXY_FW_SCRIPT="/usr/local/sbin/ikev2-vpn-proxy-firewall"
PROXY_SERVICE_FILE="/etc/systemd/system/ikev2-vpn-proxy.service"
PROXY_SERVICE="ikev2-vpn-proxy.service"
PROXY_DEFAULT_IP="10.254.254.1"
PROXY_DEFAULT_PORT="1080"
PROXY_INFO_BEGIN="# BEGIN IKEV2 EASY INSTALLER PROXY MODE"
PROXY_INFO_END="# END IKEV2 EASY INSTALLER PROXY MODE"

CA_KEY="/etc/ipsec.d/private/ikev2-installer-ca-key.pem"
CA_CERT="/etc/ipsec.d/cacerts/ikev2-installer-ca-cert.pem"
SERVER_KEY="/etc/ipsec.d/private/ikev2-installer-server-key.pem"
SERVER_CERT="/etc/ipsec.d/certs/ikev2-installer-server-cert.pem"

REQUESTED_PACKAGES=(
  strongswan-starter
  strongswan-charon
  strongswan-libcharon
  strongswan-pki
  libstrongswan-standard-plugins
  libstrongswan-extra-plugins
  libcharon-extauth-plugins
  iptables
  openssl
  ca-certificates
)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log()  { printf '%b[+]%b %s\n' "$GREEN" "$RESET" "$*"; }
info() { printf '%b[i]%b %s\n' "$CYAN" "$RESET" "$*"; }
warn() { printf '%b[!]%b %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%b[x]%b %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

pause_main_menu() {
  printf '\n'
  read -r -p 'Press Enter to return to the main menu...' _ || true
}

on_error() {
  local line="$1"
  printf '\n%b[x]%b The installer stopped because of an error near line %s.\n' "$RED" "$RESET" "$line" >&2
  if [[ -f "$STATE_FILE" ]]; then
    printf 'A managed state file exists. After reviewing the error, you can run: %s uninstall\n' "$0" >&2
  fi
}
trap 'on_error "$LINENO"' ERR

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

package_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed$'
}

require_root() {
  [[ ${EUID:-$(id -u)} -eq 0 ]] || die "Run this script as root, for example: sudo $0"
}

check_ubuntu() {
  [[ -r /etc/os-release ]] || die "/etc/os-release was not found."
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || die "This installer supports Ubuntu only."
  case "${VERSION_ID:-}" in
    22.04|24.04) info "Ubuntu ${VERSION_ID} detected." ;;
    *) die "Supported Ubuntu versions are 22.04 and 24.04. Detected: ${VERSION_ID:-unknown}" ;;
  esac
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-N}"
  local answer

  while true; do
    if [[ "$default" == "Y" ]]; then
      read -r -p "$prompt [Y/n]: " answer || true
      answer=${answer:-Y}
    else
      read -r -p "$prompt [y/N]: " answer || true
      answer=${answer:-N}
    fi

    case "$answer" in
      y|Y|yes|YES|Yes) return 0 ;;
      n|N|no|NO|No) return 1 ;;
      *) echo "Please enter y or n." ;;
    esac
  done
}

ask_value() {
  local output_var="$1"
  local title="$2"
  local help_text="$3"
  local default_value="${4:-}"
  local value=""

  printf '\n%b%s%b\n' "$BOLD" "$title" "$RESET"
  printf '  %s\n' "$help_text"

  if [[ -n "$default_value" ]]; then
    read -r -p "  Value [${default_value}]: " value || true
    value=${value:-$default_value}
  else
    while [[ -z "$value" ]]; do
      read -r -p "  Value: " value || true
    done
  fi

  printf -v "$output_var" '%s' "$value"
}

is_ipv4() {
  local ip="$1"
  local a b c d extra
  IFS=. read -r a b c d extra <<<"$ip"
  [[ -z "${extra:-}" && -n "${a:-}" && -n "${b:-}" && -n "${c:-}" && -n "${d:-}" ]] || return 1

  local octet
  for octet in "$a" "$b" "$c" "$d"; do
    [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
    ((10#$octet >= 0 && 10#$octet <= 255)) || return 1
  done
}

ipv4_to_int() {
  local ip="$1"
  local a b c d
  IFS=. read -r a b c d <<<"$ip"
  printf '%u' "$(( (10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d ))"
}

cidr_parts() {
  local cidr="$1"
  local ip prefix
  IFS=/ read -r ip prefix <<<"$cidr"
  [[ -n "$ip" && -n "$prefix" && "$cidr" == */* ]] || return 1
  is_ipv4 "$ip" || return 1
  [[ "$prefix" =~ ^[0-9]{1,2}$ ]] || return 1
  ((10#$prefix >= 0 && 10#$prefix <= 32)) || return 1
  printf '%s %s\n' "$ip" "$prefix"
}

cidr_network_int() {
  local cidr="$1"
  local ip prefix ip_int mask
  IFS=' ' read -r ip prefix < <(cidr_parts "$cidr") || return 1
  ip_int=$(ipv4_to_int "$ip")
  mask=$(( (0xFFFFFFFF << (32 - 10#$prefix)) & 0xFFFFFFFF ))
  printf '%u' "$(( ip_int & mask ))"
}

cidr_broadcast_int() {
  local cidr="$1"
  local ip prefix network mask
  IFS=' ' read -r ip prefix < <(cidr_parts "$cidr") || return 1
  network=$(cidr_network_int "$cidr")
  mask=$(( (0xFFFFFFFF << (32 - 10#$prefix)) & 0xFFFFFFFF ))
  printf '%u' "$(( network | ((~mask) & 0xFFFFFFFF) ))"
}

validate_cidr() {
  local cidr="$1"
  local ip prefix ip_int network
  IFS=' ' read -r ip prefix < <(cidr_parts "$cidr") || return 1
  ((10#$prefix >= 1 && 10#$prefix <= 30)) || return 1
  ip_int=$(ipv4_to_int "$ip")
  network=$(cidr_network_int "$cidr")
  ((ip_int == network))
}

cidr_is_private() {
  local cidr="$1"
  local start end
  start=$(cidr_network_int "$cidr") || return 1
  end=$(cidr_broadcast_int "$cidr") || return 1

  local rfc1918_start rfc1918_end

  rfc1918_start=$(ipv4_to_int "10.0.0.0")
  rfc1918_end=$(ipv4_to_int "10.255.255.255")
  ((start >= rfc1918_start && end <= rfc1918_end)) && return 0

  rfc1918_start=$(ipv4_to_int "172.16.0.0")
  rfc1918_end=$(ipv4_to_int "172.31.255.255")
  ((start >= rfc1918_start && end <= rfc1918_end)) && return 0

  rfc1918_start=$(ipv4_to_int "192.168.0.0")
  rfc1918_end=$(ipv4_to_int "192.168.255.255")
  ((start >= rfc1918_start && end <= rfc1918_end)) && return 0

  return 1
}

cidr_overlaps() {
  local first="$1"
  local second="$2"
  local first_start first_end second_start second_end
  first_start=$(cidr_network_int "$first") || return 1
  first_end=$(cidr_broadcast_int "$first") || return 1
  second_start=$(cidr_network_int "$second") || return 1
  second_end=$(cidr_broadcast_int "$second") || return 1
  ((first_start <= second_end && second_start <= first_end))
}

route_overlap() {
  local vpn_cidr="$1"
  local route_line destination

  while IFS= read -r route_line; do
    [[ -n "$route_line" ]] || continue
    destination=${route_line%% *}
    [[ "$destination" != "default" ]] || continue

    if is_ipv4 "$destination"; then
      destination="${destination}/32"
    fi

    if cidr_parts "$destination" >/dev/null 2>&1; then
      if cidr_overlaps "$vpn_cidr" "$destination"; then
        printf '%s\n' "$destination"
        return 0
      fi
    fi
  done < <(ip -4 route show table main 2>/dev/null)

  return 1
}

validate_dns_list() {
  local value="$1"
  local item
  local count=0
  local old_ifs="$IFS"
  local -a items=()

  IFS=','
  read -ra items <<<"$value"
  IFS="$old_ifs"

  for item in "${items[@]}"; do
    item=${item//[[:space:]]/}
    [[ -n "$item" ]] || return 1
    is_ipv4 "$item" || return 1
    ((count += 1))
  done

  ((count >= 1))
}

normalize_dns_list() {
  local value="$1"
  local item
  local output=""
  local old_ifs="$IFS"
  local -a items=()

  IFS=','
  read -ra items <<<"$value"
  IFS="$old_ifs"

  for item in "${items[@]}"; do
    item=${item//[[:space:]]/}
    if [[ -z "$output" ]]; then
      output="$item"
    else
      output+=",${item}"
    fi
  done

  printf '%s' "$output"
}

validate_server_id() {
  local value="$1"

  if is_ipv4 "$value"; then
    return 0
  fi

  [[ ${#value} -le 253 ]] || return 1
  [[ "$value" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || return 1
  [[ "$value" != *".."* ]] || return 1
  [[ "$value" != *".-"* ]] || return 1
  [[ "$value" != *"-."* ]] || return 1
}

validate_ca_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._[:space:]-]{0,63}$ ]]
}

validate_interface() {
  ip link show dev "$1" >/dev/null 2>&1
}

validate_tcp_port() {
  local value="$1"
  [[ "$value" =~ ^[0-9]{1,5}$ ]] || return 1
  ((10#$value >= 1 && 10#$value <= 65535))
}

proxy_ip_is_safe() {
  local value="$1"
  is_ipv4 "$value" || return 1
  local point_cidr="${value}/32"
  cidr_is_private "$point_cidr" || return 1
  ! cidr_overlaps "$point_cidr" "$VPN_SUBNET"
}

default_interface() {
  ip -4 route show default 2>/dev/null | awk 'NR==1 {print $5}'
}

interface_ipv4() {
  local iface="$1"
  ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk 'NR==1 {split($4,a,"/"); print a[1]}'
}

backup_file() {
  local source_path="$1"
  local backup_name="$2"

  if [[ -e "$source_path" ]]; then
    cp -a "$source_path" "${BACKUP_DIR}/${backup_name}"
    return 0
  fi
  return 1
}

restore_file() {
  local target_path="$1"
  local backup_name="$2"
  local existed="$3"

  if [[ "$existed" == "yes" && -e "${BACKUP_DIR}/${backup_name}" ]]; then
    cp -a "${BACKUP_DIR}/${backup_name}" "$target_path"
  else
    rm -f "$target_path"
  fi
}

service_is_enabled() {
  systemctl is-enabled --quiet "$1" 2>/dev/null
}

service_is_active() {
  systemctl is-active --quiet "$1" 2>/dev/null
}

write_state() {
  umask 077
  {
    printf 'INSTALLER_VERSION=%q\n' "$INSTALLER_VERSION"
    printf 'SERVER_ID=%q\n' "$SERVER_ID"
    printf 'OUT_IF=%q\n' "$OUT_IF"
    printf 'VPN_SUBNET=%q\n' "$VPN_SUBNET"
    printf 'DNS_SERVERS=%q\n' "$DNS_SERVERS"
    printf 'CA_NAME=%q\n' "$CA_NAME"
    printf 'ALLOW_STOCK_WINDOWS=%q\n' "$ALLOW_STOCK_WINDOWS"
    printf 'CLIENT_DIR=%q\n' "$CLIENT_DIR"
    printf 'NEW_PACKAGES=%q\n' "$NEW_PACKAGES"

    printf 'STRONGSWAN_WAS_ENABLED=%q\n' "$STRONGSWAN_WAS_ENABLED"
    printf 'STRONGSWAN_WAS_ACTIVE=%q\n' "$STRONGSWAN_WAS_ACTIVE"
    printf 'FW_SERVICE_WAS_ENABLED=%q\n' "$FW_SERVICE_WAS_ENABLED"
    printf 'FW_SERVICE_WAS_ACTIVE=%q\n' "$FW_SERVICE_WAS_ACTIVE"

    printf 'OLD_IP_FORWARD=%q\n' "$OLD_IP_FORWARD"
    printf 'OLD_ACCEPT_REDIRECTS_ALL=%q\n' "$OLD_ACCEPT_REDIRECTS_ALL"
    printf 'OLD_ACCEPT_REDIRECTS_DEFAULT=%q\n' "$OLD_ACCEPT_REDIRECTS_DEFAULT"
    printf 'OLD_SEND_REDIRECTS_ALL=%q\n' "$OLD_SEND_REDIRECTS_ALL"
    printf 'OLD_SEND_REDIRECTS_DEFAULT=%q\n' "$OLD_SEND_REDIRECTS_DEFAULT"

    printf 'IPSEC_CONF_EXISTED=%q\n' "$IPSEC_CONF_EXISTED"
    printf 'IPSEC_SECRETS_EXISTED=%q\n' "$IPSEC_SECRETS_EXISTED"
    printf 'SYSCTL_FILE_EXISTED=%q\n' "$SYSCTL_FILE_EXISTED"
    printf 'FW_SCRIPT_EXISTED=%q\n' "$FW_SCRIPT_EXISTED"
    printf 'FW_SERVICE_FILE_EXISTED=%q\n' "$FW_SERVICE_FILE_EXISTED"
    printf 'CA_KEY_EXISTED=%q\n' "$CA_KEY_EXISTED"
    printf 'CA_CERT_EXISTED=%q\n' "$CA_CERT_EXISTED"
    printf 'SERVER_KEY_EXISTED=%q\n' "$SERVER_KEY_EXISTED"
    printf 'SERVER_CERT_EXISTED=%q\n' "$SERVER_CERT_EXISTED"

    printf 'PROXY_ENABLED=%q\n' "$PROXY_ENABLED"
    printf 'PROXY_IP=%q\n' "$PROXY_IP"
    printf 'PROXY_PORT=%q\n' "$PROXY_PORT"
    printf 'PROXY_BASELINE_RECORDED=%q\n' "$PROXY_BASELINE_RECORDED"
    printf 'PROXY_CONF_EXISTED=%q\n' "$PROXY_CONF_EXISTED"
    printf 'PROXY_FW_SCRIPT_EXISTED=%q\n' "$PROXY_FW_SCRIPT_EXISTED"
    printf 'PROXY_SERVICE_FILE_EXISTED=%q\n' "$PROXY_SERVICE_FILE_EXISTED"
    printf 'PROXY_SERVICE_WAS_ENABLED=%q\n' "$PROXY_SERVICE_WAS_ENABLED"
    printf 'PROXY_SERVICE_WAS_ACTIVE=%q\n' "$PROXY_SERVICE_WAS_ACTIVE"
    printf 'DANTE_DEFAULT_WAS_ENABLED=%q\n' "$DANTE_DEFAULT_WAS_ENABLED"
    printf 'DANTE_DEFAULT_WAS_ACTIVE=%q\n' "$DANTE_DEFAULT_WAS_ACTIVE"
    printf 'DANTE_PACKAGE_WAS_INSTALLED=%q\n' "$DANTE_PACKAGE_WAS_INSTALLED"
  } > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
}

initialize_proxy_state_defaults() {
  PROXY_ENABLED="${PROXY_ENABLED:-no}"
  PROXY_IP="${PROXY_IP:-$PROXY_DEFAULT_IP}"
  PROXY_PORT="${PROXY_PORT:-$PROXY_DEFAULT_PORT}"
  PROXY_BASELINE_RECORDED="${PROXY_BASELINE_RECORDED:-no}"
  PROXY_CONF_EXISTED="${PROXY_CONF_EXISTED:-no}"
  PROXY_FW_SCRIPT_EXISTED="${PROXY_FW_SCRIPT_EXISTED:-no}"
  PROXY_SERVICE_FILE_EXISTED="${PROXY_SERVICE_FILE_EXISTED:-no}"
  PROXY_SERVICE_WAS_ENABLED="${PROXY_SERVICE_WAS_ENABLED:-no}"
  PROXY_SERVICE_WAS_ACTIVE="${PROXY_SERVICE_WAS_ACTIVE:-no}"
  DANTE_DEFAULT_WAS_ENABLED="${DANTE_DEFAULT_WAS_ENABLED:-no}"
  DANTE_DEFAULT_WAS_ACTIVE="${DANTE_DEFAULT_WAS_ACTIVE:-no}"
  DANTE_PACKAGE_WAS_INSTALLED="${DANTE_PACKAGE_WAS_INSTALLED:-no}"
}

append_new_package() {
  local package="$1"
  case " ${NEW_PACKAGES:-} " in
    *" ${package} "*) ;;
    *) NEW_PACKAGES="${NEW_PACKAGES:-}${package} " ;;
  esac
}

record_proxy_baseline() {
  initialize_proxy_state_defaults
  [[ "$PROXY_BASELINE_RECORDED" == "yes" ]] && return 0

  mkdir -p "$BACKUP_DIR"
  chmod 700 "$STATE_DIR" "$BACKUP_DIR"

  PROXY_CONF_EXISTED="no"
  PROXY_FW_SCRIPT_EXISTED="no"
  PROXY_SERVICE_FILE_EXISTED="no"
  PROXY_SERVICE_WAS_ENABLED="no"
  PROXY_SERVICE_WAS_ACTIVE="no"
  DANTE_DEFAULT_WAS_ENABLED="no"
  DANTE_DEFAULT_WAS_ACTIVE="no"
  DANTE_PACKAGE_WAS_INSTALLED="no"

  backup_file "$PROXY_CONF" "proxy-danted.conf" && PROXY_CONF_EXISTED="yes" || true
  backup_file "$PROXY_FW_SCRIPT" "ikev2-vpn-proxy-firewall" && PROXY_FW_SCRIPT_EXISTED="yes" || true
  backup_file "$PROXY_SERVICE_FILE" "ikev2-vpn-proxy.service" && PROXY_SERVICE_FILE_EXISTED="yes" || true

  service_is_enabled "$PROXY_SERVICE" && PROXY_SERVICE_WAS_ENABLED="yes" || true
  service_is_active "$PROXY_SERVICE" && PROXY_SERVICE_WAS_ACTIVE="yes" || true
  service_is_enabled "$DANTE_DEFAULT_SERVICE" && DANTE_DEFAULT_WAS_ENABLED="yes" || true
  service_is_active "$DANTE_DEFAULT_SERVICE" && DANTE_DEFAULT_WAS_ACTIVE="yes" || true
  package_installed "$PROXY_PACKAGE" && DANTE_PACKAGE_WAS_INSTALLED="yes" || true

  PROXY_BASELINE_RECORDED="yes"
}

generate_password() {
  openssl rand -hex 16
}

escape_secret() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s' "$value"
}

username_exists() {
  local needle="$1"
  local current
  for current in "${USER_NAMES[@]:-}"; do
    [[ "$current" == "$needle" ]] && return 0
  done
  return 1
}

collect_users() {
  USER_NAMES=()
  USER_PASSWORDS=()

  while true; do
    local username=""
    local password=""
    local password_repeat=""

    printf '\n%bVPN user%b\n' "$BOLD" "$RESET"
    printf '  Username used for EAP-MSCHAPv2 authentication. Allowed characters: A-Z a-z 0-9 . _ @ -\n'

    while true; do
      read -r -p '  Username: ' username || true
      if [[ ! "$username" =~ ^[A-Za-z0-9._@-]{1,64}$ ]]; then
        warn "Enter a valid username with 1 to 64 allowed characters."
        continue
      fi
      if username_exists "$username"; then
        warn "That username has already been added."
        continue
      fi
      break
    done

    printf '  Password for this user. Leave it empty to generate a random 32-character password.\n'
    read -r -s -p '  Password (empty = generate): ' password || true
    echo

    if [[ -z "$password" ]]; then
      password=$(generate_password)
      info "A random password was generated for ${username}. It will be shown once after installation."
    else
      [[ ${#password} -ge 12 ]] || { warn "Use a password with at least 12 characters."; continue; }
      read -r -s -p '  Repeat password: ' password_repeat || true
      echo
      [[ "$password" == "$password_repeat" ]] || { warn "Passwords do not match."; continue; }
    fi

    USER_NAMES+=("$username")
    USER_PASSWORDS+=("$password")

    ask_yes_no "Add another VPN user?" N || break
  done
}

record_preinstall_state() {
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$STATE_DIR" "$BACKUP_DIR"

  STRONGSWAN_WAS_ENABLED="no"
  STRONGSWAN_WAS_ACTIVE="no"
  FW_SERVICE_WAS_ENABLED="no"
  FW_SERVICE_WAS_ACTIVE="no"

  service_is_enabled "$STRONGSWAN_SERVICE" && STRONGSWAN_WAS_ENABLED="yes" || true
  service_is_active "$STRONGSWAN_SERVICE" && STRONGSWAN_WAS_ACTIVE="yes" || true
  service_is_enabled "$FW_SERVICE" && FW_SERVICE_WAS_ENABLED="yes" || true
  service_is_active "$FW_SERVICE" && FW_SERVICE_WAS_ACTIVE="yes" || true

  OLD_IP_FORWARD=$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)
  OLD_ACCEPT_REDIRECTS_ALL=$(sysctl -n net.ipv4.conf.all.accept_redirects 2>/dev/null || echo 1)
  OLD_ACCEPT_REDIRECTS_DEFAULT=$(sysctl -n net.ipv4.conf.default.accept_redirects 2>/dev/null || echo 1)
  OLD_SEND_REDIRECTS_ALL=$(sysctl -n net.ipv4.conf.all.send_redirects 2>/dev/null || echo 1)
  OLD_SEND_REDIRECTS_DEFAULT=$(sysctl -n net.ipv4.conf.default.send_redirects 2>/dev/null || echo 1)

  IPSEC_CONF_EXISTED="no"
  IPSEC_SECRETS_EXISTED="no"
  SYSCTL_FILE_EXISTED="no"
  FW_SCRIPT_EXISTED="no"
  FW_SERVICE_FILE_EXISTED="no"
  CA_KEY_EXISTED="no"
  CA_CERT_EXISTED="no"
  SERVER_KEY_EXISTED="no"
  SERVER_CERT_EXISTED="no"

  backup_file "$IPSEC_CONF" "ipsec.conf" && IPSEC_CONF_EXISTED="yes" || true
  backup_file "$IPSEC_SECRETS" "ipsec.secrets" && IPSEC_SECRETS_EXISTED="yes" || true
  backup_file "$SYSCTL_FILE" "99-ikev2-vpn.conf" && SYSCTL_FILE_EXISTED="yes" || true
  backup_file "$FW_SCRIPT" "ikev2-vpn-firewall" && FW_SCRIPT_EXISTED="yes" || true
  backup_file "$FW_SERVICE_FILE" "ikev2-vpn-firewall.service" && FW_SERVICE_FILE_EXISTED="yes" || true
  backup_file "$CA_KEY" "ca-key.pem" && CA_KEY_EXISTED="yes" || true
  backup_file "$CA_CERT" "ca-cert.pem" && CA_CERT_EXISTED="yes" || true
  backup_file "$SERVER_KEY" "server-key.pem" && SERVER_KEY_EXISTED="yes" || true
  backup_file "$SERVER_CERT" "server-cert.pem" && SERVER_CERT_EXISTED="yes" || true

  NEW_PACKAGES=""
  local package
  for package in "${REQUESTED_PACKAGES[@]}"; do
    if ! package_installed "$package"; then
      NEW_PACKAGES+="${package} "
    fi
  done

  initialize_proxy_state_defaults
  record_proxy_baseline
  write_state
}

install_packages() {
  log "Updating the Ubuntu package index..."
  apt-get update

  log "Installing StrongSwan and required packages..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${REQUESTED_PACKAGES[@]}"

  command_exists ipsec || die "The ipsec command was not installed."
  command_exists pki || die "The pki command was not installed."
  command_exists iptables || die "The iptables command was not installed."
  command_exists openssl || die "The openssl command was not installed."

  [[ -f /usr/lib/ipsec/plugins/libstrongswan-eap-mschapv2.so ]] || \
    die "The EAP-MSCHAPv2 plugin file was not found after package installation."
}

create_certificates() {
  log "Generating the private CA and server certificate..."

  install -d -m 700 /etc/ipsec.d/private
  install -d -m 755 /etc/ipsec.d/cacerts /etc/ipsec.d/certs

  umask 077
  pki --gen --type rsa --size 4096 --outform pem > "$CA_KEY"
  pki --self --ca --lifetime 3650 --in "$CA_KEY" --type rsa \
    --dn "CN=${CA_NAME}" --outform pem > "$CA_CERT"

  pki --gen --type rsa --size 3072 --outform pem > "$SERVER_KEY"

  if is_ipv4 "$SERVER_ID"; then
    pki --pub --in "$SERVER_KEY" --type rsa | \
      pki --issue --lifetime 1825 --cacert "$CA_CERT" --cakey "$CA_KEY" \
        --dn "CN=${SERVER_ID}" \
        --san "$SERVER_ID" \
        --san "dns:${SERVER_ID}" \
        --flag serverAuth \
        --flag ikeIntermediate \
        --outform pem > "$SERVER_CERT"
  else
    pki --pub --in "$SERVER_KEY" --type rsa | \
      pki --issue --lifetime 1825 --cacert "$CA_CERT" --cakey "$CA_KEY" \
        --dn "CN=${SERVER_ID}" \
        --san "$SERVER_ID" \
        --flag serverAuth \
        --flag ikeIntermediate \
        --outform pem > "$SERVER_CERT"
  fi

  chmod 600 "$CA_KEY" "$SERVER_KEY"
  chmod 644 "$CA_CERT" "$SERVER_CERT"

  install -d -m 700 "$CLIENT_DIR"
  install -m 644 "$CA_CERT" "${CLIENT_DIR}/ca-cert.pem"
  openssl x509 -in "$CA_CERT" -outform der -out "${CLIENT_DIR}/ca-cert.cer"
  install -m 644 "$SERVER_CERT" "${CLIENT_DIR}/server-cert.pem"
}

write_ipsec_config() {
  local ike_proposals
  local esp_proposals

  if [[ "$ALLOW_STOCK_WINDOWS" == "yes" ]]; then
    ike_proposals="aes256-sha256-modp2048,aes128-sha256-modp2048,aes256-sha256-modp1024,aes128-sha256-modp1024"
    esp_proposals="aes256-sha256,aes128-sha256,aes256-sha1,aes128-sha1"
  else
    ike_proposals="aes256-sha256-modp2048,aes128-sha256-modp2048"
    esp_proposals="aes256-sha256,aes128-sha256"
  fi

  log "Writing the StrongSwan connection configuration..."

  cat > "$IPSEC_CONF" <<EOF
# Managed by ${INSTALLER_NAME} ${INSTALLER_VERSION}
config setup
    uniqueids=no
    charondebug="ike 1, knl 1, cfg 1"

conn ikev2-eap
    keyexchange=ikev2
    type=tunnel
    fragmentation=yes
    mobike=yes
    compress=no
    dpdaction=clear
    dpddelay=30s
    rekey=no
    reauth=no

    left=%any
    leftid=${SERVER_ID}
    leftauth=pubkey
    leftcert=ikev2-installer-server-cert.pem
    leftsendcert=always
    leftsubnet=0.0.0.0/0

    right=%any
    rightid=%any
    rightauth=eap-mschapv2
    eap_identity=%identity
    rightsourceip=${VPN_SUBNET}
    rightdns=${DNS_SERVERS}

    ike=${ike_proposals}
    esp=${esp_proposals}

    auto=add
EOF

  umask 077
  {
    echo "# Managed by ${INSTALLER_NAME} ${INSTALLER_VERSION}"
    echo ": RSA ikev2-installer-server-key.pem"

    local index escaped_password
    for ((index=0; index<${#USER_NAMES[@]}; index++)); do
      escaped_password=$(escape_secret "${USER_PASSWORDS[$index]}")
      printf '%s : EAP "%s"\n' "${USER_NAMES[$index]}" "$escaped_password"
    done
  } > "$IPSEC_SECRETS"

  chmod 600 "$IPSEC_SECRETS"
}

write_sysctl_config() {
  log "Enabling IPv4 forwarding for VPN traffic..."

  cat > "$SYSCTL_FILE" <<EOF
# Managed by ${INSTALLER_NAME} ${INSTALLER_VERSION}
net.ipv4.ip_forward = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
EOF

  sysctl -p "$SYSCTL_FILE" >/dev/null
}

write_firewall_config() {
  log "Creating persistent firewall and NAT rules..."

  cat > "$FW_SCRIPT" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail

IPTABLES="\$(command -v iptables)"
VPN_SUBNET="${VPN_SUBNET}"
OUT_IF="${OUT_IF}"
TAG="${INSTALLER_NAME}"

add_filter_rule() {
  local chain="\$1"
  local position="\$2"
  shift 2
  "\$IPTABLES" -C "\$chain" "\$@" >/dev/null 2>&1 || "\$IPTABLES" -I "\$chain" "\$position" "\$@"
}

add_nat_rule() {
  local chain="\$1"
  shift
  "\$IPTABLES" -t nat -C "\$chain" "\$@" >/dev/null 2>&1 || "\$IPTABLES" -t nat -A "\$chain" "\$@"
}

delete_filter_rule() {
  local chain="\$1"
  shift
  while "\$IPTABLES" -C "\$chain" "\$@" >/dev/null 2>&1; do
    "\$IPTABLES" -D "\$chain" "\$@"
  done
}

delete_nat_rule() {
  local chain="\$1"
  shift
  while "\$IPTABLES" -t nat -C "\$chain" "\$@" >/dev/null 2>&1; do
    "\$IPTABLES" -t nat -D "\$chain" "\$@"
  done
}

start_rules() {
  add_filter_rule INPUT 1 -p udp --dport 500 -m comment --comment "\$TAG" -j ACCEPT
  add_filter_rule INPUT 1 -p udp --dport 4500 -m comment --comment "\$TAG" -j ACCEPT
  add_filter_rule FORWARD 1 -s "\$VPN_SUBNET" -o "\$OUT_IF" -m comment --comment "\$TAG" -j ACCEPT
  add_filter_rule FORWARD 1 -d "\$VPN_SUBNET" -i "\$OUT_IF" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "\$TAG" -j ACCEPT
  add_nat_rule POSTROUTING -s "\$VPN_SUBNET" -o "\$OUT_IF" -m comment --comment "\$TAG" -j MASQUERADE
}

stop_rules() {
  delete_filter_rule INPUT -p udp --dport 500 -m comment --comment "\$TAG" -j ACCEPT
  delete_filter_rule INPUT -p udp --dport 4500 -m comment --comment "\$TAG" -j ACCEPT
  delete_filter_rule FORWARD -s "\$VPN_SUBNET" -o "\$OUT_IF" -m comment --comment "\$TAG" -j ACCEPT
  delete_filter_rule FORWARD -d "\$VPN_SUBNET" -i "\$OUT_IF" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "\$TAG" -j ACCEPT
  delete_nat_rule POSTROUTING -s "\$VPN_SUBNET" -o "\$OUT_IF" -m comment --comment "\$TAG" -j MASQUERADE
}

case "\${1:-start}" in
  start) start_rules ;;
  stop) stop_rules ;;
  restart) stop_rules; start_rules ;;
  *) echo "Usage: \$0 {start|stop|restart}" >&2; exit 2 ;;
esac
EOF

  chmod 700 "$FW_SCRIPT"

  cat > "$FW_SERVICE_FILE" <<EOF
[Unit]
Description=Firewall and NAT rules for the IKEv2 VPN
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${FW_SCRIPT} start
ExecStop=${FW_SCRIPT} stop

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now "$FW_SERVICE"
}

write_client_files() {
  local credentials_file="${CLIENT_DIR}/client-credentials.txt"

  cat > "${CLIENT_DIR}/client-info.txt" <<EOF
IKEv2 VPN client information
=============================
Server address: ${SERVER_ID}
Remote ID: ${SERVER_ID}
Authentication: EAP-MSCHAPv2 username and password
VPN address pool: ${VPN_SUBNET}
DNS servers: ${DNS_SERVERS}
CA certificate for Windows: ca-cert.cer
CA certificate in PEM format: ca-cert.pem

Client setup summary
--------------------
1. Import the CA certificate into the trusted root CA store.
2. Create an IKEv2 VPN connection to the server address shown above.
3. Use the same server address as the remote/server identity when the client asks for it.
4. Authenticate with one of the configured usernames and passwords.
5. Allow UDP ports 500 and 4500 in any external cloud firewall or security group.
EOF

  if [[ "$ALLOW_STOCK_WINDOWS" == "no" ]]; then
    cat >> "${CLIENT_DIR}/client-info.txt" <<'EOF'

Windows note
------------
The secure profile does not allow MODP-1024 or ESP SHA-1 fallback. A Windows client may require an explicit IPsec policy configured with PowerShell before it can connect.
EOF
  else
    cat >> "${CLIENT_DIR}/client-info.txt" <<'EOF'

Windows note
------------
Stock Windows compatibility is enabled. The server permits the legacy MODP-1024 key exchange and ESP SHA-1 fallback because default Windows IKEv2 proposals may require them.
EOF
  fi

  chmod 600 "${CLIENT_DIR}/client-info.txt"

  umask 077
  {
    echo "IKEv2 VPN credentials"
    echo "======================"
    echo
    local index
    for ((index=0; index<${#USER_NAMES[@]}; index++)); do
      printf 'Username: %s\n' "${USER_NAMES[$index]}"
      printf 'Password: %s\n\n' "${USER_PASSWORDS[$index]}"
    done
  } > "$credentials_file"
  chmod 600 "$credentials_file"
}

update_client_proxy_info() {
  local info_file="${CLIENT_DIR}/client-info.txt"
  [[ -f "$info_file" ]] || return 0

  local temp
  temp=$(mktemp)
  awk -v begin="$PROXY_INFO_BEGIN" -v end="$PROXY_INFO_END" '
    $0 == begin {skip=1; next}
    $0 == end {skip=0; next}
    !skip {print}
  ' "$info_file" > "$temp"
  cat "$temp" > "$info_file"
  rm -f "$temp"

  if [[ "$PROXY_ENABLED" == "yes" ]]; then
    cat >> "$info_file" <<EOF

${PROXY_INFO_BEGIN}

Private SOCKS5 Proxy Mode
-------------------------
Proxy host: ${PROXY_IP}
Proxy port: ${PROXY_PORT}
Access: IKEv2 VPN clients only
Protocol: SOCKS5 TCP CONNECT

Windows Proxy Mode uses split tunneling and routes only ${PROXY_IP}/32 through the IKEv2 connection.
All other Windows traffic remains on the normal Internet connection unless the application is explicitly configured to use this SOCKS5 proxy.

${PROXY_INFO_END}
EOF
  fi

  chmod 600 "$info_file"
}

install_proxy_package() {
  if package_installed "$PROXY_PACKAGE"; then
    return 0
  fi

  append_new_package "$PROXY_PACKAGE"
  write_state

  log "Installing the SOCKS5 proxy package (${PROXY_PACKAGE})..."
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$PROXY_PACKAGE"

  if [[ "$DANTE_PACKAGE_WAS_INSTALLED" == "no" ]]; then
    systemctl disable --now "$DANTE_DEFAULT_SERVICE" >/dev/null 2>&1 || true
  fi
}

write_proxy_config() {
  log "Writing the isolated SOCKS5 proxy configuration..."
  install -d -m 700 "$PROXY_CONFIG_DIR"

  cat > "$PROXY_CONF" <<EOF
# Managed by ${INSTALLER_NAME} ${CURRENT_INSTALLER_VERSION}
# Private SOCKS5 listener reachable only through the IKEv2 tunnel.
logoutput: syslog

internal: ${PROXY_IP} port = ${PROXY_PORT}
external: ${OUT_IF}

clientmethod: none
socksmethod: none

user.privileged: root
user.unprivileged: nobody

client pass {
    from: ${VPN_SUBNET}
    to: 0.0.0.0/0
    log: connect disconnect error
}

socks pass {
    from: ${VPN_SUBNET}
    to: 0.0.0.0/0
    command: connect
    log: connect disconnect error
}
EOF
  chmod 600 "$PROXY_CONF"
}

write_proxy_firewall_script() {
  cat > "$PROXY_FW_SCRIPT" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail

IPTABLES="\$(command -v iptables)"
VPN_SUBNET="${VPN_SUBNET}"
PROXY_IP="${PROXY_IP}"
PROXY_PORT="${PROXY_PORT}"
TAG="${INSTALLER_NAME}-proxy"

add_rule() {
  local position="\$1"
  shift
  "\$IPTABLES" -C INPUT "\$@" >/dev/null 2>&1 || "\$IPTABLES" -I INPUT "\$position" "\$@"
}

delete_rule() {
  while "\$IPTABLES" -C INPUT "\$@" >/dev/null 2>&1; do
    "\$IPTABLES" -D INPUT "\$@"
  done
}

start_rules() {
  add_rule 1 -s "\$VPN_SUBNET" -d "\$PROXY_IP" -p tcp --dport "\$PROXY_PORT" -m policy --dir in --pol ipsec -m comment --comment "\$TAG-allow" -j ACCEPT
  add_rule 2 -d "\$PROXY_IP" -p tcp --dport "\$PROXY_PORT" -m comment --comment "\$TAG-deny" -j DROP
}

stop_rules() {
  delete_rule -s "\$VPN_SUBNET" -d "\$PROXY_IP" -p tcp --dport "\$PROXY_PORT" -m policy --dir in --pol ipsec -m comment --comment "\$TAG-allow" -j ACCEPT
  delete_rule -d "\$PROXY_IP" -p tcp --dport "\$PROXY_PORT" -m comment --comment "\$TAG-deny" -j DROP
}

case "\${1:-start}" in
  start) start_rules ;;
  stop) stop_rules ;;
  restart) stop_rules; start_rules ;;
  *) echo "Usage: \$0 {start|stop|restart}" >&2; exit 2 ;;
esac
EOF
  chmod 700 "$PROXY_FW_SCRIPT"
}

write_proxy_service() {
  local ip_cmd danted_cmd
  ip_cmd=$(command -v ip)
  danted_cmd=$(command -v danted || true)
  [[ -n "$danted_cmd" ]] || die "The danted binary was not found after installing ${PROXY_PACKAGE}."

  cat > "$PROXY_SERVICE_FILE" <<EOF
[Unit]
Description=Private SOCKS5 proxy over the managed IKEv2 VPN
After=network-online.target ${STRONGSWAN_SERVICE}
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/bin/sh -c '${ip_cmd} address show dev lo | grep -Fq "${PROXY_IP}/32" || ${ip_cmd} address add ${PROXY_IP}/32 dev lo'
ExecStartPre=${PROXY_FW_SCRIPT} start
ExecStart=${danted_cmd} -f ${PROXY_CONF}
ExecStopPost=${PROXY_FW_SCRIPT} stop
ExecStopPost=/bin/sh -c '${ip_cmd} address del ${PROXY_IP}/32 dev lo >/dev/null 2>&1 || true'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 "$PROXY_SERVICE_FILE"
}

stop_proxy_runtime() {
  systemctl disable --now "$PROXY_SERVICE" >/dev/null 2>&1 || true
  [[ -x "$PROXY_FW_SCRIPT" ]] && "$PROXY_FW_SCRIPT" stop >/dev/null 2>&1 || true

  if is_ipv4 "${PROXY_IP:-}"; then
    ip address del "${PROXY_IP}/32" dev lo >/dev/null 2>&1 || true
  fi
}

configure_proxy_mode() {
  record_proxy_baseline
  install_proxy_package

  # Only the isolated proxy runtime is stopped/replaced here. StrongSwan and
  # the original full-tunnel firewall/NAT service are intentionally untouched.
  stop_proxy_runtime
  write_proxy_config
  write_proxy_firewall_script

  local danted_cmd
  danted_cmd=$(command -v danted || true)
  [[ -n "$danted_cmd" ]] || die "The danted binary is unavailable."
  if ! "$danted_cmd" -V -f "$PROXY_CONF"; then
    die "Dante rejected the generated proxy configuration."
  fi

  write_proxy_service

  systemctl daemon-reload
  systemctl enable --now "$PROXY_SERVICE"
  sleep 1

  if ! service_is_active "$PROXY_SERVICE"; then
    systemctl --no-pager -l status "$PROXY_SERVICE" || true
    journalctl -u "$PROXY_SERVICE" -n 80 --no-pager || true
    die "The private SOCKS5 proxy failed to start."
  fi

  if ! ss -lnt 2>/dev/null | grep -Fq "${PROXY_IP}:${PROXY_PORT}"; then
    warn "The proxy service is active, but ${PROXY_IP}:${PROXY_PORT} was not visible in ss output."
  fi

  update_client_proxy_info
  log "Private SOCKS5 Proxy Mode is ready at ${PROXY_IP}:${PROXY_PORT}."
}

collect_proxy_settings() {
  local default_ip="${PROXY_IP:-$PROXY_DEFAULT_IP}"
  local default_port="${PROXY_PORT:-$PROXY_DEFAULT_PORT}"

  while true; do
    ask_value PROXY_IP \
      "Private SOCKS5 proxy IP" \
      "Dedicated private /32 reachable through IKEv2 only. It must not overlap VPN client subnet ${VPN_SUBNET}." \
      "$default_ip"
    proxy_ip_is_safe "$PROXY_IP" && break
    warn "Enter a private IPv4 address outside the VPN client subnet. Example: ${PROXY_DEFAULT_IP}"
  done

  while true; do
    ask_value PROXY_PORT \
      "Private SOCKS5 proxy TCP port" \
      "TCP port used by applications in Proxy Mode." \
      "$default_port"
    validate_tcp_port "$PROXY_PORT" && break
    warn "Enter a TCP port between 1 and 65535."
  done
}

upgrade_vpn() {
  [[ -f "$STATE_FILE" ]] || die "No installation managed by this script was found. Run install first."

  # shellcheck disable=SC1090
  source "$STATE_FILE"
  local previous_version="${INSTALLER_VERSION:-unknown}"
  INSTALLER_VERSION="$CURRENT_INSTALLER_VERSION"
  initialize_proxy_state_defaults
  record_proxy_baseline

  printf '\n%bIKEv2 Server v6 Upgrade / Proxy Mode%b\n' "$BOLD" "$RESET"
  printf '  Existing installation : %s\n' "$previous_version"
  printf '  Server ID             : %s\n' "$SERVER_ID"
  printf '  VPN subnet            : %s\n' "$VPN_SUBNET"
  printf '  StrongSwan            : %s\n' "$(systemctl is-active "$STRONGSWAN_SERVICE" 2>/dev/null || true)"
  printf '\nThis upgrade does NOT regenerate certificates, rewrite VPN users, replace ipsec.conf,\n'
  printf 'restart StrongSwan, or modify the existing full-tunnel NAT/firewall service.\n'
  printf 'It only adds/updates the isolated private SOCKS5 Proxy Mode.\n'

  collect_proxy_settings
  PROXY_ENABLED="yes"

  printf '\n%bProxy Mode summary%b\n' "$BOLD" "$RESET"
  printf '  Proxy IP       : %s\n' "$PROXY_IP"
  printf '  Proxy port     : %s\n' "$PROXY_PORT"
  printf '  Allowed source : %s (IKEv2 clients only)\n' "$VPN_SUBNET"
  printf '  Internet NIC   : %s\n' "$OUT_IF"

  if ! ask_yes_no "Apply the v6 Proxy Mode upgrade?" Y; then
    info "Upgrade canceled by the user."
    return 0
  fi

  write_state
  configure_proxy_mode
  write_state

  printf '\n%b================ V6 PROXY MODE READY ================%b\n' "$GREEN" "$RESET"
  printf 'SOCKS5 host : %s\n' "$PROXY_IP"
  printf 'SOCKS5 port : %s\n' "$PROXY_PORT"
  printf 'Access      : IKEv2 clients only\n'
  printf 'StrongSwan  : unchanged / not restarted by this upgrade\n'
  printf '\nWindows Proxy Mode should use split tunneling and route only:\n'
  printf '  %s/32 -> IKEv2\n' "$PROXY_IP"
  printf '\nRun: sudo %s status\n' "$0"
}


start_and_verify() {
  log "Starting StrongSwan..."

  systemctl enable "$STRONGSWAN_SERVICE" >/dev/null
  systemctl restart "$STRONGSWAN_SERVICE"
  sleep 1

  if ! service_is_active "$STRONGSWAN_SERVICE"; then
    systemctl --no-pager -l status "$STRONGSWAN_SERVICE" || true
    journalctl -u "$STRONGSWAN_SERVICE" -n 80 --no-pager || true
    die "StrongSwan failed to start. Review the service output above."
  fi

  if ! ipsec statusall 2>/dev/null | grep -q 'ikev2-eap'; then
    warn "StrongSwan is active, but the ikev2-eap connection was not visible in ipsec statusall."
  fi

  if ! iptables -C INPUT -p udp --dport 500 -m comment --comment "$INSTALLER_NAME" -j ACCEPT >/dev/null 2>&1; then
    warn "The UDP/500 firewall rule could not be verified."
  fi

  if ! iptables -C INPUT -p udp --dport 4500 -m comment --comment "$INSTALLER_NAME" -j ACCEPT >/dev/null 2>&1; then
    warn "The UDP/4500 firewall rule could not be verified."
  fi

  log "StrongSwan is running."
}

show_install_result() {
  printf '\n%b================ IKEv2 VPN READY ================%b\n' "$GREEN" "$RESET"
  printf 'Server / Remote ID : %s\n' "$SERVER_ID"
  printf 'Internet interface : %s\n' "$OUT_IF"
  printf 'VPN subnet         : %s\n' "$VPN_SUBNET"
  printf 'DNS servers        : %s\n' "$DNS_SERVERS"
  printf 'Client files       : %s\n' "$CLIENT_DIR"
  printf 'UDP ports          : 500, 4500\n'
  printf 'Windows profile    : %s\n' "$( [[ "$ALLOW_STOCK_WINDOWS" == "yes" ]] && echo 'stock-compatible' || echo 'secure' )"
  if [[ "${PROXY_ENABLED:-no}" == "yes" ]]; then
    printf 'SOCKS5 Proxy Mode  : %s:%s (VPN-only)\n' "$PROXY_IP" "$PROXY_PORT"
  else
    printf 'SOCKS5 Proxy Mode  : disabled\n'
  fi

  printf '\n%bConfigured users%b\n' "$BOLD" "$RESET"
  local index
  for ((index=0; index<${#USER_NAMES[@]}; index++)); do
    printf '  Username: %s\n' "${USER_NAMES[$index]}"
    printf '  Password: %s\n' "${USER_PASSWORDS[$index]}"
  done

  printf '\nImport this CA certificate into each client trusted root store:\n'
  printf '  %s\n' "${CLIENT_DIR}/ca-cert.cer"
  printf '\nRoot-only credential copy:\n'
  printf '  %s\n' "${CLIENT_DIR}/client-credentials.txt"
  printf '\nStatus:    sudo %s status\n' "$0"
  printf 'Uninstall: sudo %s uninstall\n' "$0"
}

install_vpn() {
  [[ ! -f "$STATE_FILE" ]] || die "A managed installation already exists. Run status or uninstall first."

  local detected_iface detected_ip overlap
  detected_iface=$(default_interface)
  [[ -n "$detected_iface" ]] || die "No default IPv4 route was found."
  detected_ip=$(interface_ipv4 "$detected_iface")

  printf '\n%bIKEv2 / StrongSwan Installer%b\n' "$BOLD" "$RESET"
  printf 'Ubuntu 22.04 / 24.04, IPv4 full tunnel, EAP-MSCHAPv2\n'

  while true; do
    ask_value SERVER_ID \
      "Server address / identity" \
      "Public FQDN or IPv4 address used by VPN clients. The same value is written into the server certificate." \
      "${detected_ip:-}"
    validate_server_id "$SERVER_ID" && break
    warn "Enter a valid FQDN, hostname, or IPv4 address."
  done

  while true; do
    ask_value OUT_IF \
      "Internet interface" \
      "Interface that carries Internet traffic and will be used for VPN client NAT." \
      "$detected_iface"
    validate_interface "$OUT_IF" && break
    warn "Interface ${OUT_IF} does not exist."
  done

  while true; do
    ask_value VPN_SUBNET \
      "VPN client subnet" \
      "IPv4 network assigned to VPN clients. Use a dedicated subnet that does not overlap server or LAN routes." \
      "10.10.10.0/24"
    validate_cidr "$VPN_SUBNET" && break
    warn "Enter a valid network CIDR with a network address, for example: 10.10.10.0/24"
  done

  if ! cidr_is_private "$VPN_SUBNET"; then
    warn "The selected VPN subnet is not inside an RFC1918 private IPv4 range."
    if ! ask_yes_no "Continue with this subnet?" N; then
      info "Installation canceled."
      return 0
    fi
  fi

  overlap=$(route_overlap "$VPN_SUBNET" || true)
  if [[ -n "$overlap" ]]; then
    warn "The VPN subnet overlaps an existing route: ${overlap}"
    if ! ask_yes_no "Continue with the overlapping subnet?" N; then
      info "Installation canceled. Choose a different VPN subnet."
      return 0
    fi
  fi

  while true; do
    ask_value DNS_SERVERS \
      "DNS servers pushed to clients" \
      "Comma-separated IPv4 DNS servers delivered to clients during IKEv2 configuration." \
      "1.1.1.1,8.8.8.8"
    if validate_dns_list "$DNS_SERVERS"; then
      DNS_SERVERS=$(normalize_dns_list "$DNS_SERVERS")
      break
    fi
    warn "Enter one or more valid IPv4 DNS addresses separated by commas."
  done

  while true; do
    ask_value CA_NAME \
      "Private CA name" \
      "Display name of the private Root CA that will sign the VPN server certificate." \
      "IKEv2 VPN Root CA"
    validate_ca_name "$CA_NAME" && break
    warn "Use letters, numbers, spaces, dot, underscore, and hyphen only; maximum 64 characters."
  done

  printf '\n%bWindows compatibility%b\n' "$BOLD" "$RESET"
  printf '  Stock Windows clients commonly require legacy MODP-1024 and ESP SHA-1 fallback unless their IPsec policy is changed.\n'
  if ask_yes_no "Enable stock Windows compatibility?" N; then
    ALLOW_STOCK_WINDOWS="yes"
    warn "Stock Windows compatibility enables legacy cryptographic fallback."
  else
    ALLOW_STOCK_WINDOWS="no"
  fi

  initialize_proxy_state_defaults
  printf '\n%bPrivate SOCKS5 Proxy Mode%b\n' "$BOLD" "$RESET"
  printf '  Optional v6 mode: selected applications use SOCKS5 over IKEv2 while other Windows traffic stays direct.\n'
  if ask_yes_no "Enable private SOCKS5 Proxy Mode?" Y; then
    PROXY_ENABLED="yes"
    collect_proxy_settings
  else
    PROXY_ENABLED="no"
  fi

  if package_installed strongswan-starter || [[ -s "$IPSEC_CONF" || -s "$IPSEC_SECRETS" ]]; then
    warn "An existing StrongSwan installation or configuration was detected."
    warn "This installer will back up the existing configuration and restore it during uninstall."
    if ! ask_yes_no "Continue and temporarily replace the current StrongSwan configuration?" N; then
      info "Installation canceled."
      return 0
    fi
  fi

  collect_users

  printf '\n%bInstallation summary%b\n' "$BOLD" "$RESET"
  printf '  Server ID          : %s\n' "$SERVER_ID"
  printf '  Internet interface : %s\n' "$OUT_IF"
  printf '  VPN subnet         : %s\n' "$VPN_SUBNET"
  printf '  DNS servers        : %s\n' "$DNS_SERVERS"
  printf '  CA name            : %s\n' "$CA_NAME"
  printf '  Users              : %s\n' "${#USER_NAMES[@]}"
  printf '  Windows profile    : %s\n' "$( [[ "$ALLOW_STOCK_WINDOWS" == "yes" ]] && echo 'stock-compatible' || echo 'secure' )"
  if [[ "$PROXY_ENABLED" == "yes" ]]; then
    printf '  SOCKS5 Proxy Mode  : enabled (%s:%s, VPN-only)\n' "$PROXY_IP" "$PROXY_PORT"
  else
    printf '  SOCKS5 Proxy Mode  : disabled\n'
  fi

  if ! ask_yes_no "Proceed with installation?" Y; then
    info "Installation canceled by the user."
    return 0
  fi

  CLIENT_DIR="/root/ikev2-client"
  if [[ -e "$CLIENT_DIR" ]]; then
    CLIENT_DIR="/root/ikev2-client-$(date +%Y%m%d-%H%M%S)"
  fi

  record_preinstall_state

  if service_is_active "$FW_SERVICE"; then
    systemctl stop "$FW_SERVICE" >/dev/null 2>&1 || true
  fi

  install_packages
  create_certificates
  write_ipsec_config
  write_sysctl_config
  write_firewall_config
  write_client_files
  start_and_verify
  if [[ "$PROXY_ENABLED" == "yes" ]]; then
    configure_proxy_mode
    write_state
  fi
  show_install_result
}

safe_purge_new_packages() {
  local package_string="$1"
  local -a packages=()
  local package
  local old_ifs="$IFS"

  IFS=' ' read -r -a packages <<<"$package_string"
  IFS="$old_ifs"

  ((${#packages[@]} > 0)) || return 0

  local simulation planned removed allowed extra=()
  if ! simulation=$(apt-get -s purge "${packages[@]}" 2>/dev/null); then
    warn "APT package removal simulation failed. Newly installed packages will be kept."
    return 1
  fi

  planned=$(awk '/^(Remv|Purg) / {print $2}' <<<"$simulation")
  while read -r removed; do
    [[ -n "$removed" ]] || continue
    removed=${removed%%:*}
    allowed="no"

    for package in "${packages[@]}"; do
      if [[ "$removed" == "$package" ]]; then
        allowed="yes"
        break
      fi
    done

    [[ "$allowed" == "yes" ]] || extra+=("$removed")
  done <<<"$planned"

  if ((${#extra[@]} > 0)); then
    warn "Package purge was skipped because APT also wanted to remove unrelated packages: ${extra[*]}"
    return 1
  fi

  DEBIAN_FRONTEND=noninteractive apt-get purge -y "${packages[@]}"
}

restore_sysctl_runtime() {
  sysctl -w "net.ipv4.ip_forward=${OLD_IP_FORWARD:-0}" >/dev/null || true
  sysctl -w "net.ipv4.conf.all.accept_redirects=${OLD_ACCEPT_REDIRECTS_ALL:-1}" >/dev/null || true
  sysctl -w "net.ipv4.conf.default.accept_redirects=${OLD_ACCEPT_REDIRECTS_DEFAULT:-1}" >/dev/null || true
  sysctl -w "net.ipv4.conf.all.send_redirects=${OLD_SEND_REDIRECTS_ALL:-1}" >/dev/null || true
  sysctl -w "net.ipv4.conf.default.send_redirects=${OLD_SEND_REDIRECTS_DEFAULT:-1}" >/dev/null || true
}

restore_service_state() {
  local service="$1"
  local was_enabled="$2"
  local was_active="$3"

  if ! systemctl list-unit-files "$service" >/dev/null 2>&1; then
    return 0
  fi

  if [[ "$was_enabled" == "yes" ]]; then
    systemctl enable "$service" >/dev/null 2>&1 || true
  else
    systemctl disable "$service" >/dev/null 2>&1 || true
  fi

  if [[ "$was_active" == "yes" ]]; then
    systemctl restart "$service" >/dev/null 2>&1 || true
  else
    systemctl stop "$service" >/dev/null 2>&1 || true
  fi
}

uninstall_vpn() {
  [[ -f "$STATE_FILE" ]] || die "No installation managed by this script was found."
  # shellcheck disable=SC1090
  source "$STATE_FILE"
  initialize_proxy_state_defaults

  printf '\n%bUninstall IKEv2 VPN%b\n' "$BOLD" "$RESET"
  printf '  Server ID  : %s\n' "$SERVER_ID"
  printf '  VPN subnet : %s\n' "$VPN_SUBNET"

  if ! ask_yes_no "Remove the VPN configuration, certificates, firewall rules, and client export created by this installer?" N; then
    info "Uninstall canceled."
    return 0
  fi

  if [[ "$PROXY_BASELINE_RECORDED" == "yes" ]]; then
    log "Stopping managed private SOCKS5 Proxy Mode..."
    stop_proxy_runtime
  fi

  log "Stopping managed firewall rules..."
  systemctl disable --now "$FW_SERVICE" >/dev/null 2>&1 || true
  [[ -x "$FW_SCRIPT" ]] && "$FW_SCRIPT" stop >/dev/null 2>&1 || true

  log "Stopping the managed StrongSwan configuration..."
  systemctl stop "$STRONGSWAN_SERVICE" >/dev/null 2>&1 || true

  log "Restoring files that existed before installation..."
  restore_file "$IPSEC_CONF" "ipsec.conf" "${IPSEC_CONF_EXISTED:-no}"
  restore_file "$IPSEC_SECRETS" "ipsec.secrets" "${IPSEC_SECRETS_EXISTED:-no}"
  restore_file "$SYSCTL_FILE" "99-ikev2-vpn.conf" "${SYSCTL_FILE_EXISTED:-no}"
  restore_file "$FW_SCRIPT" "ikev2-vpn-firewall" "${FW_SCRIPT_EXISTED:-no}"
  restore_file "$FW_SERVICE_FILE" "ikev2-vpn-firewall.service" "${FW_SERVICE_FILE_EXISTED:-no}"
  restore_file "$CA_KEY" "ca-key.pem" "${CA_KEY_EXISTED:-no}"
  restore_file "$CA_CERT" "ca-cert.pem" "${CA_CERT_EXISTED:-no}"
  restore_file "$SERVER_KEY" "server-key.pem" "${SERVER_KEY_EXISTED:-no}"
  restore_file "$SERVER_CERT" "server-cert.pem" "${SERVER_CERT_EXISTED:-no}"

  if [[ "$PROXY_BASELINE_RECORDED" == "yes" ]]; then
    restore_file "$PROXY_CONF" "proxy-danted.conf" "${PROXY_CONF_EXISTED:-no}"
    restore_file "$PROXY_FW_SCRIPT" "ikev2-vpn-proxy-firewall" "${PROXY_FW_SCRIPT_EXISTED:-no}"
    restore_file "$PROXY_SERVICE_FILE" "ikev2-vpn-proxy.service" "${PROXY_SERVICE_FILE_EXISTED:-no}"
    rmdir "$PROXY_CONFIG_DIR" 2>/dev/null || true
  fi

  if [[ -n "${CLIENT_DIR:-}" && -d "$CLIENT_DIR" ]]; then
    rm -rf -- "$CLIENT_DIR"
  fi

  systemctl daemon-reload
  restore_sysctl_runtime

  log "Restoring previous service states..."
  restore_service_state "$FW_SERVICE" "${FW_SERVICE_WAS_ENABLED:-no}" "${FW_SERVICE_WAS_ACTIVE:-no}"
  restore_service_state "$STRONGSWAN_SERVICE" "${STRONGSWAN_WAS_ENABLED:-no}" "${STRONGSWAN_WAS_ACTIVE:-no}"
  if [[ "$PROXY_BASELINE_RECORDED" == "yes" ]]; then
    restore_service_state "$PROXY_SERVICE" "${PROXY_SERVICE_WAS_ENABLED:-no}" "${PROXY_SERVICE_WAS_ACTIVE:-no}"
    restore_service_state "$DANTE_DEFAULT_SERVICE" "${DANTE_DEFAULT_WAS_ENABLED:-no}" "${DANTE_DEFAULT_WAS_ACTIVE:-no}"
  fi

  if [[ -n "${NEW_PACKAGES:-}" ]]; then
    log "Removing packages that were not installed before this installer ran..."
    safe_purge_new_packages "$NEW_PACKAGES" || warn "Some packages were intentionally kept because safe removal could not be confirmed."
  fi

  rm -rf "$STATE_DIR"
  log "Uninstall complete."
}


require_managed_installation() {
  [[ -f "$STATE_FILE" ]] || die "No installation managed by this script was found."
  # shellcheck disable=SC1090
  source "$STATE_FILE"
  initialize_proxy_state_defaults
}

active_ikev2_sa_count() {
  if ! command_exists ipsec; then
    printf '0\n'
    return
  fi

  ipsec statusall 2>/dev/null | grep -cE '\]:[[:space:]]+ESTABLISHED' || true
}

confirm_ikev2_interruption() {
  local action="$1"
  local active_count
  active_count=$(active_ikev2_sa_count)

  if [[ "$active_count" =~ ^[0-9]+$ ]] && (( active_count > 0 )); then
    warn "${active_count} active IKEv2 Security Association(s) were detected."
  fi

  warn "${action} will disconnect active VPN clients."
  ask_yes_no "Continue?" N
}

start_ikev2_service() {
  require_managed_installation

  if service_is_active "$STRONGSWAN_SERVICE"; then
    info "IKEv2 / StrongSwan is already running."
    return 0
  fi

  # The managed firewall/NAT service is required for normal VPN operation.
  # Start it when needed, but do not alter its boot enable/disable state here.
  if ! service_is_active "$FW_SERVICE"; then
    log "Starting the managed VPN firewall/NAT service..."
    systemctl start "$FW_SERVICE"
  fi

  log "Starting IKEv2 / StrongSwan..."
  systemctl start "$STRONGSWAN_SERVICE"

  if service_is_active "$STRONGSWAN_SERVICE"; then
    log "IKEv2 / StrongSwan is running."
  else
    systemctl --no-pager -l status "$STRONGSWAN_SERVICE" || true
    die "StrongSwan failed to start."
  fi
}

stop_ikev2_service() {
  require_managed_installation

  if ! service_is_active "$STRONGSWAN_SERVICE"; then
    info "IKEv2 / StrongSwan is already stopped."
    return 0
  fi

  confirm_ikev2_interruption "Stopping IKEv2 / StrongSwan" || {
    info "Stop canceled."
    return 0
  }

  log "Stopping IKEv2 / StrongSwan..."
  systemctl stop "$STRONGSWAN_SERVICE"
  log "IKEv2 / StrongSwan is stopped."
}

restart_ikev2_service() {
  require_managed_installation

  if service_is_active "$STRONGSWAN_SERVICE"; then
    confirm_ikev2_interruption "Restarting IKEv2 / StrongSwan" || {
      info "Restart canceled."
      return 0
    }
  fi

  if ! service_is_active "$FW_SERVICE"; then
    log "Starting the managed VPN firewall/NAT service..."
    systemctl start "$FW_SERVICE"
  fi

  log "Restarting IKEv2 / StrongSwan..."
  systemctl restart "$STRONGSWAN_SERVICE"

  if service_is_active "$STRONGSWAN_SERVICE"; then
    log "IKEv2 / StrongSwan restarted successfully."
  else
    systemctl --no-pager -l status "$STRONGSWAN_SERVICE" || true
    die "StrongSwan failed to restart."
  fi
}

proxy_mode_available() {
  [[ "${PROXY_ENABLED:-no}" == "yes" && -f "$PROXY_SERVICE_FILE" ]]
}

start_proxy_service() {
  require_managed_installation

  if ! proxy_mode_available; then
    warn "SOCKS5 Proxy Mode is not configured."
    warn "Use the upgrade/configure Proxy Mode option first."
    return 0
  fi

  if service_is_active "$PROXY_SERVICE"; then
    info "SOCKS5 Proxy Mode is already running."
    return 0
  fi

  if ! service_is_active "$STRONGSWAN_SERVICE"; then
    warn "StrongSwan is currently stopped. The proxy can start, but clients cannot reach it until IKEv2 is started."
  fi

  log "Starting SOCKS5 Proxy Mode..."
  systemctl start "$PROXY_SERVICE"

  if service_is_active "$PROXY_SERVICE"; then
    log "SOCKS5 Proxy Mode is running at ${PROXY_IP}:${PROXY_PORT}."
  else
    systemctl --no-pager -l status "$PROXY_SERVICE" || true
    die "The SOCKS5 Proxy Mode service failed to start."
  fi
}

stop_proxy_service() {
  require_managed_installation

  if ! proxy_mode_available; then
    warn "SOCKS5 Proxy Mode is not configured."
    return 0
  fi

  if ! service_is_active "$PROXY_SERVICE"; then
    info "SOCKS5 Proxy Mode is already stopped."
    return 0
  fi

  log "Stopping SOCKS5 Proxy Mode..."
  systemctl stop "$PROXY_SERVICE"
  log "SOCKS5 Proxy Mode is stopped."
}

restart_proxy_service() {
  require_managed_installation

  if ! proxy_mode_available; then
    warn "SOCKS5 Proxy Mode is not configured."
    warn "Use the upgrade/configure Proxy Mode option first."
    return 0
  fi

  log "Restarting SOCKS5 Proxy Mode..."
  systemctl restart "$PROXY_SERVICE"

  if service_is_active "$PROXY_SERVICE"; then
    log "SOCKS5 Proxy Mode restarted successfully at ${PROXY_IP}:${PROXY_PORT}."
  else
    systemctl --no-pager -l status "$PROXY_SERVICE" || true
    die "The SOCKS5 Proxy Mode service failed to restart."
  fi
}

start_all_vpn_services() {
  require_managed_installation

  if ! service_is_active "$FW_SERVICE"; then
    log "Starting VPN firewall/NAT..."
    systemctl start "$FW_SERVICE"
  fi

  if ! service_is_active "$STRONGSWAN_SERVICE"; then
    log "Starting IKEv2 / StrongSwan..."
    systemctl start "$STRONGSWAN_SERVICE"
  fi

  if proxy_mode_available && ! service_is_active "$PROXY_SERVICE"; then
    log "Starting SOCKS5 Proxy Mode..."
    systemctl start "$PROXY_SERVICE"
  fi

  log "Managed VPN services are running."
}

stop_all_vpn_services() {
  require_managed_installation

  if service_is_active "$STRONGSWAN_SERVICE"; then
    confirm_ikev2_interruption "Stopping all managed VPN services" || {
      info "Stop canceled."
      return 0
    }
  fi

  if proxy_mode_available && service_is_active "$PROXY_SERVICE"; then
    log "Stopping SOCKS5 Proxy Mode..."
    systemctl stop "$PROXY_SERVICE"
  fi

  if service_is_active "$STRONGSWAN_SERVICE"; then
    log "Stopping IKEv2 / StrongSwan..."
    systemctl stop "$STRONGSWAN_SERVICE"
  fi

  if service_is_active "$FW_SERVICE"; then
    log "Stopping VPN firewall/NAT..."
    systemctl stop "$FW_SERVICE"
  fi

  log "All managed VPN runtime services are stopped."
  info "Configuration and boot enable/disable settings were not removed or changed."
}

service_control_menu() {
  local choice=""

  while true; do
    require_managed_installation

    printf '\n%bService Control%b\n' "$BOLD" "$RESET"
    printf '  IKEv2 / StrongSwan : %s\n' "$(systemctl is-active "$STRONGSWAN_SERVICE" 2>/dev/null || true)"
    printf '  Firewall / NAT     : %s\n' "$(systemctl is-active "$FW_SERVICE" 2>/dev/null || true)"
    if proxy_mode_available; then
      printf '  SOCKS5 Proxy       : %s (%s:%s)\n' "$(systemctl is-active "$PROXY_SERVICE" 2>/dev/null || true)" "$PROXY_IP" "$PROXY_PORT"
    else
      printf '  SOCKS5 Proxy       : not configured\n'
    fi

    printf '\n'
    printf '  1) Start IKEv2\n'
    printf '  2) Stop IKEv2\n'
    printf '  3) Restart IKEv2\n'
    printf '  4) Start SOCKS5 Proxy\n'
    printf '  5) Stop SOCKS5 Proxy\n'
    printf '  6) Restart SOCKS5 Proxy\n'
    printf '  7) Start All VPN Services\n'
    printf '  8) Stop All VPN Services\n'
    printf '  9) Back\n'
    read -r -p 'Choose [1-9]: ' choice || true

    case "$choice" in
      1) start_ikev2_service ;;
      2) stop_ikev2_service ;;
      3) restart_ikev2_service ;;
      4) start_proxy_service ;;
      5) stop_proxy_service ;;
      6) restart_proxy_service ;;
      7) start_all_vpn_services ;;
      8) stop_all_vpn_services ;;
      9) return 0 ;;
      *) warn "Invalid selection." ;;
    esac
  done
}

status_vpn() {
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "Status: NOT INSTALLED by this script"
    return 0
  fi

  # shellcheck disable=SC1090
  source "$STATE_FILE"
  local installed_version="${INSTALLER_VERSION:-unknown}"
  initialize_proxy_state_defaults

  printf '%bIKEv2 installer status%b\n' "$BOLD" "$RESET"
  printf '  Installed version  : %s\n' "$installed_version"
  printf '  Script version     : %s\n' "$CURRENT_INSTALLER_VERSION"
  printf '  Server ID          : %s\n' "$SERVER_ID"
  printf '  Internet interface : %s\n' "$OUT_IF"
  printf '  VPN subnet         : %s\n' "$VPN_SUBNET"
  printf '  DNS servers        : %s\n' "$DNS_SERVERS"
  printf '  Client directory   : %s\n' "$CLIENT_DIR"
  printf '  Windows profile    : %s\n' "$( [[ "${ALLOW_STOCK_WINDOWS:-no}" == "yes" ]] && echo 'stock-compatible' || echo 'secure' )"
  printf '  StrongSwan service : %s\n' "$(systemctl is-active "$STRONGSWAN_SERVICE" 2>/dev/null || true)"
  printf '  Firewall service   : %s\n' "$(systemctl is-active "$FW_SERVICE" 2>/dev/null || true)"
  printf '  IPv4 forwarding    : %s\n' "$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo unknown)"

  if [[ "$PROXY_ENABLED" == "yes" ]]; then
    printf '  SOCKS5 Proxy Mode  : enabled\n'
    printf '  SOCKS5 endpoint    : %s:%s\n' "$PROXY_IP" "$PROXY_PORT"
    printf '  Proxy service      : %s\n' "$(systemctl is-active "$PROXY_SERVICE" 2>/dev/null || true)"
    printf '  Proxy exposure     : VPN subnet only (%s)\n' "$VPN_SUBNET"
  else
    printf '  SOCKS5 Proxy Mode  : disabled/not configured\n'
  fi

  echo
  echo "Configured users:"
  if [[ -r "$IPSEC_SECRETS" ]]; then
    awk '$2==":" && $3=="EAP" {print "  - "$1}' "$IPSEC_SECRETS" || true
  else
    echo "  Unable to read ${IPSEC_SECRETS}"
  fi

  if command_exists ipsec; then
    echo
    echo "StrongSwan status:"
    ipsec statusall 2>/dev/null | sed -n '1,80p' || true
  fi
}

interactive_menu() {
  local choice=""

  while true; do
    if [[ -f "$STATE_FILE" ]]; then
      printf '\n%bIKEv2 installation detected%b\n' "$BOLD" "$RESET"
      printf '  1) Status\n'
      printf '  2) Service Control\n'
      printf '  3) Upgrade / Configure SOCKS5 Proxy Mode\n'
      printf '  4) Uninstall\n'
      printf '  5) Exit\n'
      read -r -p 'Choose [1-5]: ' choice || true

      case "$choice" in
        1)
          status_vpn
          pause_main_menu
          ;;
        2)
          service_control_menu
          ;;
        3)
          upgrade_vpn
          pause_main_menu
          ;;
        4)
          uninstall_vpn
          pause_main_menu
          ;;
        5)
          printf '\nExiting...\n'
          exit 0
          ;;
        *)
          warn "Invalid selection."
          ;;
      esac
    else
      printf '\n%bIKEv2 / StrongSwan Server v6%b\n' "$BOLD" "$RESET"
      printf '  1) Install\n'
      printf '  2) Exit\n'
      read -r -p 'Choose [1-2]: ' choice || true

      case "$choice" in
        1)
          install_vpn
          pause_main_menu
          ;;
        2)
          printf '\nExiting...\n'
          exit 0
          ;;
        *)
          warn "Invalid selection."
          ;;
      esac
    fi
  done
}

usage() {
  cat <<EOF
Usage: $0 [install|upgrade|status|start|stop|restart|proxy-start|proxy-stop|proxy-restart|start-all|stop-all|uninstall]

Commands:
  install        Full interactive IKEv2 installation; all previous features are retained.
  upgrade        Add or update private SOCKS5 Proxy Mode on an existing managed installation.
  status         Show VPN, StrongSwan, firewall, and Proxy Mode status.
  start          Start IKEv2 / StrongSwan (and the managed firewall/NAT if needed).
  stop           Stop IKEv2 / StrongSwan after confirmation.
  restart        Restart IKEv2 / StrongSwan after confirmation when active.
  proxy-start    Start the private SOCKS5 Proxy Mode service.
  proxy-stop     Stop the private SOCKS5 Proxy Mode service.
  proxy-restart  Restart the private SOCKS5 Proxy Mode service.
  start-all      Start firewall/NAT, IKEv2, and configured Proxy Mode.
  stop-all       Stop Proxy Mode, IKEv2, and firewall/NAT after confirmation.
  uninstall      Remove managed configuration and restore previous files/services.

The upgrade command does not regenerate certificates, rewrite users, replace ipsec.conf,
restart StrongSwan, or modify the original full-tunnel NAT/firewall service.

Run without arguments to open the interactive menu.
EOF
}

main() {
  require_root
  check_ubuntu

  case "${1:-}" in
    install) install_vpn ;;
    upgrade) upgrade_vpn ;;
    status) status_vpn ;;
    start) start_ikev2_service ;;
    stop) stop_ikev2_service ;;
    restart) restart_ikev2_service ;;
    proxy-start) start_proxy_service ;;
    proxy-stop) stop_proxy_service ;;
    proxy-restart) restart_proxy_service ;;
    start-all) start_all_vpn_services ;;
    stop-all) stop_all_vpn_services ;;
    uninstall) uninstall_vpn ;;
    -h|--help) usage ;;
    "") interactive_menu ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
