# Server guide

[Back to README](../README.md) · [Client guide](clients.md) · [Web UI](webui.md)

## Server requirements

You need:

- Ubuntu Server **22.04** or **24.04**
- root or `sudo` access
- a public IPv4 address or public DNS hostname
- Internet access for package installation
- inbound UDP **500** and **4500** allowed

IPv6 VPN support is optional. To enable it, the server needs working IPv6
Internet connectivity and the provider firewall must allow inbound IPv6 UDP
ports **500** and **4500**. The installer uses a private ULA client pool and
NAT66 for outbound IPv6 traffic.

If the server is behind a cloud firewall or security group, allow:

```text
UDP 500
UDP 4500
```

The installer creates its own local firewall and NAT rules, but it cannot modify an external cloud/provider firewall.

---

## Fresh server installation

Make the installer executable:

```bash
chmod +x ikev2-strongswan-ubuntu.sh
```

Run the installer:

```bash
sudo ./ikev2-strongswan-ubuntu.sh install
```

Or run it without arguments:

```bash
sudo ./ikev2-strongswan-ubuntu.sh
```

The interactive menu includes **About**, which briefly describes the server
installer and displays its version. On managed installations, **13) Reset Web UI
Password** changes the panel password, **14) Install / Update Web UI** installs the
panel, **15) Configure Web UI for CDN** prepares the CDN origin, and **16) Exit** closes the menu.

On a fresh server choose:

```text
1) Install
```

The installer asks **Install the Web UI management panel too? [y/N]** before
its final summary. Accept to install the panel after the VPN passes its checks;
press Enter to skip. Panel setup asks for a separate admin password.
If panel setup fails, the VPN remains installed. Retry with
`sudo bash ./ikev2-strongswan-ubuntu.sh webui`.
See [Web UI installation and access](webui.md#install-with-working-service-controls)
for prerequisites, release bundles, SSH access and rollback.

For an installed panel, run `sudo bash ./ikev2-strongswan-ubuntu.sh cdn` or
choose **15) Configure Web UI for CDN**. The wizard collects the hostname,
HTTP origin port and provider CIDRs, validates them and requests confirmation.
It backs up existing proxy settings and installs/restarts only the panel/proxy;
DNS and firewall setup remain manual. See [CDN setup and rollback](../WEBUI_CDN_GUIDE.md).

### Installation prompts

#### Server address / identity

Enter the public IP address or hostname that clients will use.

Examples:

```text
vpn.example.com
```

or:

```text
203.0.113.10
```

Use this exact same value on Windows and Linux clients.

#### Internet interface

The installer normally detects the default Internet-facing interface automatically.

Unless the server uses custom routing, accept the detected value.

#### VPN client subnet

Default example:

```text
10.10.10.0/24
```

Use a dedicated RFC1918 subnet that does not overlap the server LAN, existing routes, or common client-side networks.

#### Optional IPv6 full tunnel

During installation, answer **Yes** to enable IPv6 VPN support. The installer
creates a random ULA `/64`, enables IPv6 forwarding, and adds managed IPv6
firewall and NAT66 rules. It requires an IPv6 default route on the selected
Internet interface; if that route uses Router Advertisements, the installer
preserves RA reception while forwarding is enabled and restores the previous
setting on uninstall. Keep this option disabled if the host or provider does
not supply working IPv6 connectivity.

Exported `.ikev` profiles advertise whether the server offers IPv6. The Linux
client uses that setting on import; manual Linux profile creation asks whether
to request IPv6. Windows Full Tunnel uses IPv6 when the server negotiates it.
Windows Proxy Mode routes only the IPv4 SOCKS5 endpoint, so other IPv6 traffic
remains direct by design.

#### DNS servers

Enter one or more IPv4 DNS servers separated by commas.

Example:

```text
1.1.1.1,8.8.8.8
```

#### Private CA name

Example:

```text
IKEv2 VPN Root CA
```

The installer generates a private CA and signs the VPN server certificate with it.

#### Public Let’s Encrypt certificate

The installer can instead use a publicly trusted Let’s Encrypt certificate. Choose
`2) Public CA (ACME)` in the certificate mode prompt, then enter a DNS name such as
`vpn.example.com`. The name must resolve to the server and TCP port `80` must be
reachable during the initial HTTP-01 certificate request. An IPv4 address cannot
be used for this mode.

New ACME certificates explicitly use RSA keys (3072 bits), matching the server's
StrongSwan key configuration and native Windows clients.

Certbot renews certificates through its normal systemd timer. The managed deploy
hook validates the certificate's hostname, expiry, server purpose, RSA key, and
certificate/key match before replacing the managed files. It serializes deployments,
keeps temporary backups, reloads both credentials and connection definitions, and
waits until StrongSwan lists the renewed certificate with its private key. A failed
deployment attempts to restore the previous files and reload them. The hook does
not restart the daemon; established VPN sessions remain connected. If StrongSwan
is stopped, the files are installed for its next start without starting the service.

Use `sudo ./ikev2-strongswan-ubuntu.sh renew` for a manual attempt, or `certificate`
for certificate status and renewal actions. After updating the installer on an
existing ACME server, run `renew` once to refresh its installed deploy hook and
apply the current certificate even if renewal is not yet due. If the
existing Certbot key is ECDSA, this command requests an immediate replacement RSA
certificate even when renewal is not yet due; that one-time request uses the ACME
issuance quota. Later RSA renewals follow Certbot's normal renewal schedule. An
invalid or missing Certbot key stops renewal before managed files are changed.

No custom CA file is exported or imported in this mode; supported clients use the
operating system's public trust store. Verify an upgrade or renewal on a disposable
Ubuntu VM, including a new Windows/Linux connection and an existing VPN session.

#### Windows compatibility

The server offers optional stock Windows compatibility.

When using the included Windows v6 utility, the normal secure profile is recommended because the utility explicitly applies:

```text
IKE encryption : AES256
IKE integrity  : SHA256
DH group       : Group14
ESP cipher     : AES256
ESP auth       : SHA256128
PFS            : None
```

Enable stock Windows compatibility only when you need default/manual Windows clients that do not use the included PowerShell utility.

#### Private SOCKS5 Proxy Mode

Server v6 can optionally install a private Dante SOCKS5 proxy.

The installer asks:

```text
Enable private SOCKS5 Proxy Mode? [Y/n]
```

The defaults are:

```text
Proxy IP   : 10.254.254.1
Proxy Port : 1080
```

The proxy IP is a dedicated `/32` address and must be outside the VPN client subnet.

For example:

```text
VPN subnet : 10.10.10.0/24
Proxy IP   : 10.254.254.1
```

The proxy listener is intended to be reachable only through the IKEv2 VPN.

#### VPN users

Create at least one VPN username/password.

If the password is left empty, the installer generates a random password.

Multiple VPN users can be created during installation.

On an existing managed installation, open `User Management` from the main
menu to list users, add a user, change a password, or remove a user without
reinstalling the VPN. User credentials remain in `/etc/ipsec.secrets`.
The menu derives each user's current `Online` or `Offline` state from
StrongSwan when the view is rendered. If StrongSwan is stopped, users appear
offline; if its active-session status cannot be queried, they appear as
`Unknown`.
StrongSwan rereads the secrets after each change without restarting the VPN
service. Manually entered passwords must contain at least 12 characters;
generated passwords are shown once.

`Connected Clients` provides a read-only view of the current `ikev2-eap`
sessions directly from StrongSwan runtime state. It shows each authenticated
username and, when available, the assigned VPN IP, public peer IP, and
connection age. It shows current-session RX/TX traffic and cumulative per-user totals.
Cumulative counters are saved in `/var/lib/ikev2-easy-installer/traffic.tsv`
every 30 seconds by `ikev2-easy-installer-traffic-snapshot.timer`; they survive
service/server restarts. Missing counters show `N/A`.

Before installation begins, review the summary and confirm with `Y`.

---

## Server installation result

A successful installation ends with:

```text
================ IKEv2 VPN READY ================
```

Client files are normally exported to:

```text
/root/ikev2-client/
```

Important files include:

```text
ca-cert.cer
ca-cert.pem
server-cert.pem
client-info.txt
client-credentials.txt
```

### Portable `.ikev` Client Profiles

The server can export a configured user as the project's portable `.ikev`
VPN profile format. Version 1 is a JSON document identified by
`format = ikev-profile` and `version = 1`. It is designed for later import
by the Windows and Linux clients. The included Linux and Windows clients import this same
format.

Private-CA profiles embed the public CA certificate as single-line Base64-encoded
DER and include its OpenSSL SHA-256 fingerprint. Let’s Encrypt profiles instead
contain `"certificate_trust": "public"` and do not embed a CA certificate. VPN
passwords, private keys, and other secret material are never included in CLI
file exports. Web UI sharing can optionally include a password in a separate
envelope; see [configuration sharing](config-sharing.md). CLI profiles are written with mode
`0600` under:

```text
/root/ikev2-client/profiles/
```

Example with deliberately fake certificate data:

```json
{
  "format": "ikev-profile",
  "version": 1,
  "name": "user@vpn.example.com",
  "server": "vpn.example.com",
  "remote_id": "vpn.example.com",
  "username": "user",
  "authentication": "eap-mschapv2",
  "ca_certificate": {
    "encoding": "der-base64",
    "data": "RkFLRS1ERVItQ0VSVElGSUNBVEUtREFUQQ==",
    "sha256": "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
  },
  "connection": {
    "mode": "full-tunnel"
  },
  "server_profile": "secure",
  "proxy": {
    "enabled": true,
    "type": "socks5",
    "host": "10.254.254.1",
    "port": 1080
  }
}
```

When Proxy Mode is unavailable, `proxy` contains only
`"enabled": false`. Exporting a profile does not reload StrongSwan or modify
VPN, user, firewall, NAT, certificate, or proxy configuration.

For the included Windows and Linux clients, the main certificate file is:

```text
ca-cert.cer
```

Protect:

```text
client-credentials.txt
```

because it contains VPN usernames and passwords.

If Proxy Mode is enabled, `client-info.txt` also contains the private SOCKS5 endpoint information.

---

## Upgrade an existing managed server to v6

If an older installation was created by this installer family, you do **not** need to uninstall and reinstall the VPN.

Copy `ikev2-strongswan-ubuntu.sh` to the server and run:

```bash
chmod +x ikev2-strongswan-ubuntu.sh
sudo ./ikev2-strongswan-ubuntu.sh upgrade
```

Or run:

```bash
sudo ./ikev2-strongswan-ubuntu.sh
```

When an existing managed installation is detected, the menu is:

```text
IKEv2 installation detected

1) Status
2) Service Control
3) User Management
4) Connected Clients
5) Diagnostics
6) Show Last 100 Log Entries
7) Export Client Profile (.ikev)
8) SOCKS5 Proxy Mode
9) Certificate management
10) Update Installer
11) Uninstall
12) About
13) Reset Web UI Password
14) Install / Update Web UI
15) Configure Web UI for CDN
16) Exit
```

Choose:

```text
8) SOCKS5 Proxy Mode
```

Recommended defaults:

```text
Private SOCKS5 proxy IP  : 10.254.254.1
Private SOCKS5 proxy port: 1080
```

The v6 upgrade path is intentionally isolated.

It does **not**:

```text
- regenerate the CA
- regenerate the server certificate
- rewrite VPN users or passwords
- replace /etc/ipsec.conf
- replace /etc/ipsec.secrets
- restart StrongSwan
- modify the existing full-tunnel NAT/firewall service
```

It only installs or updates the private SOCKS5 Proxy Mode.

Successful upgrade output includes:

```text
================ V6 PROXY MODE READY ================

SOCKS5 host : 10.254.254.1
SOCKS5 port : 1080
Access      : IKEv2 clients only
StrongSwan  : unchanged / not restarted by this upgrade
```

---

## Update the installer

Choose **10) Update Installer** or run `sudo bash ./ikev2-strongswan-ubuntu.sh update`.
Updates come from published server Releases. A newer script on main is not an
available release; if your script is ahead of the published version, the
updater reports this and keeps your current script. The menu reloads after a
successful replacement. The CLI command completes without opening a menu.
See [release/update behavior](../README.md#how-server-updates-are-published)
for checksums, backups, rollback and separate panel updates.

## Check server status

Run:

```bash
sudo ./ikev2-strongswan-ubuntu.sh status
```

Run the read-only server health check from the installed-system menu or with:

```bash
sudo ./ikev2-strongswan-ubuntu.sh diagnostics
```

Diagnostics checks the managed state, services, IKEv2 connection, forwarding,
firewall/NAT rules, certificates and keys, secrets permissions, VPN users,
network interface, VPN subnet, optional Proxy Mode, and active-user status. It
reports `HEALTHY`, `WARNING`, or `FAILED` without changing the server.

For StrongSwan details:

```bash
sudo ipsec statusall
```

For StrongSwan logs:

```bash
sudo journalctl -u strongswan-starter -n 100 --no-pager
```

If Proxy Mode is enabled, check its service:

```bash
sudo systemctl status ikev2-vpn-proxy
```

Check the SOCKS5 listener:

```bash
sudo ss -lntp | grep 1080
```

With the default configuration it should listen on:

```text
10.254.254.1:1080
```

It should not be exposed as a public `0.0.0.0:1080` listener.

---

## How Proxy Mode works

Proxy Mode uses IKEv2 as the secure transport to a private SOCKS5 service on the VPN server.

With the default configuration:

```text
Application
    |
    | SOCKS5
    v
10.254.254.1:1080
    |
    | IKEv2
    v
Ubuntu VPN Server
    |
    | Dante SOCKS5
    v
Internet
```

The server-side proxy uses Dante and supports SOCKS5 TCP `CONNECT`.

The generated firewall rules permit access to the proxy from the configured VPN subnet only when the packet is received through IPsec, and deny other access to the private proxy endpoint.

The proxy itself does not use a second username/password layer. Access is protected by the IKEv2 authentication and VPN-only firewall policy.

---

## Verify the VPN

After connecting from Windows or Linux, check the server:

```bash
sudo ipsec statusall
```

You should see an established IKEv2 Security Association for the connected client.

### Full Tunnel verification

In Full Tunnel mode, the client's public IPv4 address should appear as the VPN server's public address.

### Proxy Mode verification

In Windows Proxy Mode:

- an application configured for `10.254.254.1:1080` should exit through the VPN server
- applications not configured to use the SOCKS5 proxy should continue using the normal ISP connection

This makes it possible for two applications on the same Windows machine to use different Internet exit paths.

---

## Troubleshooting

### VPN cannot connect

Verify UDP 500 and 4500 through every firewall between the client and server.

On the server:

```bash
sudo ss -lunp | grep -E ':(500|4500)\b'
```

Also verify any cloud firewall or provider security group.

### Certificate or trust error

For manual Linux setup, use the exact `ca-cert.cer` generated by the server.
For `.ikev` import, the Linux utility validates the embedded CA certificate
and requires its SHA-256 fingerprint to match before asking for trust.

Do not reuse a CA certificate generated by another VPN server.

Linux check:

```bash
sudo ipsec listcacerts
```

### Authentication failed

Verify:

- server address / Remote ID
- VPN username
- VPN password
- correct CA certificate

### Windows cannot connect

Use the included PowerShell client rather than manually creating a default Windows VPN profile.

The utility applies the explicit IPsec policy expected by the secure server configuration.

### Windows Proxy Mode connects but SOCKS5 is unreachable

First check the Windows utility status.

The expected route is:

```text
10.254.254.1/32 -> VPN
```

Then check the server:

```bash
sudo ./ikev2-strongswan-ubuntu.sh status
```

Check the proxy service:

```bash
sudo systemctl status ikev2-vpn-proxy
```

Check the listener:

```bash
sudo ss -lntp | grep 1080
```

With default settings, expect:

```text
10.254.254.1:1080
```

Check proxy logs if required:

```bash
sudo journalctl -u ikev2-vpn-proxy -n 100 --no-pager
```

### Proxy-enabled application still shows the normal ISP IP

Verify that the application is actually configured to use:

```text
SOCKS5 10.254.254.1:1080
```

Merely connecting the VPN in Proxy Mode does not automatically proxy applications.

### Linux connects but DNS is incorrect

Check:

```bash
sudo journalctl -u strongswan-starter -n 100 --no-pager
```

The Linux utility uses native StrongSwan/systemd-resolved integration when suitable. Otherwise, it manages the VPN DNS configuration itself and restores the previous resolver state after disconnect.

---

## Uninstall the VPN server

Run:

```bash
sudo ./ikev2-strongswan-ubuntu.sh uninstall
```

Or open the interactive menu and choose:

```text
11) Uninstall
```

on a managed server.

The installer restores the managed backups and previous service/runtime state that it recorded.

If Proxy Mode was added by v6, the uninstall also handles the managed proxy configuration and service.

The optional Web UI is managed separately: VPN uninstall does not remove its
application, authentication config, quota state, Telegram settings or systemd units.
See [Web UI disable and rollback](webui.md#install-with-working-service-controls)
and [limits rollback](user-limits.md#deployment-and-rollback) before removal.

Packages installed by the installer are removed only when the installer's safe package-removal check allows it.

---

## Security notes

- Keep the CA private key on the VPN server.
- Distribute only `ca-cert.cer` or `ca-cert.pem` to clients.
- Protect `client-credentials.txt`.
- Use strong VPN passwords.
- Keep Ubuntu, StrongSwan, and installed packages updated.
- Use the same server hostname/IP consistently.
- The client address must match the identity in the server certificate.
- Avoid VPN subnets that overlap server or client networks.
- The SOCKS5 endpoint is designed to be reachable through IKEv2 only.
- Do not expose TCP port `1080` directly on the public Internet.
- Proxy Mode only affects applications explicitly configured to use the SOCKS5 endpoint.
- Use proxy/remote DNS in SOCKS-capable applications when appropriate to reduce DNS leakage outside the proxy path.

---
