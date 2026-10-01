#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="IKEv2 Linux VPN Utility"
APP_VERSION="1.9.4"
STATE_DIR="/etc/ikev2-client-utility"
META_DIR="$STATE_DIR/profiles"
CONF_DIR="/etc/ipsec.d/ikev2-client-profiles"
SECRETS_DIR="/etc/ipsec.d/ikev2-client-secrets"
CACERT_DIR="/etc/ipsec.d/cacerts"
SYSTEM_CA="/usr/local/share/ca-certificates/ikev2-client-ca.crt"
SYSTEM_INSTALL_DIR="/opt/ikev2-client"
SYSTEM_SCRIPT="$SYSTEM_INSTALL_DIR/ikev2-client.sh"
SYSTEM_CERT="$SYSTEM_INSTALL_DIR/ca-cert.cer"
SYSTEM_COMMAND="/usr/local/bin/ikev2"
AUTORECONNECT_UNIT_PREFIX="ikev2-client-autoreconnect-"
DNS_STATE_DIR="$STATE_DIR/dns-state"
DNS_GLOBAL_BACKUP="$STATE_DIR/resolv.conf.pre-vpn"
DNS_OWNER_FILE="$STATE_DIR/resolv.conf.owner"
STRONGSWAN_RESOLVE_OVERRIDE="/etc/strongswan.d/charon/zz-ikev2-client-resolve.conf"
RESOLVCONF_NOOP="/usr/local/libexec/ikev2-client-resolvconf-noop"
GATEWAY_MODE_STATE_FILE="$STATE_DIR/gateway-mode.state"
GATEWAY_RULE_COMMENT="ikev2-client-gateway-mode"
GATEWAY_PRIVATE_CIDRS=("10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16")
GATEWAY_LAN_IFACE="${GATEWAY_LAN_IFACE:-}"
GATEWAY_LAN_GATEWAY="${GATEWAY_LAN_GATEWAY:-}"
GATEWAY_REMOTE_VPN_SERVER="${GATEWAY_REMOTE_VPN_SERVER:-}"
GATEWAY_LEGACY_LAN_SUBNET=""
GATEWAY_VPN_VIRTUAL_IP="${GATEWAY_VPN_VIRTUAL_IP:-10.10.10.1}"
GATEWAY_ROUTE_TABLE="${GATEWAY_ROUTE_TABLE:-main}"
GATEWAY_ROUTE_IFACE="${GATEWAY_ROUTE_IFACE:-}"
DNS_MODE="managed"
STRONGSWAN_DNS_CONFIG_CHANGED=0
DNS_CONFIG_MARKER="$STATE_DIR/managed-dns-config.sha256"
PENDING_DNS_CONFIG_HASH=""
IPSEC_CONF="/etc/ipsec.conf"
IPSEC_SECRETS="/etc/ipsec.secrets"
CONF_INCLUDE="include $CONF_DIR/*.conf"
SECRETS_INCLUDE="include $SECRETS_DIR/*.secrets"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '[i] %s\n' "$*"; }
step() { printf '[+] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die() { warn "$*"; exit 1; }

show_about() {
    printf '\nIKEv2 Linux VPN Utility v%s configures and manages Ubuntu StrongSwan client profiles with EAP authentication, optional IPv6 full-tunnel routing, and VPN-aware DNS handling.\n\n' "$APP_VERSION"
}

gateway_mode_enabled() {
    [[ -f "$GATEWAY_MODE_STATE_FILE" ]]
}

gateway_write_state() {
    cat > "$GATEWAY_MODE_STATE_FILE" <<EOF
GATEWAY_LAN_IFACE=$GATEWAY_LAN_IFACE
GATEWAY_LAN_GATEWAY=$GATEWAY_LAN_GATEWAY
GATEWAY_REMOTE_VPN_SERVER=$GATEWAY_REMOTE_VPN_SERVER
GATEWAY_VPN_VIRTUAL_IP=$GATEWAY_VPN_VIRTUAL_IP
GATEWAY_ROUTE_TABLE=$GATEWAY_ROUTE_TABLE
GATEWAY_ROUTE_IFACE=$GATEWAY_ROUTE_IFACE
EOF
    chmod 0600 "$GATEWAY_MODE_STATE_FILE"
}

gateway_load_state() {
    [[ -f "$GATEWAY_MODE_STATE_FILE" ]] || return 0

    local key value
    while IFS='=' read -r key value; do
        case "$key" in
            GATEWAY_LAN_IFACE) GATEWAY_LAN_IFACE="$value" ;;
            GATEWAY_LAN_GATEWAY) GATEWAY_LAN_GATEWAY="$value" ;;
            GATEWAY_REMOTE_VPN_SERVER) GATEWAY_REMOTE_VPN_SERVER="$value" ;;
            GATEWAY_LAN_SUBNET) GATEWAY_LEGACY_LAN_SUBNET="$value" ;;
            GATEWAY_VPN_VIRTUAL_IP) GATEWAY_VPN_VIRTUAL_IP="$value" ;;
            GATEWAY_ROUTE_TABLE) GATEWAY_ROUTE_TABLE="$value" ;;
            GATEWAY_ROUTE_IFACE) GATEWAY_ROUTE_IFACE="$value" ;;
        esac
    done < "$GATEWAY_MODE_STATE_FILE"
}

gateway_resolve_remote_server() {
    local profile_file server
    local -a connected_servers=()

    if [[ -z "$GATEWAY_REMOTE_VPN_SERVER" ]]; then
        while IFS= read -r profile_file; do
            [[ -n "$profile_file" ]] || continue
            read_metadata "$profile_file"
            if is_connected "$META_ID"; then
                connected_servers+=("$META_SERVER")
            fi
        done < <(list_profile_files)

        [[ ${#connected_servers[@]} -eq 1 ]] || {
            warn "Connect exactly one managed VPN profile, or set GATEWAY_REMOTE_VPN_SERVER to its IPv4 endpoint."
            return 1
        }
        server="${connected_servers[0]}"
        if [[ "$server" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            GATEWAY_REMOTE_VPN_SERVER="$server"
        else
            GATEWAY_REMOTE_VPN_SERVER="$(getent ahostsv4 "$server" 2>/dev/null | awk 'NR==1 {print $1}')"
        fi
    fi

    [[ "$GATEWAY_REMOTE_VPN_SERVER" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
        warn "The VPN endpoint must resolve to an IPv4 address: $GATEWAY_REMOTE_VPN_SERVER"
        return 1
    }
}

gateway_validate_config() {
    command -v ip >/dev/null 2>&1 || die "Gateway Mode requires the ip command."
    command -v iptables >/dev/null 2>&1 || die "Gateway Mode requires iptables."

    if [[ -z "$GATEWAY_LAN_IFACE" ]]; then
        GATEWAY_LAN_IFACE="$(ip -4 route show default | awk 'NR==1 {for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')"
    fi
    [[ -n "$GATEWAY_LAN_IFACE" ]] || die "Set GATEWAY_LAN_IFACE to the interface that receives downstream traffic."

    [[ "$GATEWAY_VPN_VIRTUAL_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Gateway Mode virtual IP is invalid: $GATEWAY_VPN_VIRTUAL_IP"
    [[ "$GATEWAY_REMOTE_VPN_SERVER" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Gateway Mode remote VPN server is invalid: $GATEWAY_REMOTE_VPN_SERVER"

    ip link show dev "$GATEWAY_LAN_IFACE" >/dev/null 2>&1 || die "Gateway Mode LAN interface '$GATEWAY_LAN_IFACE' is not present."
    ip -4 -o addr show dev "$GATEWAY_LAN_IFACE" scope global 2>/dev/null | awk 'NR==1 {split($4,a,"/"); print a[1]}' | grep -q '.' || \
        die "Gateway Mode LAN interface '$GATEWAY_LAN_IFACE' does not have an IPv4 address."

    ip route show default dev "$GATEWAY_LAN_IFACE" >/dev/null 2>&1 || warn "No default route is currently attached to '$GATEWAY_LAN_IFACE'. Gateway Mode still uses the configured gateway: $GATEWAY_LAN_GATEWAY"
}

gateway_route_to_vpn_endpoint() {
    local route_info route_dev route_gateway lan_ip
    route_info="$(ip -4 route get "$GATEWAY_REMOTE_VPN_SERVER" 2>&1)" || {
        warn "Could not look up a route to VPN endpoint $GATEWAY_REMOTE_VPN_SERVER: $route_info"
        return 1
    }
    route_dev="$(awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}' <<< "$route_info")"
    route_gateway="$GATEWAY_LAN_GATEWAY"
    if [[ -z "$route_gateway" ]]; then
        route_gateway="$(awk '{for (i=1; i<=NF; i++) if ($i == "via") {print $(i+1); exit}}' <<< "$route_info")"
    fi
    GATEWAY_ROUTE_TABLE="$(awk '{for (i=1; i<=NF; i++) if ($i == "table") {print $(i+1); exit}}' <<< "$route_info")"
    GATEWAY_ROUTE_TABLE="${GATEWAY_ROUTE_TABLE:-main}"
    GATEWAY_ROUTE_IFACE="$route_dev"

    [[ -n "$route_dev" ]] || {
        warn "The route lookup for VPN endpoint $GATEWAY_REMOTE_VPN_SERVER did not return an interface: $route_info"
        return 1
    }
    lan_ip="$(ip -4 -o addr show dev "$route_dev" scope global 2>/dev/null | awk 'NR==1 {split($4,a,"/"); print a[1]}')"
    [[ -n "$lan_ip" ]] || {
        warn "No global IPv4 address was found on VPN route interface '$route_dev'."
        return 1
    }

    local -a route_args=(replace "${GATEWAY_REMOTE_VPN_SERVER}/32")
    [[ -n "$route_gateway" ]] && route_args+=(via "$route_gateway")
    route_args+=(dev "$route_dev" src "$lan_ip" table "$GATEWAY_ROUTE_TABLE")
    if ! ip route "${route_args[@]}"; then
        warn "Failed to install the VPN endpoint route using table '$GATEWAY_ROUTE_TABLE' and interface '$route_dev'."
        return 1
    fi
    GATEWAY_LAN_GATEWAY="$route_gateway"
}

gateway_enable() {
    local private_cidr

    ensure_directories
    gateway_load_state
    gateway_resolve_remote_server || die "Gateway Mode could not determine the active VPN endpoint."
    gateway_validate_config

    if gateway_mode_enabled; then
        if [[ -n "$GATEWAY_LEGACY_LAN_SUBNET" ]]; then
            info "Replacing the saved single-subnet Gateway Mode rules with RFC1918 private ranges."
            gateway_disable
        else
            info "Gateway Mode is already enabled."
            gateway_status
            return 0
        fi
    fi

    gateway_route_to_vpn_endpoint || die "The host route to the VPN endpoint could not be configured. Check the LAN interface and gateway values."

    sysctl -w net.ipv4.ip_forward=1 >/dev/null

    for private_cidr in "${GATEWAY_PRIVATE_CIDRS[@]}"; do
        iptables -C FORWARD -i "$GATEWAY_LAN_IFACE" -s "$private_cidr" -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT >/dev/null 2>&1 || \
            iptables -I FORWARD 1 -i "$GATEWAY_LAN_IFACE" -s "$private_cidr" -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT

        iptables -C FORWARD -o "$GATEWAY_LAN_IFACE" -d "$private_cidr" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT >/dev/null 2>&1 || \
            iptables -I FORWARD 2 -o "$GATEWAY_LAN_IFACE" -d "$private_cidr" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT

        iptables -t nat -C POSTROUTING -s "$private_cidr" -m policy --dir out --pol ipsec -m comment --comment "$GATEWAY_RULE_COMMENT" -j SNAT --to-source "$GATEWAY_VPN_VIRTUAL_IP" >/dev/null 2>&1 || \
            iptables -t nat -A POSTROUTING -s "$private_cidr" -m policy --dir out --pol ipsec -m comment --comment "$GATEWAY_RULE_COMMENT" -j SNAT --to-source "$GATEWAY_VPN_VIRTUAL_IP"
    done

    gateway_write_state
    info "Gateway Mode enabled for RFC1918 private sources and SNATed to $GATEWAY_VPN_VIRTUAL_IP when sent through IPsec."
    gateway_status
}

gateway_disable() {
    local private_cidr

    gateway_load_state
    if ! gateway_mode_enabled; then
        info "Gateway Mode is already disabled."
        return 0
    fi

    for private_cidr in "${GATEWAY_PRIVATE_CIDRS[@]}"; do
        iptables -D FORWARD -i "$GATEWAY_LAN_IFACE" -s "$private_cidr" -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT >/dev/null 2>&1 || true
        iptables -D FORWARD -o "$GATEWAY_LAN_IFACE" -d "$private_cidr" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT >/dev/null 2>&1 || true
        iptables -t nat -D POSTROUTING -s "$private_cidr" -m policy --dir out --pol ipsec -m comment --comment "$GATEWAY_RULE_COMMENT" -j SNAT --to-source "$GATEWAY_VPN_VIRTUAL_IP" >/dev/null 2>&1 || true
    done

    if [[ -n "$GATEWAY_LEGACY_LAN_SUBNET" ]]; then
        iptables -D FORWARD -i "$GATEWAY_LAN_IFACE" -s "$GATEWAY_LEGACY_LAN_SUBNET" -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT >/dev/null 2>&1 || true
        iptables -D FORWARD -o "$GATEWAY_LAN_IFACE" -d "$GATEWAY_LEGACY_LAN_SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT >/dev/null 2>&1 || true
        iptables -t nat -D POSTROUTING -s "$GATEWAY_LEGACY_LAN_SUBNET" -m policy --dir out --pol ipsec -m comment --comment "$GATEWAY_RULE_COMMENT" -j SNAT --to-source "$GATEWAY_VPN_VIRTUAL_IP" >/dev/null 2>&1 || true
    fi
    ip route del "${GATEWAY_REMOTE_VPN_SERVER}/32" table "$GATEWAY_ROUTE_TABLE" >/dev/null 2>&1 || true

    rm -f "$GATEWAY_MODE_STATE_FILE"
    GATEWAY_LEGACY_LAN_SUBNET=""
    info "Gateway Mode disabled. The previous default LAN path and tunnel policy were left in place."
    gateway_status
}

gateway_status() {
    local enabled="disabled"
    local forwarding="unknown"

    gateway_load_state

    if gateway_mode_enabled; then
        enabled="enabled"
    fi

    forwarding="$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo unknown)"

    printf '\nGateway / Router Mode\n'
    printf '====================\n'
    printf 'Mode              : %s\n' "$enabled"
    printf 'IPv4 forwarding   : %s\n' "$forwarding"
    printf 'LAN interface     : %s\n' "$GATEWAY_LAN_IFACE"
    if [[ -n "$GATEWAY_LEGACY_LAN_SUBNET" ]]; then
        printf 'Source ranges     : legacy %s (press Enable to migrate)\n' "$GATEWAY_LEGACY_LAN_SUBNET"
    else
        printf 'Source ranges     : RFC1918 private IPv4\n'
    fi
    printf 'LAN gateway       : %s\n' "$GATEWAY_LAN_GATEWAY"
    printf 'VPN virtual IP    : %s\n' "$GATEWAY_VPN_VIRTUAL_IP"
    printf 'Remote VPN server : %s\n' "$GATEWAY_REMOTE_VPN_SERVER"
    printf 'Route table       : %s\n' "$GATEWAY_ROUTE_TABLE"
    printf '\nRelevant routes:\n'
    ip route show table "$GATEWAY_ROUTE_TABLE" 2>/dev/null | awk -v endpoint="$GATEWAY_REMOTE_VPN_SERVER" '$1 == "default" || $1 == endpoint || $1 == endpoint "/32" { print; found = 1 } END { if (!found) exit 1 }' || echo "  none visible"
    printf '\nRelevant iptables filter rules:\n'
    iptables -S 2>/dev/null | grep "$GATEWAY_RULE_COMMENT" || echo "  none"
    printf '\nRelevant iptables NAT rules:\n'
    iptables -t nat -S 2>/dev/null | grep "$GATEWAY_RULE_COMMENT" || echo "  none"
    printf '\nRelevant XFRM policy:\n'
    ip xfrm policy 2>/dev/null | sed -n '1,20p' || echo "  XFRM policy unavailable"
}

gateway_diagnostic_check() {
    local label="$1"
    shift

    if "$@"; then
        printf '[OK]   %s\n' "$label"
    else
        printf '[FAIL] %s\n' "$label"
        GATEWAY_DIAGNOSTIC_FAILURES=$((GATEWAY_DIAGNOSTIC_FAILURES + 1))
    fi
}

gateway_diagnostic_interface() {
    [[ -n "$GATEWAY_LAN_IFACE" ]] || return 1
    ip link show dev "$GATEWAY_LAN_IFACE" >/dev/null 2>&1 || return 1
    ip -4 -o addr show dev "$GATEWAY_LAN_IFACE" scope global 2>/dev/null |
        awk 'NR == 1 { found = 1 } END { exit !found }'
}

gateway_diagnostic_forwarding() {
    [[ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null || true)" == "1" ]]
}

gateway_diagnostic_connected_profile() {
    local profile_file
    local connected_count=0

    while IFS= read -r profile_file; do
        [[ -n "$profile_file" ]] || continue
        read_metadata "$profile_file"
        if is_connected "$META_ID"; then
            connected_count=$((connected_count + 1))
        fi
    done < <(list_profile_files)

    [[ "$connected_count" -eq 1 ]]
}

gateway_diagnostic_endpoint_route() {
    local route
    route="$(ip -4 route show table "$GATEWAY_ROUTE_TABLE" 2>/dev/null |
        awk -v endpoint="$GATEWAY_REMOTE_VPN_SERVER" '$1 == endpoint || $1 == endpoint "/32" { print; exit }')"
    [[ -n "$route" && "$route" == *"dev $GATEWAY_ROUTE_IFACE"* ]] || return 1
    [[ -z "$GATEWAY_LAN_GATEWAY" || "$route" == *"via $GATEWAY_LAN_GATEWAY"* ]]
}

gateway_diagnostic_policy_rule() {
    ip -4 rule show 2>/dev/null |
        grep -Eq "lookup[[:space:]]+${GATEWAY_ROUTE_TABLE}([[:space:]]|$)"
}

gateway_diagnostic_xfrm_policy() {
    ip -4 xfrm policy 2>/dev/null | awk -v virtual_ip="${GATEWAY_VPN_VIRTUAL_IP}/32" '
        /^src / { source_matches = ($2 == virtual_ip) }
        /dir out/ && source_matches { found = 1 }
        END { exit !found }
    '
}

gateway_diagnostic_xfrm_state() {
    ip -4 xfrm state 2>/dev/null |
        awk -v endpoint="$GATEWAY_REMOTE_VPN_SERVER" '$1 == "src" && $4 == endpoint { found = 1 } END { exit !found }'
}

gateway_diagnostics() {
    local private_cidr

    GATEWAY_DIAGNOSTIC_FAILURES=0
    gateway_load_state
    if [[ -z "$GATEWAY_LAN_IFACE" ]]; then
        GATEWAY_LAN_IFACE="$(ip -4 route show default 2>/dev/null | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }')"
    fi

    printf '\nGateway / Router Diagnostics (read-only)\n'
    printf '========================================\n'
    gateway_diagnostic_check "Gateway Mode is enabled" gateway_mode_enabled
    gateway_diagnostic_check "Required commands are installed (ip, iptables, ipsec, sysctl)" sh -c 'command -v ip >/dev/null && command -v iptables >/dev/null && command -v ipsec >/dev/null && command -v sysctl >/dev/null'
    gateway_diagnostic_check "Exactly one managed VPN profile is connected" gateway_diagnostic_connected_profile
    if ! gateway_diagnostic_check "VPN endpoint resolves to IPv4" gateway_resolve_remote_server; then
        GATEWAY_REMOTE_VPN_SERVER=""
    fi
    gateway_diagnostic_check "LAN interface '$GATEWAY_LAN_IFACE' exists and has IPv4" gateway_diagnostic_interface
    gateway_diagnostic_check "IPv4 forwarding is enabled" gateway_diagnostic_forwarding
    gateway_diagnostic_check "Policy rule selects routing table '$GATEWAY_ROUTE_TABLE'" gateway_diagnostic_policy_rule
    gateway_diagnostic_check "Protected VPN endpoint route uses the LAN next hop" gateway_diagnostic_endpoint_route
    gateway_diagnostic_check "Outbound XFRM policy matches VPN IP $GATEWAY_VPN_VIRTUAL_IP" gateway_diagnostic_xfrm_policy
    gateway_diagnostic_check "XFRM state exists for VPN endpoint $GATEWAY_REMOTE_VPN_SERVER" gateway_diagnostic_xfrm_state

    if [[ -n "$GATEWAY_LEGACY_LAN_SUBNET" ]]; then
        printf '[WARN] Saved Gateway Mode uses legacy source range %s; select Enable to migrate it.\n' "$GATEWAY_LEGACY_LAN_SUBNET"
    fi

    for private_cidr in "${GATEWAY_PRIVATE_CIDRS[@]}"; do
        gateway_diagnostic_check "FORWARD permits source $private_cidr" \
            iptables -C FORWARD -i "$GATEWAY_LAN_IFACE" -s "$private_cidr" -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT
        gateway_diagnostic_check "FORWARD permits return traffic to $private_cidr" \
            iptables -C FORWARD -o "$GATEWAY_LAN_IFACE" -d "$private_cidr" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$GATEWAY_RULE_COMMENT" -j ACCEPT
        gateway_diagnostic_check "IPsec SNAT exists for $private_cidr" \
            iptables -t nat -C POSTROUTING -s "$private_cidr" -m policy --dir out --pol ipsec -m comment --comment "$GATEWAY_RULE_COMMENT" -j SNAT --to-source "$GATEWAY_VPN_VIRTUAL_IP"
    done

    printf '\n[INFO] This checks the VPN gateway itself; it cannot verify routes configured on downstream hosts or routers.\n'
    if (( GATEWAY_DIAGNOSTIC_FAILURES > 0 )); then
        printf '[FAIL] %s local check(s) failed.\n' "$GATEWAY_DIAGNOSTIC_FAILURES"
        return 1
    fi

    printf '[OK] All local Gateway Mode checks passed. Generate traffic from a downstream host to test forwarding end-to-end.\n'
}

pause_menu() {
    printf '\n'
    read -r -p "Press Enter to continue" _
}

require_root() {
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
        return
    fi

    command -v sudo >/dev/null 2>&1 || die "This utility requires root privileges and sudo is not available."
    exec sudo -- "$0" "$@"
}

detect_ubuntu() {
    [[ -r /etc/os-release ]] || die "Cannot detect the operating system."
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] || die "This utility currently supports Ubuntu only."

    case "${VERSION_ID:-}" in
        22.04|24.04) ;;
        *) warn "Ubuntu ${VERSION_ID:-unknown} detected. The utility is designed for Ubuntu 22.04 and 24.04." ;;
    esac
}

ensure_directories() {
    install -d -m 0700 "$STATE_DIR" "$META_DIR" "$SECRETS_DIR" "$DNS_STATE_DIR"
    install -d -m 0755 "$CONF_DIR" "$CACERT_DIR"
}

find_ca_certificate() {
    local preferred="$SCRIPT_DIR/ca-cert.cer"
    local -a certs=()

    if [[ -f "$preferred" ]]; then
        printf '%s\n' "$preferred"
        return
    fi

    shopt -s nullglob
    certs=("$SCRIPT_DIR"/*.cer)
    shopt -u nullglob

    case "${#certs[@]}" in
        0) die "No .cer certificate was found next to this script." ;;
        1) printf '%s\n' "${certs[0]}" ;;
        *) die "More than one .cer file was found. Keep only the VPN CA certificate next to this script, or name it ca-cert.cer." ;;
    esac
}

install_system_wide() {
    local source_script source_cert
    source_script="$(readlink -f -- "${BASH_SOURCE[0]}")"
    source_cert="$(find_ca_certificate)"

    step "Installing the IKEv2 utility system-wide..."
    install -d -m 0755 "$SYSTEM_INSTALL_DIR"

    if [[ "$source_script" != "$SYSTEM_SCRIPT" ]]; then
        install -m 0755 "$source_script" "$SYSTEM_SCRIPT"
    else
        chmod 0755 "$SYSTEM_SCRIPT"
    fi

    if [[ "$source_cert" != "$SYSTEM_CERT" ]]; then
        install -m 0644 "$source_cert" "$SYSTEM_CERT"
    else
        chmod 0644 "$SYSTEM_CERT"
    fi

    cat > "$SYSTEM_COMMAND" <<'EOF'
#!/bin/sh
exec /opt/ikev2-client/ikev2-client.sh "$@"
EOF
    chmod 0755 "$SYSTEM_COMMAND"

    printf '\nSystem-wide installation complete.\n'
    printf '  Command : ikev2\n'
    printf '  Script  : %s\n' "$SYSTEM_SCRIPT"
    printf '  CA cert : %s\n' "$SYSTEM_CERT"
}

prompt_system_wide_install() {
    local answer

    printf '\n'
    read -r -p "Install this utility system-wide as the 'ikev2' command? [y/N]: " answer

    if [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        install_system_wide
    else
        info "System-wide installation skipped."
    fi
}

install_packages() {
    if command -v ipsec >/dev/null 2>&1; then
        if ! ipsec --version 2>/dev/null | grep -qi strongSwan; then
            die "An ipsec command is installed, but it is not strongSwan. Remove the conflicting IPsec implementation first."
        fi
    fi

    if dpkg-query -W -f='${Status}' charon-systemd 2>/dev/null | grep -q "install ok installed"; then
        if ! dpkg-query -W -f='${Status}' strongswan-starter 2>/dev/null | grep -q "install ok installed"; then
            die "charon-systemd/swanctl is already installed. This utility uses strongSwan starter/ipsec.conf and will not replace an existing swanctl stack."
        fi
    fi

    local -a packages=(
        strongswan
        strongswan-starter
        strongswan-charon
        strongswan-libcharon
        libstrongswan-standard-plugins
        libstrongswan-extra-plugins
        libcharon-extauth-plugins
        libcharon-extra-plugins
        openssl
        ca-certificates
        python3-minimal
        xxd
    )

    local -a missing=()
    local pkg
    for pkg in "${packages[@]}"; do
        if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            missing+=("$pkg")
        fi
    done

    if [[ ${#missing[@]} -eq 0 ]]; then
        info "Required strongSwan packages are already installed."
        return
    fi

    step "Installing required strongSwan packages..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "${missing[@]}"
}

ensure_include_line() {
    local file="$1"
    local line="$2"
    local mode="$3"

    [[ -e "$file" ]] || install -m "$mode" /dev/null "$file"

    if ! grep -Fqx "$line" "$file"; then
        printf '\n%s\n' "$line" >> "$file"
    fi

    chmod "$mode" "$file"
}

install_ca_certificate() {
    local source
    source="$(find_ca_certificate)"

    ensure_directories

    local temp
    temp="$(mktemp)"
    trap 'rm -f "$temp"' RETURN

    if openssl x509 -in "$source" -noout >/dev/null 2>&1; then
        openssl x509 -in "$source" -out "$temp" -outform PEM
    elif openssl x509 -inform DER -in "$source" -noout >/dev/null 2>&1; then
        openssl x509 -inform DER -in "$source" -out "$temp" -outform PEM
    else
        die "The .cer file is not a readable X.509 certificate."
    fi

    if ! openssl x509 -in "$temp" -noout -text | grep -q "CA:TRUE"; then
        die "The certificate next to this script is not a CA certificate."
    fi

    step "Installing the VPN CA certificate for strongSwan..."
    install -m 0644 "$temp" "$CACERT_DIR/ikev2-client-ca.pem"

    step "Adding the VPN CA certificate to the Ubuntu system trust store..."
    install -m 0644 "$temp" "$SYSTEM_CA"
    update-ca-certificates >/dev/null

    local fingerprint
    fingerprint="$(openssl x509 -in "$temp" -noout -fingerprint -sha256 | cut -d= -f2)"
    INSTALLED_CA_SUBJECT="$(openssl x509 -in "$temp" -noout -subject -nameopt RFC2253 | sed 's/^subject=//')"
    info "Trusted CA SHA-256 fingerprint: $fingerprint"
    info "Trusted CA subject: $INSTALLED_CA_SUBJECT"

    trap - RETURN
    rm -f "$temp"
}

restart_or_reload_strongswan() {
    local dns_restart_required="${STRONGSWAN_DNS_CONFIG_CHANGED:-0}"

    if ! systemctl is-active --quiet strongswan-starter.service 2>/dev/null; then
        step "Starting strongSwan..."
        systemctl enable --now strongswan-starter.service
        mark_dns_config_applied
    elif [[ "$dns_restart_required" -eq 1 ]]; then
        step "Restarting strongSwan to apply DNS integration settings..."
        systemctl restart strongswan-starter.service
        mark_dns_config_applied
    else
        step "Reloading strongSwan configuration..."
        if ! ipsec reload >/dev/null 2>&1; then
            systemctl restart strongswan-starter.service
        fi
    fi

    STRONGSWAN_DNS_CONFIG_CHANGED=0

    step "Reloading strongSwan certificates and secrets..."
    if ! ipsec rereadall >/dev/null 2>&1; then
        ipsec rereadcacerts >/dev/null 2>&1 || true
        ipsec rereadsecrets >/dev/null 2>&1 || true
    fi
}

verify_ca_loaded() {
    local expected_subject="$1"
    local loaded

    loaded="$(ipsec listcacerts 2>/dev/null || true)"
    [[ -n "$loaded" ]] || return 1

    grep -Fq "$expected_subject" <<< "$loaded"
}

profile_id_from_name() {
    local name="$1"
    printf 'ikev2c_%s\n' "$(printf '%s' "$name" | sha256sum | awk '{print substr($1,1,12)}')"
}

validate_profile_name() {
    local value="$1"
    [[ -n "$value" ]] || return 1
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]]
}

validate_server() {
    local value="$1"
    [[ "$value" =~ ^[A-Za-z0-9._:-]+$ ]]
}

validate_username() {
    local value="$1"
    [[ "$value" =~ ^[A-Za-z0-9._@+-]+$ ]]
}

metadata_path() {
    printf '%s/%s.meta\n' "$META_DIR" "$1"
}

write_metadata() {
    local id="$1" name="$2" server="$3" username="$4"
    printf '%s\t%s\t%s\t%s\n' "$id" "$name" "$server" "$username" > "$(metadata_path "$id")"
    chmod 0600 "$(metadata_path "$id")"
}

read_metadata() {
    local file="$1"
    IFS=$'\t' read -r META_ID META_NAME META_SERVER META_USER < "$file"
}

list_profile_files() {
    shopt -s nullglob
    local -a files=("$META_DIR"/*.meta)
    shopt -u nullglob
    printf '%s\n' "${files[@]}"
}

is_connected() {
    local id="$1"
    ipsec status "$id" 2>/dev/null | grep -qE 'ESTABLISHED|INSTALLED'
}

profile_status_word() {
    local id="$1"
    if is_connected "$id"; then
        printf 'Connected'
    else
        printf 'Disconnected'
    fi
}

select_profile() {
    local action="$1"
    ensure_directories

    local -a files=()
    local file

    while IFS= read -r file; do
        [[ -n "$file" ]] && files+=("$file")
    done < <(list_profile_files)

    if [[ ${#files[@]} -eq 0 ]]; then
        warn "No IKEv2 profiles created by this utility were found."
        return 1
    fi

    printf '\nAvailable IKEv2 VPN profiles\n'
    printf '============================\n\n'

    local i=0
    for file in "${files[@]}"; do
        read_metadata "$file"
        ((i+=1))
        printf '%d) %s\n' "$i" "$META_NAME"
        printf '   Server : %s\n' "$META_SERVER"
        printf '   User   : %s\n' "$META_USER"
        printf '   Status : %s\n\n' "$(profile_status_word "$META_ID")"
    done

    printf '0) Cancel\n\n'

    local choice
    while true; do
        read -r -p "Choose a VPN profile to $action [0-${#files[@]}]: " choice
        [[ "$choice" == "0" ]] && return 1
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#files[@]} )); then
            SELECTED_META="${files[choice-1]}"
            read_metadata "$SELECTED_META"
            return 0
        fi
        warn "Invalid selection."
    done
}

escape_ipsec_value() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '"%s"' "$value"
}

write_strongswan_profile() {
    local id="$1" name="$2" server="$3" remote_id="$4" username="$5" password="$6" ipv6_enabled="${7:-no}"
    local q_user q_server q_remote_id password_hex right_subnets="0.0.0.0/0"

    if [[ "$ipv6_enabled" == "yes" ]]; then
        right_subnets+=",::/0"
    fi

    q_user="$(escape_ipsec_value "$username")"
    q_server="$(escape_ipsec_value "$server")"
    q_remote_id="$(escape_ipsec_value "$remote_id")"
    password_hex="$(printf '%s' "$password" | od -An -tx1 | tr -d '[:space:]')"
    unset password

    step "Writing the strongSwan IKEv2 profile..."

    cat > "$CONF_DIR/$id.conf" <<EOF
conn $id
    keyexchange=ikev2
    type=tunnel
    auto=add
    left=%defaultroute
    leftauth=eap-mschapv2
    leftsourceip=%config
    eap_identity=$q_user
    right=$q_server
    rightid=$q_remote_id
    rightauth=pubkey
    rightsubnet=$right_subnets
    ike=aes256-sha256-modp2048!
    esp=aes256-sha256!
    fragmentation=yes
    mobike=yes
    dpdaction=clear
    dpddelay=30s
EOF

    chmod 0644 "$CONF_DIR/$id.conf"

    printf '%s : EAP 0x%s\n' "$username" "$password_hex" > "$SECRETS_DIR/$id.secrets"
    chmod 0600 "$SECRETS_DIR/$id.secrets"
    unset password_hex

    write_metadata "$id" "$name" "$server" "$username"
}

systemd_resolved_usable() {
    systemctl is-active --quiet systemd-resolved.service 2>/dev/null || return 1
    [[ -x /usr/sbin/resolvconf ]] || return 1

    local target
    target="$(readlink -f /etc/resolv.conf 2>/dev/null || true)"

    case "$target" in
        /run/systemd/resolve/*|/usr/lib/systemd/resolv.conf)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

write_file_if_changed() {
    local path="$1" mode="$2" content="$3"
    local current=""

    if [[ -f "$path" ]]; then
        current="$(cat "$path")"
    fi

    if [[ "$current" != "$content" ]]; then
        install -d -m 0755 "$(dirname "$path")"
        printf '%s\n' "$content" > "$path"
        chmod "$mode" "$path"
        return 0
    fi

    chmod "$mode" "$path"
    return 1
}

calculate_managed_dns_config_hash() {
    if [[ ! -f "$RESOLVCONF_NOOP" || ! -f "$STRONGSWAN_RESOLVE_OVERRIDE" ]]; then
        return 1
    fi

    {
        cat "$RESOLVCONF_NOOP"
        printf '\n--CONFIG--\n'
        cat "$STRONGSWAN_RESOLVE_OVERRIDE"
    } | sha256sum | awk '{print $1}'
}

mark_dns_config_applied() {
    if [[ "${DNS_MODE:-managed}" == "managed" && -n "${PENDING_DNS_CONFIG_HASH:-}" ]]; then
        printf '%s\n' "$PENDING_DNS_CONFIG_HASH" > "$DNS_CONFIG_MARKER"
        chmod 0600 "$DNS_CONFIG_MARKER"
    else
        rm -f "$DNS_CONFIG_MARKER"
    fi
}

configure_strongswan_dns_integration() {
    DNS_MODE="managed"

    if systemd_resolved_usable; then
        DNS_MODE="native"

        if [[ -f "$STRONGSWAN_RESOLVE_OVERRIDE" ]]; then
            step "Restoring native strongSwan DNS integration..."
            rm -f "$STRONGSWAN_RESOLVE_OVERRIDE"
            STRONGSWAN_DNS_CONFIG_CHANGED=1
        fi

        rm -f "$DNS_CONFIG_MARKER"
        PENDING_DNS_CONFIG_HASH=""

        info "Native strongSwan/systemd-resolved DNS integration is available."
        return 0
    fi

    local helper_content
    helper_content='#!/bin/sh
case "${1:-}" in
    -a) cat >/dev/null ;;
esac
exit 0'

    if write_file_if_changed "$RESOLVCONF_NOOP" 0755 "$helper_content"; then
        STRONGSWAN_DNS_CONFIG_CHANGED=1
    fi

    local override_content
    override_content="resolve {
    resolvconf {
        path = $RESOLVCONF_NOOP
    }
}"

    if write_file_if_changed "$STRONGSWAN_RESOLVE_OVERRIDE" 0644 "$override_content"; then
        STRONGSWAN_DNS_CONFIG_CHANGED=1
    fi

    PENDING_DNS_CONFIG_HASH="$(calculate_managed_dns_config_hash || true)"

    local applied_hash=""
    if [[ -f "$DNS_CONFIG_MARKER" ]]; then
        applied_hash="$(cat "$DNS_CONFIG_MARKER" 2>/dev/null || true)"
    fi

    if [[ -z "$PENDING_DNS_CONFIG_HASH" || "$applied_hash" != "$PENDING_DNS_CONFIG_HASH" ]]; then
        STRONGSWAN_DNS_CONFIG_CHANGED=1
    fi

    info "Native strongSwan DNS integration is unavailable or unsuitable."
    info "The utility will manage VPN DNS without using the system resolvconf backend."

    if [[ "${STRONGSWAN_DNS_CONFIG_CHANGED:-0}" -eq 1 ]]; then
        info "A strongSwan restart is required to activate the managed DNS helper."
    fi
}

dns_state_path() {
    printf '%s/%s.state\n' "$DNS_STATE_DIR" "$1"
}

extract_dns_servers() {
    sed -nE 's/.*installing DNS server ([0-9A-Fa-f:.]+).*/\1/p' |
        awk 'NF && !seen[$0]++'
}

prepare_direct_dns_backup() {
    local id="$1"

    if [[ "${DNS_MODE:-managed}" == "native" ]]; then
        return
    fi

    if [[ -f "$DNS_OWNER_FILE" ]]; then
        return
    fi

    if [[ -r /etc/resolv.conf ]]; then
        cp -L /etc/resolv.conf "$DNS_GLOBAL_BACKUP"
        chmod 0600 "$DNS_GLOBAL_BACKUP"
        info "Saved the current /etc/resolv.conf for automatic restoration."
    fi
}

apply_dns_for_profile() {
    local id="$1"
    shift
    local -a dns_servers=("$@")

    [[ ${#dns_servers[@]} -gt 0 ]] || return 1

    if [[ "${DNS_MODE:-managed}" == "native" ]]; then
        info "VPN DNS is managed by the native strongSwan resolve integration."
        return 0
    fi

    local owner=""
    [[ -f "$DNS_OWNER_FILE" ]] && owner="$(cat "$DNS_OWNER_FILE" 2>/dev/null || true)"

    if [[ -n "$owner" && "$owner" != "$id" ]] && is_connected "$owner"; then
        warn "Another VPN profile currently owns the direct /etc/resolv.conf override."
        warn "DNS was not changed for this profile."
        return 1
    fi

    if [[ ! -f "$DNS_GLOBAL_BACKUP" && -r /etc/resolv.conf ]]; then
        cp -L /etc/resolv.conf "$DNS_GLOBAL_BACKUP"
        chmod 0600 "$DNS_GLOBAL_BACKUP"
    fi

    step "Applying VPN DNS directly to /etc/resolv.conf..."

    local temp
    temp="$(mktemp)"
    {
        local dns
        for dns in "${dns_servers[@]}"; do
            printf 'nameserver %s\n' "$dns"
        done

        if [[ -f "$DNS_GLOBAL_BACKUP" ]]; then
            grep -Ev '^[[:space:]]*nameserver[[:space:]]+' "$DNS_GLOBAL_BACKUP" || true
        fi
    } > "$temp"

    cat "$temp" > /etc/resolv.conf
    rm -f "$temp"

    printf '%s\n' "$id" > "$DNS_OWNER_FILE"
    printf 'direct\t/etc/resolv.conf\n' > "$(dns_state_path "$id")"
    chmod 0600 "$DNS_OWNER_FILE" "$(dns_state_path "$id")"

    info "VPN DNS was applied with a managed /etc/resolv.conf override."
    return 0
}

remove_dns_for_profile() {
    local id="$1"

    if [[ "${DNS_MODE:-managed}" == "native" ]]; then
        rm -f "$(dns_state_path "$id")"
        return 0
    fi

    local state_file
    state_file="$(dns_state_path "$id")"

    [[ -f "$state_file" ]] || return 0

    local mode value
    IFS=$'\t' read -r mode value < "$state_file"

    case "$mode" in
        resolvconf)
            if [[ -n "$value" && -x /usr/sbin/resolvconf ]]; then
                step "Removing VPN DNS from systemd-resolved..."
                /usr/sbin/resolvconf -d "$value" >/dev/null 2>&1 || true
            fi
            ;;
        direct)
            local owner=""
            [[ -f "$DNS_OWNER_FILE" ]] && owner="$(cat "$DNS_OWNER_FILE" 2>/dev/null || true)"

            if [[ "$owner" == "$id" && -f "$DNS_GLOBAL_BACKUP" ]]; then
                step "Restoring the previous /etc/resolv.conf..."
                cat "$DNS_GLOBAL_BACKUP" > /etc/resolv.conf
                rm -f "$DNS_GLOBAL_BACKUP" "$DNS_OWNER_FILE"
            fi
            ;;
    esac

    rm -f "$state_file"
}

cleanup_stale_dns_override() {
    [[ -f "$DNS_OWNER_FILE" ]] || return 0

    local owner
    owner="$(cat "$DNS_OWNER_FILE" 2>/dev/null || true)"
    [[ -n "$owner" ]] || return 0

    if ! is_connected "$owner"; then
        if [[ -f "$DNS_GLOBAL_BACKUP" ]]; then
            info "Restoring DNS left by a previously disconnected VPN session."
            cat "$DNS_GLOBAL_BACKUP" > /etc/resolv.conf
        fi
        rm -f "$DNS_GLOBAL_BACKUP" "$DNS_OWNER_FILE" "$(dns_state_path "$owner")"
    fi
}

parse_ikev_profile() {
    local profile_path="$1"
    local parser_output error_code error_value
    local -a values=()

    if ! parser_output="$(
        python3 - "$profile_path" <<'PY'
import json
import sys

def display(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = str(value)
        if len(value) <= 120 and all(32 <= ord(ch) < 127 for ch in value):
            return value
    return "<invalid>"

def fail(code, value=""):
    print(f"{code}\t{display(value)}")
    raise SystemExit(1)

def string_field(container, key):
    value = container.get(key)
    if not isinstance(value, str) or not value:
        fail("SCHEMA", key)
    if any(ord(ch) < 32 for ch in value):
        fail("SCHEMA", key)
    return value

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        profile = json.load(handle)
except (OSError, UnicodeError, json.JSONDecodeError):
    fail("JSON")

if not isinstance(profile, dict):
    fail("FORMAT")

if profile.get("format") != "ikev-profile":
    fail("FORMAT", profile.get("format"))

version = profile.get("version")
if not isinstance(version, int) or isinstance(version, bool):
    fail("VERSION", version)
if version > 1:
    fail("VERSION_FUTURE", version)
if version != 1:
    fail("VERSION", version)

name = string_field(profile, "name")
server = string_field(profile, "server")
remote_id = string_field(profile, "remote_id")
username = string_field(profile, "username")

authentication = string_field(profile, "authentication")
if authentication != "eap-mschapv2":
    fail("AUTH", authentication)

connection = profile.get("connection")
if not isinstance(connection, dict):
    fail("SCHEMA", "connection")
mode = string_field(connection, "mode")
if mode != "full-tunnel":
    fail("MODE", mode)
ipv6_enabled = connection.get("ipv6", False)
if not isinstance(ipv6_enabled, bool):
    fail("SCHEMA", "connection.ipv6")

certificate_trust = profile.get("certificate_trust", "private-ca")
if certificate_trust not in ("private-ca", "public"):
    fail("TRUST", certificate_trust)
ca_data = ""
ca_sha256 = ""
if certificate_trust == "private-ca":
    ca_certificate = profile.get("ca_certificate")
    if not isinstance(ca_certificate, dict):
        fail("SCHEMA", "ca_certificate")
    encoding = string_field(ca_certificate, "encoding")
    if encoding != "der-base64":
        fail("ENCODING", encoding)
    ca_data = string_field(ca_certificate, "data")
    ca_sha256 = string_field(ca_certificate, "sha256")

server_profile = string_field(profile, "server_profile")
if server_profile not in ("secure", "stock-windows-compatible"):
    fail("SERVER_PROFILE", server_profile)

proxy = profile.get("proxy")
if not isinstance(proxy, dict) or not isinstance(proxy.get("enabled"), bool):
    fail("PROXY")
proxy_enabled = proxy["enabled"]
if proxy_enabled:
    proxy_type = string_field(proxy, "type")
    if proxy_type != "socks5":
        fail("PROXY_TYPE", proxy_type)
    proxy_host = string_field(proxy, "host")
    proxy_port = proxy.get("port")
    if not isinstance(proxy_port, int) or isinstance(proxy_port, bool) or not 1 <= proxy_port <= 65535:
        fail("PROXY_PORT", proxy_port)
else:
    proxy_type = "-"
    proxy_host = "-"
    proxy_port = "-"

for value in (
    name,
    server,
    remote_id,
    username,
    ca_data,
    certificate_trust,
    ca_sha256,
    server_profile,
    "true" if ipv6_enabled else "false",
    "true" if proxy_enabled else "false",
    proxy_type,
    proxy_host,
    str(proxy_port),
):
    print(value)
PY
    )"; then
        IFS=$'\t' read -r error_code error_value <<< "$parser_output"
        case "$error_code" in
            FORMAT|JSON|SCHEMA)
                warn "This is not a supported IKEv profile."
                ;;
            VERSION_FUTURE)
                warn "This profile uses .ikev format version ${error_value}."
                warn "Linux client v${APP_VERSION} supports version 1 only."
                ;;
            VERSION)
                warn "Unsupported .ikev format version: ${error_value}."
                warn "Linux client v${APP_VERSION} supports version 1 only."
                ;;
            AUTH)
                warn "Unsupported authentication method: ${error_value}."
                ;;
            MODE)
                warn "Unsupported connection mode: ${error_value}."
                ;;
            ENCODING)
                warn "Unsupported CA certificate encoding: ${error_value}."
                ;;
            SERVER_PROFILE)
                warn "Unsupported server profile: ${error_value}."
                ;;
            TRUST)
                warn "Unsupported certificate trust mode: ${error_value}."
                ;;
            PROXY_TYPE)
                warn "Unsupported proxy type: ${error_value}."
                ;;
            PROXY_PORT|PROXY)
                warn "The .ikev profile contains invalid proxy metadata."
                ;;
            *)
                warn "This is not a supported IKEv profile."
                ;;
        esac
        return 1
    fi

    mapfile -t values <<< "$parser_output"
    if [[ ${#values[@]} -ne 13 ]]; then
        warn "This is not a supported IKEv profile."
        return 1
    fi

    IKEV_NAME="${values[0]}"
    IKEV_SERVER="${values[1]}"
    IKEV_REMOTE_ID="${values[2]}"
    IKEV_USERNAME="${values[3]}"
    IKEV_CA_DATA="${values[4]}"
    IKEV_CERTIFICATE_TRUST="${values[5]}"
    IKEV_CA_SHA256="${values[6]}"
    IKEV_IPV6_ENABLED="${values[8]}"
    IKEV_PROXY_ENABLED="${values[9]}"
    IKEV_PROXY_HOST="${values[11]}"
    IKEV_PROXY_PORT="${values[12]}"

    if ! validate_profile_name "$IKEV_NAME"; then
        warn "The .ikev profile name is invalid."
        return 1
    fi
    if ! validate_server "$IKEV_SERVER"; then
        warn "The .ikev server address is invalid."
        return 1
    fi
    if ! validate_server "$IKEV_REMOTE_ID"; then
        warn "The .ikev remote identity is invalid."
        return 1
    fi
    if ! validate_username "$IKEV_USERNAME"; then
        warn "The .ikev username is invalid."
        return 1
    fi
    if [[ "$IKEV_PROXY_ENABLED" == "true" ]]; then
        if ! validate_server "$IKEV_PROXY_HOST" ||
           [[ ! "$IKEV_PROXY_PORT" =~ ^[0-9]{1,5}$ ]] ||
           ((10#$IKEV_PROXY_PORT < 1 || 10#$IKEV_PROXY_PORT > 65535)); then
            warn "The .ikev profile contains invalid proxy metadata."
            return 1
        fi
    fi
}

cleanup_ikev_import_temp() {
    if [[ -n "${IKEV_IMPORT_TEMP_DIR:-}" && -d "$IKEV_IMPORT_TEMP_DIR" ]]; then
        rm -rf -- "$IKEV_IMPORT_TEMP_DIR"
    fi
    IKEV_IMPORT_TEMP_DIR=""
}

verify_imported_ca() {
    local der_file="$1" pem_file="$2" expected_fingerprint="$3"
    local certificate_text calculated_fingerprint

    if ! openssl x509 -inform DER -in "$der_file" -noout >/dev/null 2>&1; then
        warn "The embedded CA certificate is invalid."
        return 1
    fi

    if ! certificate_text="$(
        openssl x509 -inform DER -in "$der_file" -noout -ext basicConstraints 2>/dev/null
    )"; then
        warn "The embedded CA certificate is invalid."
        return 1
    fi
    if ! grep -Eq '^[[:space:]]*CA:TRUE([[:space:],]|$)' <<< "$certificate_text"; then
        warn "The embedded certificate is not a CA certificate."
        return 1
    fi

    if ! openssl x509 -inform DER -in "$der_file" -checkend 0 -noout >/dev/null 2>&1; then
        warn "The embedded CA certificate is expired."
        return 1
    fi

    calculated_fingerprint="$(
        openssl x509 -inform DER -in "$der_file" -noout -fingerprint -sha256 2>/dev/null |
            cut -d= -f2
    )"
    if [[ -z "$calculated_fingerprint" || "${calculated_fingerprint^^}" != "${expected_fingerprint^^}" ]]; then
        warn "CA certificate fingerprint verification failed."
        warn "The profile may be corrupted or modified."
        return 1
    fi

    install -m 0600 /dev/null "$pem_file"
    if ! openssl x509 -inform DER -in "$der_file" -out "$pem_file" -outform PEM 2>/dev/null; then
        warn "The embedded CA certificate is invalid."
        return 1
    fi

    IKEV_VERIFIED_CA_FINGERPRINT="$calculated_fingerprint"
    IKEV_VERIFIED_CA_SUBJECT="$(
        openssl x509 -in "$pem_file" -noout -subject -nameopt RFC2253 2>/dev/null |
            sed 's/^subject=//'
    )"
    [[ -n "$IKEV_VERIFIED_CA_SUBJECT" ]] || {
        warn "The embedded CA certificate is invalid."
        return 1
    }
}

managed_profiles_exist() {
    local -a profiles=()

    shopt -s nullglob
    profiles=("$META_DIR"/*.meta)
    shopt -u nullglob

    [[ ${#profiles[@]} -gt 0 ]]
}

check_imported_ca_compatibility() {
    local imported_fingerprint="$1"
    local managed_ca="$CACERT_DIR/ikev2-client-ca.pem"
    local existing_fingerprint=""

    IKEV_CA_ACTION="install"
    [[ -f "$managed_ca" ]] || return 0

    existing_fingerprint="$(
        openssl x509 -in "$managed_ca" -noout -fingerprint -sha256 2>/dev/null |
            cut -d= -f2
    )"

    if [[ -n "$existing_fingerprint" && "${existing_fingerprint^^}" == "${imported_fingerprint^^}" ]]; then
        IKEV_CA_ACTION="reuse"
        return 0
    fi

    if managed_profiles_exist; then
        warn "This .ikev profile uses a different VPN CA."
        warn "Linux client v${APP_VERSION} uses one managed VPN CA at a time."
        warn "Replacing the CA could break existing managed VPN profiles."
        return 1
    fi

    IKEV_CA_ACTION="replace"
}

install_imported_ca() {
    local pem_file="$1"
    local managed_ca="$CACERT_DIR/ikev2-client-ca.pem"

    INSTALLED_CA_SUBJECT="$IKEV_VERIFIED_CA_SUBJECT"

    if [[ "$IKEV_CA_ACTION" == "reuse" ]]; then
        info "The same managed VPN CA is already installed; reusing it."
        return 0
    fi

    ensure_directories

    step "Installing the imported VPN CA certificate for strongSwan..."
    if ! install -m 0644 "$pem_file" "$managed_ca"; then
        warn "The imported VPN CA certificate could not be installed."
        return 1
    fi

    step "Adding the imported VPN CA certificate to the Ubuntu system trust store..."
    if ! install -m 0644 "$pem_file" "$SYSTEM_CA" || ! update-ca-certificates >/dev/null; then
        warn "The imported VPN CA certificate could not be added to the system trust store."
        return 1
    fi

    info "Trusted CA SHA-256 fingerprint: $IKEV_VERIFIED_CA_FINGERPRINT"
    info "Trusted CA subject: $INSTALLED_CA_SUBJECT"
}

import_ikev_profile() (
    local profile_path profile_size temp_der temp_pem
    local id proxy_summary answer password existing_profile="no"

    printf '\nImport .ikev VPN Profile\n'
    printf '========================\n\n'

    if ! read -r -p "Path to .ikev profile: " profile_path || [[ -z "$profile_path" ]]; then
        warn "A readable .ikev profile path is required."
        return
    fi
    if [[ ! -e "$profile_path" || ! -f "$profile_path" || ! -r "$profile_path" ]]; then
        warn "The .ikev profile path must be a readable regular file."
        return
    fi

    if ! profile_size="$(stat -c '%s' -- "$profile_path" 2>/dev/null)"; then
        warn "The .ikev profile could not be read."
        return
    fi
    if ((profile_size > 1048576)); then
        warn "The .ikev profile is unexpectedly large."
        return
    fi

    install_packages
    if ! command -v python3 >/dev/null 2>&1; then
        warn "Python 3 is required to import .ikev profiles."
        return
    fi

    if ! parse_ikev_profile "$profile_path"; then
        return
    fi

    if ! IKEV_IMPORT_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ikev2-import.XXXXXX")"; then
        warn "A secure temporary directory could not be created."
        return
    fi
    chmod 0700 "$IKEV_IMPORT_TEMP_DIR"
    trap cleanup_ikev_import_temp EXIT

    if [[ "$IKEV_CERTIFICATE_TRUST" == "private-ca" ]]; then
        temp_der="$IKEV_IMPORT_TEMP_DIR/ca.der"
        temp_pem="$IKEV_IMPORT_TEMP_DIR/ca.pem"
        if ! (umask 077; printf '%s' "$IKEV_CA_DATA" | base64 --decode > "$temp_der" 2>/dev/null); then
            warn "The embedded CA certificate is invalid."
            return
        fi
        if ! verify_imported_ca "$temp_der" "$temp_pem" "$IKEV_CA_SHA256"; then
            return
        fi
        if ! check_imported_ca_compatibility "$IKEV_VERIFIED_CA_FINGERPRINT"; then
            return
        fi
    fi

    if [[ "$IKEV_PROXY_ENABLED" == "true" ]]; then
        proxy_summary="available (${IKEV_PROXY_HOST}:${IKEV_PROXY_PORT})"
    else
        proxy_summary="not advertised"
    fi

    printf '\nIKEv Profile\n'
    printf '============\n\n'
    printf 'Name        : %s\n' "$IKEV_NAME"
    printf 'Server      : %s\n' "$IKEV_SERVER"
    printf 'Remote ID   : %s\n' "$IKEV_REMOTE_ID"
    printf 'Username    : %s\n' "$IKEV_USERNAME"
    printf 'Auth        : EAP-MSCHAPv2\n'
    printf 'Mode        : Full tunnel\n'
    if [[ "$IKEV_CERTIFICATE_TRUST" == "public" ]]; then
        printf 'Trust       : Public system trust\n'
    else
        printf 'CA SHA-256  : %s\n' "$IKEV_VERIFIED_CA_FINGERPRINT"
    fi
    printf 'Proxy Mode  : %s\n\n' "$proxy_summary"

    if ! read -r -p "Import and trust this VPN profile? [y/N]: " answer ||
       [[ ! "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        info "Import canceled."
        return
    fi

    id="$(profile_id_from_name "$IKEV_NAME")"
    if [[ -f "$(metadata_path "$id")" ]]; then
        existing_profile="yes"
        printf "Profile '%s' already exists.\n" "$IKEV_NAME"
        if ! read -r -p "Replace it? [y/N]: " answer ||
           [[ ! "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
            info "Import canceled."
            return
        fi
    fi

    while true; do
        if ! read -r -s -p "VPN password: " password; then
            printf '\n'
            info "Import canceled."
            return
        fi
        printf '\n'
        [[ -n "$password" ]] && break
        warn "Password cannot be empty."
    done

    if [[ "$IKEV_CERTIFICATE_TRUST" == "private-ca" ]]; then
        if ! install_imported_ca "$temp_pem"; then
            unset password
            return
        fi
    fi

    ensure_directories
    configure_strongswan_dns_integration
    ensure_include_line "$IPSEC_CONF" "$CONF_INCLUDE" 0644
    ensure_include_line "$IPSEC_SECRETS" "$SECRETS_INCLUDE" 0600

    if [[ "$existing_profile" == "yes" ]] && is_connected "$id"; then
        step "Disconnecting the existing profile..."
        ipsec down "$id" >/dev/null 2>&1 || true
        remove_dns_for_profile "$id" || true
    fi

    write_strongswan_profile \
        "$id" "$IKEV_NAME" "$IKEV_SERVER" "$IKEV_REMOTE_ID" "$IKEV_USERNAME" "$password" \
        "$( [[ "$IKEV_IPV6_ENABLED" == "true" ]] && echo yes || echo no )"
    unset password

    restart_or_reload_strongswan

    if [[ -n "${INSTALLED_CA_SUBJECT:-}" ]] && ! verify_ca_loaded "$INSTALLED_CA_SUBJECT"; then
        warn "The VPN CA certificate is installed on disk but is not loaded by strongSwan."
        warn "Restarting strongSwan once to force a complete certificate reload..."
        systemctl restart strongswan-starter.service
        sleep 1
        ipsec rereadall >/dev/null 2>&1 || true

        if ! verify_ca_loaded "$INSTALLED_CA_SUBJECT"; then
            warn "The VPN CA certificate still is not visible in 'ipsec listcacerts'."
            warn "Check: $CACERT_DIR/ikev2-client-ca.pem"
            warn "Check: sudo ipsec listcacerts"
            return
        fi
    fi

    if ! ipsec statusall 2>/dev/null | grep -Fq "$id"; then
        warn "The profile was written, but strongSwan did not report it. Check: journalctl -u strongswan-starter -n 100"
        return
    fi

    printf '\nVPN profile imported successfully.\n\n'
    printf 'Profile : %s\n' "$IKEV_NAME"
    printf 'Server  : %s\n' "$IKEV_SERVER"
    printf 'User    : %s\n' "$IKEV_USERNAME"
    printf 'Mode    : Full tunnel%s\n\n' "$( [[ "$IKEV_IPV6_ENABLED" == "true" ]] && echo ' IPv4 + IPv6' || echo ' IPv4' )"

    cleanup_ikev_import_temp
    trap - EXIT

    read -r -p "Connect now? [Y/n]: " answer || true
    if [[ ! "$answer" =~ ^[Nn]([Oo])?$ ]]; then
        connect_profile_by_id "$id" "$IKEV_NAME"
    fi
)

install_update_profile() {
    install_packages
    ensure_directories
    install_ca_certificate
    configure_strongswan_dns_integration

    ensure_include_line "$IPSEC_CONF" "$CONF_INCLUDE" 0644
    ensure_include_line "$IPSEC_SECRETS" "$SECRETS_INCLUDE" 0600

    printf '\nInstall / Update IKEv2 VPN\n'
    printf '==========================\n\n'

    local name server username password id ipv6_enabled="no"
    while true; do
        read -r -p "VPN profile name [IKEv2 VPN]: " name
        name="${name:-IKEv2 VPN}"
        validate_profile_name "$name" && break
        warn "Profile name may not contain tabs or line breaks."
    done

    while true; do
        read -r -p "VPN server IP or hostname: " server
        validate_server "$server" && break
        warn "Enter a valid IPv4 address, IPv6 address, or hostname."
    done

    while true; do
        read -r -p "VPN username: " username
        validate_username "$username" && break
        warn "Username may contain letters, numbers, dot, underscore, @, +, and -."
    done

    while true; do
        read -r -s -p "VPN password: " password
        printf '\n'
        [[ -n "$password" ]] && break
        warn "Password cannot be empty."
    done

    if ask_yes_no "Enable IPv6 full-tunnel for this profile? (The server must support IPv6.)" N; then
        ipv6_enabled="yes"
    fi

    id="$(profile_id_from_name "$name")"

    if is_connected "$id"; then
        step "Disconnecting the existing profile..."
        ipsec down "$id" >/dev/null 2>&1 || true
    fi

    write_strongswan_profile "$id" "$name" "$server" "$server" "$username" "$password" "$ipv6_enabled"
    unset password

    restart_or_reload_strongswan

    if [[ -n "${INSTALLED_CA_SUBJECT:-}" ]] && ! verify_ca_loaded "$INSTALLED_CA_SUBJECT"; then
        warn "The VPN CA certificate is installed on disk but is not loaded by strongSwan."
        warn "Restarting strongSwan once to force a complete certificate reload..."
        systemctl restart strongswan-starter.service
        sleep 1
        ipsec rereadall >/dev/null 2>&1 || true

        if ! verify_ca_loaded "$INSTALLED_CA_SUBJECT"; then
            warn "The VPN CA certificate still is not visible in 'ipsec listcacerts'."
            warn "Check: $CACERT_DIR/ikev2-client-ca.pem"
            warn "Check: sudo ipsec listcacerts"
            return
        fi
    fi

    if ! ipsec statusall 2>/dev/null | grep -Fq "$id"; then
        warn "The profile was written, but strongSwan did not report it. Check: journalctl -u strongswan-starter -n 100"
        return
    fi

    printf '\nVPN profile created successfully.\n'
    printf '  Profile : %s\n' "$name"
    printf '  Server  : %s\n' "$server"
    printf '  User    : %s\n' "$username"
    printf '  Mode    : Full tunnel%s\n\n' "$( [[ "$ipv6_enabled" == "yes" ]] && echo ' IPv4 + IPv6' || echo ' IPv4' )"

    prompt_system_wide_install

    local answer
    read -r -p "Connect now? [Y/n]: " answer
    if [[ ! "$answer" =~ ^[Nn]([Oo])?$ ]]; then
        connect_profile_by_id "$id" "$name"
    fi
}

connect_profile_by_id() {
    local id="$1" name="$2"
    local output rc started journal_output
    local -a dns_servers=()

    if is_connected "$id"; then
        info "VPN profile '$name' is already connected."
        return
    fi

    configure_strongswan_dns_integration

    if [[ "${STRONGSWAN_DNS_CONFIG_CHANGED:-0}" -eq 1 ]]; then
        restart_or_reload_strongswan
    elif [[ "${DNS_MODE:-managed}" == "managed" ]]; then
        info "Managed strongSwan DNS helper configuration is active."
    fi

    if [[ "${STRONGSWAN_DNS_CONFIG_CHANGED:-0}" -eq 0 ]]; then
        step "Reloading CA certificates and secrets before connection..."
        ipsec rereadall >/dev/null 2>&1 || true
    fi

    prepare_direct_dns_backup "$id"

    started="$(date '+%Y-%m-%d %H:%M:%S')"

    step "Connecting '$name'..."
    set +e
    output="$(ipsec up "$id" 2>&1)"
    rc=$?
    set -e

    printf '%s
' "$output"
    sleep 1

    if is_connected "$id"; then
        journal_output="$(
            journalctl -u strongswan-starter.service \
                --since "$started" \
                --no-pager 2>/dev/null || true
        )"

        while IFS= read -r dns; do
            [[ -n "$dns" ]] && dns_servers+=("$dns")
        done < <(
            {
                printf '%s
' "$output"
                printf '%s
' "$journal_output"
            } | extract_dns_servers
        )

        if [[ ${#dns_servers[@]} -gt 0 ]]; then
            apply_dns_for_profile "$id" "${dns_servers[@]}" || true
            info "Negotiated VPN DNS: ${dns_servers[*]}"
        else
            warn "The VPN is connected, but no pushed DNS server was found in strongSwan output."
            warn "Check whether the server sends INTERNAL_IP4_DNS/INTERNAL_IP6_DNS attributes."
        fi

        printf '
VPN connected successfully.
'
        return 0
    fi

    remove_dns_for_profile "$id" || true

    warn "VPN connection failed."

    if grep -q "no issuer certificate found" <<< "$output" || \
       grep -q "no trusted .* public key found" <<< "$output"; then
        warn "The server certificate could not be chained to a trusted CA."
        warn "Run: sudo ipsec listcacerts"
        warn "Expected CA file: $CACERT_DIR/ikev2-client-ca.pem"
    elif grep -q "AUTH_FAILED" <<< "$output"; then
        warn "Authentication failed. Check the VPN username/password and certificate trust."
    elif (( rc != 0 )); then
        warn "The strongSwan command returned exit code $rc."
    fi

    printf 'Check logs with:
'
    printf '  sudo journalctl -u strongswan-starter -n 100 --no-pager
'
    printf '  sudo ipsec statusall
'
    return 1
}

connect_selected() {
    select_profile "connect" || return
    connect_profile_by_id "$META_ID" "$META_NAME"
}

disconnect_selected() {
    select_profile "disconnect" || return

    disable_autoreconnect "$META_ID"

    if ! is_connected "$META_ID"; then
        remove_dns_for_profile "$META_ID"
        info "VPN profile '$META_NAME' is already disconnected."
        return
    fi

    step "Disconnecting '$META_NAME'..."
    if ipsec down "$META_ID"; then
        remove_dns_for_profile "$META_ID"
        printf '\nVPN disconnected successfully.\n'
    else
        warn "The disconnect command failed."
        if ! is_connected "$META_ID"; then
            remove_dns_for_profile "$META_ID"
        fi
    fi
}

autoreconnect_unit_name() {
    printf '%s%s.service' "$AUTORECONNECT_UNIT_PREFIX" "$1"
}

autoreconnect_watch() {
    local id="$1"
    local metadata

    metadata="$(metadata_path "$id")"
    [[ -f "$metadata" ]] || die "Auto-reconnect profile no longer exists."

    while [[ -f "$metadata" ]]; do
        if ! is_connected "$id"; then
            read_metadata "$metadata"
            info "Auto-reconnect: attempting to connect '$META_NAME'."
            connect_profile_by_id "$id" "$META_NAME" || true
        fi
        sleep 15
    done
}

enable_autoreconnect() {
    local id="$1"
    local unit unit_path

    unit="$(autoreconnect_unit_name "$id")"
    unit_path="/etc/systemd/system/$unit"
    install -d -m 0755 "$SYSTEM_INSTALL_DIR"
    install -m 0755 "$(readlink -f -- "${BASH_SOURCE[0]}")" "$SYSTEM_SCRIPT"
    cat > "$unit_path" <<EOF
[Unit]
Description=IKEv2 auto-reconnect for profile $id
After=network-online.target strongswan-starter.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$SYSTEM_SCRIPT --autoreconnect-watch $id
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$unit"
    info "Auto-reconnect enabled for '$META_NAME' and will start at boot."
}

disable_autoreconnect() {
    local id="$1"
    local unit

    unit="$(autoreconnect_unit_name "$id")"
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$unit"
    systemctl daemon-reload
}

toggle_autoreconnect() {
    select_profile "configure auto-reconnect" || return
    local unit
    unit="$(autoreconnect_unit_name "$META_ID")"

    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        disable_autoreconnect "$META_ID"
        info "Auto-reconnect disabled for '$META_NAME'."
    else
        enable_autoreconnect "$META_ID"
    fi
}

show_selected_status() {
    select_profile "view" || return

    printf '\nVPN Status\n'
    printf '==========\n\n'
    printf 'Profile    : %s\n' "$META_NAME"
    printf 'Server     : %s\n' "$META_SERVER"
    printf 'Username   : %s\n' "$META_USER"
    printf 'Connection : %s\n\n' "$(profile_status_word "$META_ID")"

    if command -v ipsec >/dev/null 2>&1; then
        ipsec status "$META_ID" || true
        printf '\n'
    fi

    if is_connected "$META_ID"; then
        printf 'XFRM state:\n'
        ip -brief xfrm state 2>/dev/null || ip xfrm state 2>/dev/null | sed -n '1,20p' || true
        printf '\n'
        printf 'Virtual IP / routes:\n'
        ip -brief address 2>/dev/null | grep -E '10\.|172\.|192\.168\.' || true
        ip route show table 220 2>/dev/null || true
        ip -6 route show table 220 2>/dev/null || true
    fi
}

remove_selected_profile() {
    select_profile "remove" || return

    printf '\n'
    read -r -p "Remove VPN profile '$META_NAME'? [y/N]: " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || return

    disable_autoreconnect "$META_ID"

    if is_connected "$META_ID"; then
        ipsec down "$META_ID" >/dev/null 2>&1 || true
    fi
    remove_dns_for_profile "$META_ID"

    rm -f \
        "$CONF_DIR/$META_ID.conf" \
        "$SECRETS_DIR/$META_ID.secrets" \
        "$(metadata_path "$META_ID")"

    ipsec reload >/dev/null 2>&1 || true
    ipsec rereadsecrets >/dev/null 2>&1 || true

    printf 'VPN profile removed.\n'
}

remove_exact_line() {
    local file="$1"
    local line="$2"
    local mode="$3"

    [[ -f "$file" ]] || return 0

    local temp
    temp="$(mktemp)"

    grep -Fvx -- "$line" "$file" > "$temp" || true
    cat "$temp" > "$file"
    chmod "$mode" "$file"
    rm -f "$temp"
}

restore_all_dns_state() {
    local -a state_files=()
    local state_file mode value

    shopt -s nullglob
    state_files=("$DNS_STATE_DIR"/*.state)
    shopt -u nullglob

    for state_file in "${state_files[@]}"; do
        mode=""
        value=""
        IFS=$'\t' read -r mode value < "$state_file" || true

        if [[ "$mode" == "resolvconf" && -n "$value" && -x /usr/sbin/resolvconf ]]; then
            /usr/sbin/resolvconf -d "$value" >/dev/null 2>&1 || true
        fi
    done

    if [[ -f "$DNS_GLOBAL_BACKUP" ]]; then
        step "Restoring the pre-VPN /etc/resolv.conf..."
        cat "$DNS_GLOBAL_BACKUP" > /etc/resolv.conf
    fi

    rm -f "$DNS_GLOBAL_BACKUP" "$DNS_OWNER_FILE"
    rm -f "$DNS_STATE_DIR"/*.state 2>/dev/null || true
}

uninstall_utility() {
    printf '\nUninstall IKEv2 Linux VPN Utility\n'
    printf '================================\n\n'
    printf 'This will remove:\n'
    printf '  - All VPN profiles created by this utility\n'
    printf '  - Stored VPN credentials created by this utility\n'
    printf '  - The VPN CA certificate installed by this utility\n'
    printf '  - Managed DNS configuration and DNS overrides\n'
    printf '  - The system-wide ikev2 command and /opt installation\n\n'
    printf 'StrongSwan packages will NOT be removed.\n'
    printf 'Unrelated StrongSwan configuration will NOT be removed.\n\n'

    local answer
    read -r -p "Continue with uninstall? [y/N]: " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || {
        info "Uninstall cancelled."
        return
    }

    step "Disconnecting managed VPN profiles..."

    local unit_file
    shopt -s nullglob
    for unit_file in "/etc/systemd/system/${AUTORECONNECT_UNIT_PREFIX}"*.service; do
        systemctl disable --now "${unit_file##*/}" >/dev/null 2>&1 || true
        rm -f "$unit_file"
    done
    shopt -u nullglob
    systemctl daemon-reload

    local -a meta_files=()
    local meta_file

    shopt -s nullglob
    meta_files=("$META_DIR"/*.meta)
    shopt -u nullglob

    for meta_file in "${meta_files[@]}"; do
        read_metadata "$meta_file"

        if command -v ipsec >/dev/null 2>&1 && is_connected "$META_ID"; then
            ipsec down "$META_ID" >/dev/null 2>&1 || true
        fi
    done

    restore_all_dns_state

    step "Removing managed VPN profiles and credentials..."
    rm -f "$CONF_DIR"/*.conf 2>/dev/null || true
    rm -f "$SECRETS_DIR"/*.secrets 2>/dev/null || true
    rm -f "$META_DIR"/*.meta 2>/dev/null || true

    step "Removing managed StrongSwan include lines..."
    remove_exact_line "$IPSEC_CONF" "$CONF_INCLUDE" 0644
    remove_exact_line "$IPSEC_SECRETS" "$SECRETS_INCLUDE" 0600

    step "Removing the VPN CA certificate..."
    rm -f "$CACERT_DIR/ikev2-client-ca.pem"
    rm -f "$SYSTEM_CA"

    if command -v update-ca-certificates >/dev/null 2>&1; then
        update-ca-certificates >/dev/null 2>&1 || true
    fi

    step "Removing managed DNS integration files..."
    rm -f "$STRONGSWAN_RESOLVE_OVERRIDE"
    rm -f "$RESOLVCONF_NOOP"
    rm -f "$DNS_CONFIG_MARKER"

    if command -v ipsec >/dev/null 2>&1 && \
       systemctl is-active --quiet strongswan-starter.service 2>/dev/null; then
        step "Reloading StrongSwan configuration..."
        ipsec reload >/dev/null 2>&1 || true
        ipsec rereadall >/dev/null 2>&1 || true
    fi

    rmdir "$META_DIR" 2>/dev/null || true
    rmdir "$DNS_STATE_DIR" 2>/dev/null || true
    rmdir "$SECRETS_DIR" 2>/dev/null || true
    rmdir "$CONF_DIR" 2>/dev/null || true

    step "Removing system-wide utility files..."
    rm -f "$SYSTEM_COMMAND"
    rm -rf "$SYSTEM_INSTALL_DIR"

    rm -rf "$STATE_DIR"

    printf '\nUninstall complete.\n'
    printf '  Managed VPN profiles : removed\n'
    printf '  Managed credentials  : removed\n'
    printf '  Managed CA certificate: removed\n'
    printf '  Managed DNS state    : removed/restored\n'
    printf '  System-wide command  : removed\n'
    printf '  StrongSwan packages  : kept installed\n\n'

    if systemctl is-active --quiet strongswan-starter.service 2>/dev/null; then
        printf 'Note: StrongSwan was not restarted to avoid interrupting unrelated VPN sessions.\n'
        printf 'If no other StrongSwan VPN is in use, you may later run:\n'
        printf '  sudo systemctl restart strongswan-starter\n\n'
    fi

    exit 0
}

show_all_status() {
    ensure_directories
    local -a files=()
    local file
    while IFS= read -r file; do
        [[ -n "$file" ]] && files+=("$file")
    done < <(list_profile_files)

    printf '\nIKEv2 Profiles\n'
    printf '==============\n\n'

    if [[ ${#files[@]} -eq 0 ]]; then
        printf 'No profiles created by this utility.\n'
        return
    fi

    for file in "${files[@]}"; do
        read_metadata "$file"
        printf '%-24s %-16s %s\n' "$META_NAME" "$(profile_status_word "$META_ID")" "$META_SERVER"
    done
}

usage() {
    printf '%s\n' "Usage: ikev2 [gateway {enable|disable|status|diagnose}]"
    printf '%s\n' "       ikev2 [menu options]"
}

update_client_script() {
    local update_url="https://raw.githubusercontent.com/scargosnail598/myIkev2-connection/main/ikev2-linux-client.sh"
    local downloaded current_path remote_version latest_version target staging
    local -a targets=()

    downloaded="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --silent --show-error --connect-timeout 10 --max-time 120 \
            "$update_url" --output "$downloaded" || {
            rm -f "$downloaded"
            die "Failed to download the latest Linux client. Check your Internet connection."
        }
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet --timeout=120 --output-document="$downloaded" "$update_url" || {
            rm -f "$downloaded"
            die "Failed to download the latest Linux client. Check your Internet connection."
        }
    else
        rm -f "$downloaded"
        die "Install curl or wget to update the Linux client."
    fi

    if [[ ! -s "$downloaded" ]] || ! bash -n "$downloaded"; then
        rm -f "$downloaded"
        die "The downloaded Linux client is empty or has invalid Bash syntax."
    fi

    remote_version="$(sed -n 's/^APP_VERSION="\([0-9][0-9.]*\)"$/\1/p' "$downloaded" | head -n 1)"
    if [[ ! "$remote_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        rm -f "$downloaded"
        die "Could not read a valid version from the downloaded Linux client."
    fi

    current_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
    if [[ "$remote_version" == "$APP_VERSION" ]]; then
        rm -f "$downloaded"
        info "Linux client is already up to date (v$APP_VERSION)."
        return
    fi

    latest_version="$(printf '%s\n%s\n' "$APP_VERSION" "$remote_version" | sort -V | tail -n 1)"
    if [[ "$latest_version" != "$remote_version" ]]; then
        rm -f "$downloaded"
        die "The upstream client version ($remote_version) is older than v$APP_VERSION; refusing to downgrade."
    fi

    targets+=("$current_path")
    if [[ -f "$SYSTEM_SCRIPT" ]] && [[ "$(readlink -f -- "$SYSTEM_SCRIPT")" != "$current_path" ]]; then
        targets+=("$SYSTEM_SCRIPT")
    fi

    for target in "${targets[@]}"; do
        staging="$(mktemp "${target}.update.XXXXXX")"
        if ! install -m 0755 "$downloaded" "$staging" || ! mv -f -- "$staging" "$target"; then
            rm -f "$staging" "$downloaded"
            die "Failed to replace Linux client at $target."
        fi
    done

    rm -f "$downloaded"
    info "Linux client updated to v$remote_version. Restart the utility to use it."
}

main_menu() {
    while true; do
        clear
        printf '%s\n' "$APP_NAME"
        printf '%*s\n' "${#APP_NAME}" '' | tr ' ' '='
        printf 'Version: %s\n\n' "$APP_VERSION"
        printf '1) Install / Update IKEv2 VPN\n'
        printf '2) Import .ikev Profile\n'
        printf '3) List / Status\n'
        printf '4) Connect\n'
        printf '5) Disconnect\n'
        printf '6) Auto-reconnect (toggle)\n'
        printf '7) Remove Profile\n'
        printf '8) Uninstall Utility\n'
        printf '9) Update Client\n'
        printf '10) Gateway Mode\n'
        printf '11) About\n'
        printf '12) Exit\n\n'

        read -r -p "Choose an option [1-12]: " choice

        case "$choice" in
            1) install_update_profile; pause_menu ;;
            2) import_ikev_profile; pause_menu ;;
            3)
                show_all_status
                printf '\n'
                read -r -p "View detailed status for a profile? [y/N]: " answer
                if [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
                    show_selected_status
                fi
                pause_menu
                ;;
            4) connect_selected; pause_menu ;;
            5) disconnect_selected; pause_menu ;;
            6) toggle_autoreconnect; pause_menu ;;
            7) remove_selected_profile; pause_menu ;;
            8) uninstall_utility; pause_menu ;;
            9) update_client_script; pause_menu ;;
            10)
                printf '\nGateway / Router Mode\n'
                printf '====================\n'
                printf '1) Enable Gateway Mode\n'
                printf '2) Disable Gateway Mode\n'
                printf '3) Gateway Status\n'
                printf '4) Gateway Diagnostics\n'
                printf '5) Back\n\n'
                read -r -p "Choose an action [1-5]: " action
                case "$action" in
                    1) gateway_enable ;;
                    2) gateway_disable ;;
                    3) gateway_status ;;
                    4) gateway_diagnostics || true ;;
                    5) return ;;
                    *) warn "Invalid option." ;;
                esac
                pause_menu
                ;;
            11) show_about; pause_menu ;;
            12) printf '\nExiting...\n'; exit 0 ;;
            *) warn "Invalid option."; sleep 1 ;;
        esac
    done
}

require_root "$@"
detect_ubuntu
ensure_directories

if [[ "${1:-}" =~ ^(-h|--help)$ ]]; then
    usage
    exit 0
fi

if [[ "${1:-}" == "--autoreconnect-watch" ]]; then
    [[ $# -eq 2 && "$2" =~ ^ikev2c_[a-f0-9]{12}$ ]] || die "Invalid auto-reconnect profile ID."
    autoreconnect_watch "$2"
    exit 0
fi

if [[ "${1:-}" == "gateway" ]]; then
    shift || true
    case "${1:-}" in
        enable) gateway_enable ; exit $? ;;
        disable) gateway_disable ; exit $? ;;
        status) gateway_status ; exit $? ;;
        diagnose) gateway_diagnostics ; exit $? ;;
        "") usage ; exit 1 ;;
        *) usage ; exit 2 ;;
    esac
fi

cleanup_stale_dns_override

if command -v ipsec >/dev/null 2>&1; then
    configure_strongswan_dns_integration

    if [[ "${STRONGSWAN_DNS_CONFIG_CHANGED:-0}" -eq 1 ]]; then
        restart_or_reload_strongswan
    fi
fi

main_menu
