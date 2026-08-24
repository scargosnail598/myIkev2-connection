#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="IKEv2 Linux VPN Utility"
APP_VERSION="1.5.0"
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
DNS_STATE_DIR="$STATE_DIR/dns-state"
DNS_GLOBAL_BACKUP="$STATE_DIR/resolv.conf.pre-vpn"
DNS_OWNER_FILE="$STATE_DIR/resolv.conf.owner"
STRONGSWAN_RESOLVE_OVERRIDE="/etc/strongswan.d/charon/zz-ikev2-client-resolve.conf"
RESOLVCONF_NOOP="/usr/local/libexec/ikev2-client-resolvconf-noop"
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

install_update_profile() {
    install_packages
    ensure_directories
    install_ca_certificate
    configure_strongswan_dns_integration

    ensure_include_line "$IPSEC_CONF" "$CONF_INCLUDE" 0644
    ensure_include_line "$IPSEC_SECRETS" "$SECRETS_INCLUDE" 0600

    printf '\nInstall / Update IKEv2 VPN\n'
    printf '==========================\n\n'

    local name server username password id
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

    id="$(profile_id_from_name "$name")"

    if is_connected "$id"; then
        step "Disconnecting the existing profile..."
        ipsec down "$id" >/dev/null 2>&1 || true
    fi

    local q_user q_server password_hex
    q_user="$(escape_ipsec_value "$username")"
    q_server="$(escape_ipsec_value "$server")"
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
    rightid=$q_server
    rightauth=pubkey
    rightsubnet=0.0.0.0/0
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
    printf '  Mode    : Full tunnel IPv4\n\n'

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
    fi
}

remove_selected_profile() {
    select_profile "remove" || return

    printf '\n'
    read -r -p "Remove VPN profile '$META_NAME'? [y/N]: " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || return

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

main_menu() {
    while true; do
        clear
        printf '%s\n' "$APP_NAME"
        printf '%*s\n' "${#APP_NAME}" '' | tr ' ' '='
        printf 'Version: %s\n\n' "$APP_VERSION"
        printf '1) Install / Update IKEv2 VPN\n'
        printf '2) List / Status\n'
        printf '3) Connect\n'
        printf '4) Disconnect\n'
        printf '5) Remove Profile\n'
        printf '6) Uninstall Utility\n'
        printf '7) Exit\n\n'

        read -r -p "Choose an option [1-7]: " choice

        case "$choice" in
            1) install_update_profile; pause_menu ;;
            2)
                show_all_status
                printf '\n'
                read -r -p "View detailed status for a profile? [y/N]: " answer
                if [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
                    show_selected_status
                fi
                pause_menu
                ;;
            3) connect_selected; pause_menu ;;
            4) disconnect_selected; pause_menu ;;
            5) remove_selected_profile; pause_menu ;;
            6) uninstall_utility; pause_menu ;;
            7) printf '\nExiting...\n'; exit 0 ;;
            *) warn "Invalid option."; sleep 1 ;;
        esac
    done
}

require_root "$@"
detect_ubuntu
ensure_directories
cleanup_stale_dns_override

if command -v ipsec >/dev/null 2>&1; then
    configure_strongswan_dns_integration

    if [[ "${STRONGSWAN_DNS_CONFIG_CHANGED:-0}" -eq 1 ]]; then
        restart_or_reload_strongswan
    fi
fi

main_menu
