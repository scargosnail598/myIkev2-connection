# IKEv2 VPN — Ubuntu StrongSwan Server + Native Clients

A complete IKEv2 VPN setup built around StrongSwan, EAP-MSCHAPv2 authentication, a private CA, and native Windows, Linux, and Android clients. Windows supports two traffic modes:

- **Full Tunnel** — all IPv4 traffic goes through the VPN.
- **Proxy Mode** — only the private SOCKS5 proxy endpoint goes through IKEv2; all other Windows traffic stays direct.

The Linux client currently operates as a full-tunnel client.

---

## Included files

| Component | File | Version / Target |
|---|---|---|
| Server | `ikev2-strongswan-ubuntu-v6.1.1.sh` | v6.1.1-en / Ubuntu 22.04 & 24.04 |
| Windows client | `ikev2-windows-client-v6.1.ps1` | v6.1.0 / PowerShell 5.1+ |
| Linux client | `ikev2-linux-client-v1.6.sh` | v1.6.0 / Ubuntu 22.04 & 24.04 |
| Android client | [`android-client/`](android-client/) | v1.0.0 / Android 11+ (API 30+) |

The server installer manages StrongSwan packages, certificates, users, routing, DNS, NAT, firewall rules, status, uninstall, and the optional private SOCKS5 Proxy Mode.

The Windows utility supports both Full Tunnel and Proxy Mode without requiring separate VPN profiles.

The **Android Client v1.0.0** is a native Android IKEv2 client for Android 11+
(API 30+). It provisions a single IPv4 full-tunnel profile through Android's
platform VPN stack:

```text
Android App
    ↓
VpnManager / Ikev2VpnProfile
    ↓
Android platform IKEv2/IPsec stack
    ↓
strongSwan server
```

It uses EAP-MSCHAPv2 and validates the server with the imported private CA. It
does not implement IKEv2, intercept traffic, or use `VpnService`. Android v1.0
supports profile provisioning, connect/disconnect, basic status, and diagnostics;
it does not support Proxy Mode, split tunneling, multiple profiles, or automatic
connection.

The app needs only the server hostname or IPv4 address, EAP username/password,
and `ca-cert.cer`. **Never transfer a server or CA private key to Android.** See
the [Android build and release guide](android-client/README.md) and
[release acceptance matrix](android-client/RELEASE_TESTING.md).

---

# 1. Server requirements

You need:

- Ubuntu Server **22.04** or **24.04**
- root or `sudo` access
- a public IPv4 address or public DNS hostname
- Internet access for package installation
- inbound UDP **500** and **4500** allowed

If the server is behind a cloud firewall or security group, allow:

```text
UDP 500
UDP 4500
```

The installer creates its own local firewall and NAT rules, but it cannot modify an external cloud/provider firewall.

---

# 2. Fresh server installation

Make the installer executable:

```bash
chmod +x ikev2-strongswan-ubuntu-v6.1.1.sh
```

Run the installer:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh install
```

Or run it without arguments:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh
```

On a fresh server choose:

```text
1) Install
```

## Installation prompts

### Server address / identity

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

### Internet interface

The installer normally detects the default Internet-facing interface automatically.

Unless the server uses custom routing, accept the detected value.

### VPN client subnet

Default example:

```text
10.10.10.0/24
```

Use a dedicated RFC1918 subnet that does not overlap the server LAN, existing routes, or common client-side networks.

### DNS servers

Enter one or more IPv4 DNS servers separated by commas.

Example:

```text
1.1.1.1,8.8.8.8
```

### Private CA name

Example:

```text
IKEv2 VPN Root CA
```

The installer generates a private CA and signs the VPN server certificate with it.

### Windows compatibility

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

### Private SOCKS5 Proxy Mode

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

### VPN users

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
connection age. It does not retain session history or collect accounting or
traffic data.

Before installation begins, review the summary and confirm with `Y`.

---

# 3. Server installation result

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

## Portable `.ikev` Client Profiles

The server can export a configured user as the project's portable `.ikev`
VPN profile format. Version 1 is a JSON document identified by
`format = ikev-profile` and `version = 1`. It is designed for later import
by the Windows, Linux, and Android clients. Linux v1.6 is the first importer;
Windows and Android import support will follow separately.

The profile embeds the public CA certificate as single-line Base64-encoded DER
and includes its OpenSSL SHA-256 fingerprint. VPN passwords, private keys, and
other secret material are never included. Profiles are written with mode
`0600` under:

```text
/root/ikev2-client/profiles/
```

Example with deliberately fake certificate data:

```json
{
  "format": "ikev-profile",
  "version": 1,
  "name": "saeed@vpn.example.com",
  "server": "vpn.example.com",
  "remote_id": "vpn.example.com",
  "username": "saeed",
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

# 4. Upgrade an existing managed server to v6

If an older installation was created by this installer family, you do **not** need to uninstall and reinstall the VPN.

Copy `ikev2-strongswan-ubuntu-v6.1.1.sh` to the server and run:

```bash
chmod +x ikev2-strongswan-ubuntu-v6.1.1.sh
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh upgrade
```

Or run:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh
```

When an existing managed installation is detected, the menu is:

```text
IKEv2 installation detected

1) Status
2) Service Control
3) User Management
4) Connected Clients
5) Diagnostics
6) Export Client Profile (.ikev)
7) Upgrade / Configure SOCKS5 Proxy Mode
8) Uninstall
9) Exit
```

Choose:

```text
7) Upgrade / Configure SOCKS5 Proxy Mode
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

# 5. Check server status

Run:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh status
```

Run the read-only server health check from the installed-system menu or with:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.2.0.sh diagnostics
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

# 6. How Proxy Mode works

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

# 7. Prepare client files

Manual client setup needs:

- the appropriate client utility
- the server-generated `ca-cert.cer`
- the VPN server IP or hostname
- a VPN username/password created on the server

Portable `.ikev` setup needs the client utility, the exported `username.ikev`,
and the user's VPN password. The public CA and server settings are embedded in
the profile.

Example certificate copy:

```bash
scp root@YOUR_SERVER:/root/ikev2-client/ca-cert.cer .
```

Do **not** copy the CA private key to clients. For manual setup, keep
`ca-cert.cer` next to the client utility. Portable `.ikev` profiles embed the
public CA certificate and do not require a separate certificate file.

Windows manual setup:

```text
ikev2-windows-client-v6.1.ps1
ca-cert.cer
```

Windows portable-profile import:

```text
ikev2-windows-client-v6.1.ps1
username.ikev
```

Linux manual setup:

```text
ikev2-linux-client-v1.6.sh
ca-cert.cer
```

Linux portable-profile import:

```text
ikev2-linux-client-v1.6.sh
username.ikev
```

---

# 8. Windows client v6.1

## Requirements

- Windows with built-in VPN PowerShell cmdlets
- PowerShell 5.1+
- Administrator privileges
- `ikev2-windows-client-v6.1.ps1`
- either `ca-cert.cer` for manual setup or an exported `username.ikev` for portable setup

The script automatically requests Administrator privileges through UAC when required.

## Run the utility

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client-v6.1.ps1
```

A sibling `ca-cert.cer` is still validated and trusted at startup when present.
When no local `.cer` exists, v6.1 starts normally and displays:

```text
Trusted CA : no local CA loaded
Credentials: Windows native EAP dialog
```

This allows a directory containing only the script and `username.ikev` to
provision the VPN. After a successful portable import, the menu displays the
embedded CA's name as utility status.

The menu is:

```text
IKEv2 Windows VPN Utility v6.1.0
===============================

1) Install / Update IKEv2 VPN
2) Import .ikev Profile
3) Status
4) Connect
5) Disconnect
6) Traffic Mode (Full Tunnel / Proxy Mode)
7) Exit
```

---

# 9. Create or import a Windows VPN profile

## Manual setup

Keep together:

```text
ikev2-windows-client-v6.1.ps1
ca-cert.cer
```

Choose `Install / Update IKEv2 VPN`, enter the connection name and server, and
select Full Tunnel or Proxy Mode. The server value must exactly match the
Server / Remote ID used during server installation.

If no sibling CA exists, manual setup stops with guidance to add
`ca-cert.cer` or use portable import.

## Import a portable `.ikev` profile

Keep together:

```text
ikev2-windows-client-v6.1.ps1
username.ikev
```

Then:

1. Export `username.ikev` from the server.
2. Copy it to Windows.
3. Run the Windows client as Administrator.
4. Choose `Import .ikev Profile`.
5. Enter the profile path.
6. Verify the server and CA SHA-256 fingerprint.
7. Trust the profile.
8. Choose Full Tunnel or Proxy Mode when Proxy Mode is advertised.
9. Connect.
10. Enter the displayed username and its password in the native Windows EAP dialog.

The importer uses native `Get-Content -Raw` and `ConvertFrom-Json`, validates
the frozen schema version 1 fields, decodes the embedded DER certificate with
`.NET`, verifies CA Basic Constraints and validity dates, and calculates the
SHA-256 fingerprint over the DER bytes before offering trust.

**`.ikev` files never contain VPN passwords or private keys.** Windows retains
its native EAP credential handling; the importer does not use Credential
Manager, `cmdkey`, or a custom password store.

Windows v6.1 currently requires imported `remote_id` to match `server` because
the PowerShell/RAS profile API used here has no safe independent equivalent of
StrongSwan's `rightid` setting. Profiles where they differ are rejected.

---

# 10. Windows Full Tunnel mode

Full Tunnel is the default for manual setup and portable import. The created
All-Users IKEv2/EAP profile has split tunneling disabled, so all IPv4 traffic
uses the VPN while connected.

```text
Windows IPv4 traffic -> IKEv2 -> VPN Server -> Internet
```

If an imported profile advertises `proxy.enabled = false`, Full Tunnel is
selected automatically.

---

# 11. Windows Proxy Mode

For manual setup, Proxy Mode continues to prompt for the SOCKS5 IP and port.
For portable import, Proxy Mode is offered only when validated SOCKS5 metadata
is present, and the imported host and port are used without another prompt.

With the default server settings, the client enables split tunneling and adds
only this utility-managed VPN route:

```text
10.254.254.1/32 -> IKEv2
SOCKS5 endpoint: 10.254.254.1:1080
All other Windows traffic: DIRECT
```

No default VPN route is added in Proxy Mode. After connection, the utility
checks whether the private SOCKS5 endpoint is reachable.

---

# 12. Configure an application to use Proxy Mode

Connecting the VPN in Proxy Mode does **not** automatically proxy applications.
Configure only selected SOCKS5-capable applications with the endpoint shown by
the utility. For the default server configuration:

```text
SOCKS5 10.254.254.1:1080
```

When available, enable remote/proxy DNS in the application to reduce DNS
leakage outside the proxy path.

---

# 13. Change Windows traffic mode later

Run the utility and choose:

```text
6) Traffic Mode (Full Tunnel / Proxy Mode)
```

The existing `Apply-TrafficMode` path removes utility-managed stale Proxy Mode
state when switching to Full Tunnel and creates only the selected `/32` route
when switching to Proxy Mode. If connected, disconnect and reconnect before
relying on the changed routing state.

---

# 14. Upgrade an existing Windows profile

Windows v6.1 can configure the traffic mode of existing IKEv2 profiles. Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client-v6.1.ps1
```

Then choose option 6 and select the existing profile. Manual creation still
uses `ca-cert.cer`; portable import uses its embedded CA.

---

# 15. Windows VPN credentials and IPsec policy

Windows owns VPN credential entry. On the first imported-profile connection,
the utility displays the imported username and opens the native EAP credential
dialog when saved credentials are unavailable. No password is read or stored
by the PowerShell importer.

Both manual and imported profiles use the unchanged policy:

```text
IKE encryption : AES256
IKE integrity  : SHA256
DH group       : Group14
ESP cipher     : AES256
ESP auth       : SHA256128
PFS            : None
```

---

# 16. Windows status

Choose:

```text
3) Status
```

Full Tunnel reports split tunneling disabled. Proxy Mode reports split
tunneling enabled, its saved SOCKS5 endpoint, and the single VPN-only `/32`
route.

---

# 17. Ubuntu/Linux client

The Linux client is designed for Ubuntu 22.04 and 24.04.

Current Linux client version:

```text
1.6.0
```

Linux uses **Full Tunnel IPv4**. Version 1.6 can display advertised SOCKS5
Proxy Mode metadata during import, but it does not configure or use the proxy,
add proxy routes, or enable split tunneling.

Run:

```bash
chmod +x ikev2-linux-client-v1.6.sh
sudo ./ikev2-linux-client-v1.6.sh
```

Menu:

```text
IKEv2 Linux VPN Utility
=======================
Version: 1.6.0

1) Install / Update IKEv2 VPN
2) Import .ikev Profile
3) List / Status
4) Connect
5) Disconnect
6) Remove Profile
7) Uninstall Utility
8) Exit
```

## Manual setup

Keep these files together:

```text
ikev2-linux-client-v1.6.sh
ca-cert.cer
```

Choose:

```text
1) Install / Update IKEv2 VPN
```

Enter the profile name, server IP or hostname, VPN username, and VPN password.
The server value must match the Server / Remote ID used during server
installation.

The unchanged manual workflow installs the required StrongSwan packages,
validates and installs the sibling CA certificate, writes root-only EAP
credentials, creates the full-tunnel profile, preserves the existing DNS
integration, and reloads StrongSwan.

## Import a portable `.ikev` profile

The easier v1.6 workflow is:

1. Export `username.ikev` on the VPN server.
2. Copy the `.ikev` file to Ubuntu.
3. Run the Linux client.
4. Choose `Import .ikev Profile`.
5. Enter the path to the file.
6. Review the server, remote identity, username, CA fingerprint, and proxy
   availability.
7. Confirm trust and enter the VPN password.
8. Connect.

Linux v1.6 supports `.ikev` schema version 1 only. It parses JSON with
Python 3's standard-library `json` module, verifies the embedded DER CA and
SHA-256 fingerprint, and then uses the same StrongSwan profile, secrets, DNS,
reload, and connection architecture as manual setup.

**`.ikev` files never contain the VPN password or private keys.** The
importer asks for the password using hidden input and stores it only through
the existing root-only StrongSwan secrets mechanism. The original `.ikev`
file and embedded Base64 data are not copied into managed runtime state.

Linux v1.6 uses one managed VPN CA at a time. A profile using the existing CA
reuses it. A different CA is rejected while managed profiles exist because
silently replacing trust could break them.

Linux client crypto policy remains:

```text
IKEv2
EAP-MSCHAPv2
AES256 / SHA256 / MODP2048
ESP AES256 / SHA256
Full Tunnel IPv4
```

---

# 18. Optional Linux system-wide command

After creating a profile through manual setup, the Linux utility asks:

```text
Install this utility system-wide as the 'ikev2' command? [y/N]
```

If you answer `y`, it installs under:

```text
/opt/ikev2-client/
```

and creates:

```text
/usr/local/bin/ikev2
```

You can then run:

```bash
sudo ikev2
```

`.ikev` import also works through this installed command. The selected
profile may remain anywhere readable on the machine and is not copied into
`/opt/ikev2-client`.

---

# 19. Verify the VPN

After connecting from Windows or Linux, check the server:

```bash
sudo ipsec statusall
```

You should see an established IKEv2 Security Association for the connected client.

## Full Tunnel verification

In Full Tunnel mode, the client's public IPv4 address should appear as the VPN server's public address.

## Proxy Mode verification

In Windows Proxy Mode:

- an application configured for `10.254.254.1:1080` should exit through the VPN server
- applications not configured to use the SOCKS5 proxy should continue using the normal ISP connection

This makes it possible for two applications on the same Windows machine to use different Internet exit paths.

---

# 20. Troubleshooting

## VPN cannot connect

Verify UDP 500 and 4500 through every firewall between the client and server.

On the server:

```bash
sudo ss -lunp | grep -E ':(500|4500)\b'
```

Also verify any cloud firewall or provider security group.

## Certificate or trust error

For manual Linux setup, use the exact `ca-cert.cer` generated by the server.
For `.ikev` import, the Linux utility validates the embedded CA certificate
and requires its SHA-256 fingerprint to match before asking for trust.

Do not reuse a CA certificate generated by another VPN server.

Linux check:

```bash
sudo ipsec listcacerts
```

## Authentication failed

Verify:

- server address / Remote ID
- VPN username
- VPN password
- correct CA certificate

## Windows cannot connect

Use the included PowerShell client rather than manually creating a default Windows VPN profile.

The utility applies the explicit IPsec policy expected by the secure server configuration.

## Windows Proxy Mode connects but SOCKS5 is unreachable

First check the Windows utility status.

The expected route is:

```text
10.254.254.1/32 -> VPN
```

Then check the server:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh status
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

## Proxy-enabled application still shows the normal ISP IP

Verify that the application is actually configured to use:

```text
SOCKS5 10.254.254.1:1080
```

Merely connecting the VPN in Proxy Mode does not automatically proxy applications.

## Linux connects but DNS is incorrect

Check:

```bash
sudo journalctl -u strongswan-starter -n 100 --no-pager
```

The Linux utility uses native StrongSwan/systemd-resolved integration when suitable. Otherwise, it manages the VPN DNS configuration itself and restores the previous resolver state after disconnect.

---

# 21. Uninstall the Linux client utility

Run the Linux utility:

```bash
sudo ./ikev2-linux-client-v1.6.sh
```

Choose:

```text
7) Uninstall Utility
```

The uninstall removes utility-managed:

- VPN profiles
- stored VPN credentials
- CA certificate
- managed DNS state
- system-wide `ikev2` command
- `/opt/ikev2-client` installation

StrongSwan packages are kept installed.

Unrelated StrongSwan configuration is not removed.

---

# 22. Uninstall the VPN server

Run:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh uninstall
```

Or open the interactive menu and choose:

```text
8) Uninstall
```

on an installed v6 system.

The installer restores the managed backups and previous service/runtime state that it recorded.

If Proxy Mode was added by v6, the uninstall also handles the managed proxy configuration and service.

Packages installed by the installer are removed only when the installer's safe package-removal check allows it.

---

# 23. Security notes

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

# 24. Quick start — fresh installation

## Server

```bash
chmod +x ikev2-strongswan-ubuntu-v6.1.1.sh
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh install
```

Enable the private SOCKS5 Proxy Mode when prompted if you plan to use Windows Proxy Mode.

For manual setup, copy:

```text
/root/ikev2-client/ca-cert.cer
```

For portable setup, export and copy the user's `username.ikev` instead.

## Windows

Manual setup:

```text
ikev2-windows-client-v6.1.ps1
ca-cert.cer
```

Portable setup:

```text
ikev2-windows-client-v6.1.ps1
username.ikev
```

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client-v6.1.ps1
```

Choose either:

```text
Full Tunnel
```

or:

```text
Proxy Mode
SOCKS5 10.254.254.1:1080
```

## Linux

```bash
chmod +x ikev2-linux-client-v1.6.sh
sudo ./ikev2-linux-client-v1.6.sh
```

---

# 25. Quick start — upgrade existing installation to Proxy Mode

## Server

```bash
chmod +x ikev2-strongswan-ubuntu-v6.1.1.sh
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh upgrade
```

Accept the defaults unless you need custom values:

```text
Proxy IP   : 10.254.254.1
Proxy Port : 1080
```

Verify:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh status
sudo systemctl status ikev2-vpn-proxy
sudo ss -lntp | grep 1080
```

## Windows

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client-v6.1.ps1
```

Choose:

```text
6) Traffic Mode (Full Tunnel / Proxy Mode)
```

Then:

```text
2) Proxy Mode
```

Use:

```text
SOCKS5 host : 10.254.254.1
SOCKS5 port : 1080
```

Disconnect and reconnect the VPN if it was connected while changing traffic mode.

Finally, configure only the desired applications to use:

```text
SOCKS5 10.254.254.1:1080
```

---

# License

This project is available under the [MIT License](LICENSE).
