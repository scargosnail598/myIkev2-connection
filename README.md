# IKEv2 VPN — Ubuntu StrongSwan Server + Windows/Linux Clients

A complete IKEv2 VPN setup built around StrongSwan, EAP-MSCHAPv2 authentication, a private CA, and two Windows traffic modes:

- **Full Tunnel** — all IPv4 traffic goes through the VPN.
- **Proxy Mode** — only the private SOCKS5 proxy endpoint goes through IKEv2; all other Windows traffic stays direct.

The Linux client currently operates as a full-tunnel client.

---

## Included files

| Component | File | Version / Target |
|---|---|---|
| Server | `ikev2-strongswan-ubuntu-v6.sh` | v6.0.0-en / Ubuntu 22.04 & 24.04 |
| Windows client | `ikev2-windows-client-v6.ps1` | v6.0.0 / PowerShell 5.1+ |
| Linux client | `ikev2-linux-client-v1.5.sh` | v1.5.0 / Ubuntu 22.04 & 24.04 |

The server installer manages StrongSwan packages, certificates, users, routing, DNS, NAT, firewall rules, status, uninstall, and the optional private SOCKS5 Proxy Mode.

The Windows utility supports both Full Tunnel and Proxy Mode without requiring separate VPN profiles.

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
chmod +x ikev2-strongswan-ubuntu-v6.sh
```

Run the installer:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.sh install
```

Or run it without arguments:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.sh
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

Copy `ikev2-strongswan-ubuntu-v6.sh` to the server and run:

```bash
chmod +x ikev2-strongswan-ubuntu-v6.sh
sudo ./ikev2-strongswan-ubuntu-v6.sh upgrade
```

Or run:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.sh
```

When an existing managed installation is detected, the menu is:

```text
IKEv2 installation detected

1) Status
2) Upgrade / Configure SOCKS5 Proxy Mode
3) Uninstall
4) Exit
```

Choose:

```text
2) Upgrade / Configure SOCKS5 Proxy Mode
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
sudo ./ikev2-strongswan-ubuntu-v6.sh status
```

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

Every client needs:

- the appropriate client utility
- the server-generated `ca-cert.cer`
- the VPN server IP or hostname
- a VPN username/password created on the server

Example certificate copy:

```bash
scp root@YOUR_SERVER:/root/ikev2-client/ca-cert.cer .
```

Do **not** copy the CA private key to clients.

Keep `ca-cert.cer` next to the client utility.

Windows:

```text
ikev2-windows-client-v6.ps1
ca-cert.cer
```

Linux:

```text
ikev2-linux-client-v1.5.sh
ca-cert.cer
```

---

# 8. Windows client v6

## Requirements

- Windows with built-in VPN PowerShell cmdlets
- PowerShell 5.1+
- Administrator privileges
- `ikev2-windows-client-v6.ps1`
- `ca-cert.cer` in the same directory

The script automatically requests Administrator privileges through UAC when required.

## Run the utility

Open PowerShell in the directory containing the files:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client-v6.ps1
```

At startup, the utility validates the CA certificate and imports it into:

```text
Local Computer / Trusted Root Certification Authorities
```

when required.

The menu is:

```text
IKEv2 Windows VPN Utility v6.0.0
==============================

1) Install / Update IKEv2 VPN
2) Status
3) Connect
4) Disconnect
5) Traffic Mode (Full Tunnel / Proxy Mode)
6) Exit
```

---

# 9. Create a Windows VPN profile

Choose:

```text
1) Install / Update IKEv2 VPN
```

Enter:

```text
VPN profile name
VPN server IP or hostname
```

The server value must exactly match the Server / Remote ID used during server installation.

The utility then asks for the traffic mode:

```text
Traffic Mode
============

1) Full Tunnel
   Route all IPv4 traffic through the VPN.

2) Proxy Mode
   Route only the private SOCKS5 endpoint through IKEv2.
   All other Windows traffic stays DIRECT.
```

The default is:

```text
1) Full Tunnel
```

---

# 10. Windows Full Tunnel mode

Choose:

```text
1) Full Tunnel
```

In this mode:

```text
Windows IPv4 traffic
        |
        v
      IKEv2
        |
        v
   VPN Server
        |
        v
     Internet
```

The Windows VPN profile uses:

```text
Split tunneling: Disabled
```

All IPv4 traffic uses the VPN while connected.

---

# 11. Windows Proxy Mode

Choose:

```text
2) Proxy Mode
```

The client asks:

```text
SOCKS5 proxy IP [10.254.254.1]:
SOCKS5 proxy port [1080]:
```

If the server uses the default v6 settings, simply press Enter for both.

The Windows utility enables split tunneling and installs only this VPN-specific route:

```text
10.254.254.1/32 -> IKEv2
```

All other Windows traffic stays on the normal Internet connection.

The resulting routing model is:

```text
Normal Windows traffic
        |
        +------------------------> ISP / DIRECT


Application using SOCKS5
        |
        v
10.254.254.1:1080
        |
        v
      IKEv2
        |
        v
   VPN Server
        |
        v
     Internet
```

After a successful VPN connection, the utility also checks whether the private SOCKS5 endpoint is reachable.

Expected output:

```text
[+] Checking private SOCKS5 endpoint 10.254.254.1:1080...
SOCKS5 endpoint is reachable through the VPN.
```

---

# 12. Configure an application to use Proxy Mode

Connecting the VPN in Proxy Mode does **not** automatically send applications through the proxy.

Only applications explicitly configured to use:

```text
SOCKS5 10.254.254.1:1080
```

will use the VPN server as their Internet exit.

## Firefox example

Open:

```text
Settings
→ Network Settings
→ Manual proxy configuration
```

Set:

```text
SOCKS Host : 10.254.254.1
Port       : 1080
SOCKS v5   : enabled
```

When available, enable proxy/remote DNS for SOCKS5 to avoid sending those application DNS queries through the normal local resolver.

With this setup:

```text
Firefox -> SOCKS5 -> IKEv2 -> VPN Server -> Internet
```

while applications that are not configured for the SOCKS5 proxy remain direct.

---

# 13. Change Windows traffic mode later

You do not need to recreate the VPN profile.

Run the Windows utility and choose:

```text
5) Traffic Mode (Full Tunnel / Proxy Mode)
```

Select the VPN profile and then choose either:

```text
1) Full Tunnel
```

or:

```text
2) Proxy Mode
```

When switching to Full Tunnel, the utility removes the proxy route it manages and disables split tunneling.

When switching to Proxy Mode, it enables split tunneling and creates the private `/32` route for the SOCKS5 endpoint.

If the VPN is already connected while changing modes, disconnect and reconnect the VPN before relying on the new routing state.

---

# 14. Upgrade an existing Windows v5 profile

Windows v6 can configure the traffic mode of an existing IKEv2 profile.

You normally do not need to delete and recreate the v5 VPN profile.

Place:

```text
ikev2-windows-client-v6.ps1
ca-cert.cer
```

in the same directory and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client-v6.ps1
```

Then choose:

```text
5) Traffic Mode (Full Tunnel / Proxy Mode)
```

Select the existing profile and enable Proxy Mode.

With the default server v6 Proxy Mode:

```text
SOCKS5 host : 10.254.254.1
SOCKS5 port : 1080
```

---

# 15. Windows VPN credentials

The utility uses Windows native EAP credential handling.

When connecting for the first time, Windows may display its native VPN credential dialog.

Enter the VPN username and password configured on the server.

The VPN cryptographic policy applied by the utility is:

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
2) Status
```

In Full Tunnel mode, the status reports the profile as Full Tunnel.

In Proxy Mode, it includes information similar to:

```text
Split tunneling  : True
Traffic mode     : Proxy Mode
SOCKS5 proxy     : 10.254.254.1:1080
VPN-only route   : 10.254.254.1/32
```

---

# 17. Ubuntu/Linux client

The Linux client is designed for Ubuntu 22.04 and 24.04.

Current Linux client version:

```text
1.5.0
```

The Linux client currently uses **Full Tunnel IPv4** and does not implement the Windows Proxy Mode feature.

Keep these files together:

```text
ikev2-linux-client-v1.5.sh
ca-cert.cer
```

Run:

```bash
chmod +x ikev2-linux-client-v1.5.sh
sudo ./ikev2-linux-client-v1.5.sh
```

Menu:

```text
IKEv2 Linux VPN Utility
=======================
Version: 1.5.0

1) Install / Update IKEv2 VPN
2) List / Status
3) Connect
4) Disconnect
5) Remove Profile
6) Uninstall Utility
7) Exit
```

## Create the Linux VPN profile

Choose:

```text
1) Install / Update IKEv2 VPN
```

Enter:

```text
VPN profile name
VPN server IP or hostname
VPN username
VPN password
```

The server value must match the Server / Remote ID used during server installation.

The utility automatically:

- installs required StrongSwan packages when necessary
- validates and installs the VPN CA
- creates the IKEv2 profile
- stores EAP credentials in root-only configuration
- configures full-tunnel IPv4 operation
- handles VPN DNS
- reloads StrongSwan

Linux client crypto profile:

```text
IKEv2
EAP-MSCHAPv2
AES256 / SHA256 / MODP2048
ESP AES256 / SHA256
Full Tunnel IPv4
```

---

# 18. Optional Linux system-wide command

After creating a profile, the Linux utility asks:

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

Use the exact `ca-cert.cer` generated by the server installation.

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
sudo ./ikev2-strongswan-ubuntu-v6.sh status
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
sudo ./ikev2-linux-client-v1.5.sh
```

Choose:

```text
6) Uninstall Utility
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
sudo ./ikev2-strongswan-ubuntu-v6.sh uninstall
```

Or open the interactive menu and choose:

```text
3) Uninstall
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
chmod +x ikev2-strongswan-ubuntu-v6.sh
sudo ./ikev2-strongswan-ubuntu-v6.sh install
```

Enable the private SOCKS5 Proxy Mode when prompted if you plan to use Windows Proxy Mode.

Copy:

```text
/root/ikev2-client/ca-cert.cer
```

to each client.

## Windows

Keep together:

```text
ikev2-windows-client-v6.ps1
ca-cert.cer
```

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client-v6.ps1
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
chmod +x ikev2-linux-client-v1.5.sh
sudo ./ikev2-linux-client-v1.5.sh
```

---

# 25. Quick start — upgrade existing installation to Proxy Mode

## Server

```bash
chmod +x ikev2-strongswan-ubuntu-v6.sh
sudo ./ikev2-strongswan-ubuntu-v6.sh upgrade
```

Accept the defaults unless you need custom values:

```text
Proxy IP   : 10.254.254.1
Proxy Port : 1080
```

Verify:

```bash
sudo ./ikev2-strongswan-ubuntu-v6.sh status
sudo systemctl status ikev2-vpn-proxy
sudo ss -lntp | grep 1080
```

## Windows

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client-v6.ps1
```

Choose:

```text
5) Traffic Mode (Full Tunnel / Proxy Mode)
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
