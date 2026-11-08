# IKEv2 VPN — StrongSwan server, clients and optional Web UI

Install an IKEv2 VPN on Ubuntu with EAP-MSCHAPv2 users, private-CA or public
ACME certificates, and native Windows/Linux clients. Manage it from the terminal
or install the optional Web UI during initial setup or later.

| Component | File | Version / platform |
| --- | --- | --- |
| VPN server | `ikev2-strongswan-ubuntu.sh` | 6.4.9 / Ubuntu 22.04, 24.04 |
| Windows client | `ikev2-windows-client.ps1` | 6.4.7 / PowerShell 5.1+ |
| Linux client | `ikev2-linux-client.sh` | 1.9.8 / Ubuntu 22.04, 24.04 |
| Web UI | `packaging/install-webui.sh` | Python 3 + systemd; optional |

Windows supports Full Tunnel and a VPN-only SOCKS5 Proxy Mode. Linux supports
Full Tunnel and optional Gateway Mode for a selected downstream IPv4 network.
Both clients support optional IPv6 full tunnel when enabled on the server.

## Guides

- [Server installation, prompts, certificates, status and troubleshooting](docs/server.md)
- [Windows/Linux setup, profiles, Proxy Mode and Linux Gateway Mode](docs/clients.md)
- [Web UI setup, SSH/HTTPS access, password reset, 2FA and rollback](docs/webui.md)
- [Per-user traffic limits, expiry, accounting and rollback](docs/user-limits.md)
- [Configuration links, offline QR codes and Android import contract](docs/config-sharing.md)
- [Telegram bot setup and reporting](docs/telegram-bot.md)
- [GitHub Actions production deployment](PRODUCTION_DEPLOYMENT.md)
- [Publish the panel through any CDN](WEBUI_CDN_GUIDE.md)

## Install the server

Use Ubuntu Server **22.04 or 24.04**, root/sudo access, a public IPv4 address or
DNS hostname, and Internet access for packages. Permit inbound **UDP 500 and
4500** in the provider firewall. The installer configures local firewall/NAT
rules; provider rules must be configured separately. IPv6 requires working
server IPv6 connectivity and the equivalent IPv6 UDP rules. Public ACME trust
requires a DNS hostname and reachable TCP 80 for HTTP-01 validation.

For VPN + Web UI, use a full checkout so all panel files are available:

```bash
git clone https://github.com/scargosnail598/myIkev2-connection.git
cd myIkev2-connection
sudo bash ./ikev2-strongswan-ubuntu.sh install
```

Review the server identity, Internet interface, VPN subnet, IPv6, DNS,
certificate mode, Windows compatibility, optional SOCKS5 endpoint and VPN users.
Before the final summary, the installer asks:

```text
Install the Web UI management panel too? [y/N]:
```

- **y**: install the panel after VPN provisioning succeeds; set its separate
  admin password when prompted.
- **Enter / n**: install the VPN and skip the panel. You can add it later.

Private CA mode exports `ca-cert.cer` for client trust. ACME mode uses public
system trust. Keep the server identity consistent with the certificate and
verify CA fingerprints before trusting a private CA.

The VPN installer also works as a standalone release script. If Web UI is
selected, it downloads the matching versioned panel bundle and verifies its
SHA256 checksum before running it. This requires a release produced by the
updated workflow; unreleased/older scripts without a published bundle need a
full checkout. A panel failure leaves the completed VPN installation available
and prints a retry command. See [installation details](docs/webui.md#install-with-working-service-controls).

After installation, client files are normally under `/root/ikev2-client/`.
Protect `client-credentials.txt` and all private keys. Check operation with:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh status
sudo bash ./ikev2-strongswan-ubuntu.sh diagnostics
```

## Install or update the Web UI later

Run this from the server, including on an existing VPN installation:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh webui
```

Or open the main menu and choose **14) Install / Update Web UI**. A full checkout
can also use `sudo bash ./packaging/install-webui.sh install` directly.
Python 3 and curl must be installed. Existing panel credentials are preserved
on update; a new installation uses username **admin** and prompts twice for a
password of at least 12 characters.

From your own computer, create an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 admin@YOUR_SERVER
```

Keep that session open and visit **http://127.0.0.1:8787** locally. The panel
binds to loopback; installation does not open a public management port. For
HTTPS/CDN hosting, follow the [Web UI guide](docs/webui.md) and
[CDN guide](WEBUI_CDN_GUIDE.md). Use `--secure-cookie` only with HTTPS.

The panel provides:

- Service status, confirmed service controls, CPU/RAM/network charts and diagnostics.
- Active sessions and current-session traffic.
- VPN account creation, password changes and deletion.
- Per-account traffic/validity limits and usage resets.
- Configuration links and offline QR codes; password inclusion is off by default.
- Optional TOTP 2FA and Telegram bot settings; password recovery through the server CLI.

To recover panel access from the server terminal:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh reset-webui-password
```

This preserves the panel username, resets the password, disables panel 2FA and
signs out sessions. It requires root/sudo and an installed Web UI. See
[password reset and deployment details](docs/webui.md#install-with-working-service-controls).

## Prepare the panel for a CDN

After installing the Web UI, choose **15) Configure Web UI for CDN**, or run:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh cdn
```

The wizard checks local panel health and asks for the panel hostname, origin
port (default 8080), and current provider egress CIDRs. Read the ranges from a
local file, paste them one per line, or keep the existing list. It validates the
inputs and port availability, shows a summary, and requires confirmation before
saving settings and installing/restarting the proxy. Existing origin, network
and Caddy settings are backed up under `/etc/ikev2-web-proxy/backups/`.

This uses an **HTTP origin**: the CDN-to-server leg is unencrypted. Browser HTTPS
is required. Setup enables Secure cookies and restarts the panel/proxy with
root/sudo privileges; it does not restart the VPN or edit DNS/firewall rules.
Configure the CDN record, disable caching and allow the origin TCP port only
from CDN ranges yourself. See the [CDN guide](WEBUI_CDN_GUIDE.md) for HTTPS
origin alternatives, verification and rollback.

New Web UI installations retain the wizard at
`/opt/ikev2/packaging/setup-cdn-proxy.sh`. The main installer uses the checkout,
then the installed wizard, then a verified matching release bundle if needed.
Update an older Web UI or use a full checkout if the installed helper or release
bundle lacks this feature. If proxy installation fails after confirmation, new
settings remain saved for retry; existing settings are available in the backup.

## Server management

Run `sudo bash ./ikev2-strongswan-ubuntu.sh` without arguments for the menu:

```text
1) Status                         9) Certificate management
2) Service Control               10) Update Installer
3) User Management               11) Uninstall
4) Connected Clients             12) About
5) Diagnostics                   13) Reset Web UI Password
6) Show Last 100 Log Entries      14) Install / Update Web UI
7) Export Client Profile (.ikev)  15) Configure Web UI for CDN
8) SOCKS5 Proxy Mode              16) Exit
```

| Command | Action |
| --- | --- |
| `install` | Fresh interactive VPN setup, with an optional Web UI prompt |
| `webui` | Install/update the panel; accepts `--secure-cookie`, `--password-stdin` |
| `cdn` | Interactively configure the installed panel as an HTTP CDN origin |
| `status`, `diagnostics` | Status and local health checks |
| `certificate`, `renew` | Certificate status or ACME renewal |
| `logs` | Last 100 installer log entries |
| `disconnect [username]` | Disconnect all active sessions for a selected user |
| `upgrade` | Add/update private SOCKS5 Proxy Mode on a managed server |
| `reconnect` | Apply DPD stale-session recovery policy |
| `update` | Update only the server installer from a GitHub Release |
| `start`, `stop`, `restart` | Control StrongSwan |
| `proxy-start`, `proxy-stop`, `proxy-restart` | Control private SOCKS5 |
| `start-all`, `stop-all` | Control managed VPN/firewall/proxy services |
| `reset-webui-password` | Recover panel access from the server terminal |
| `uninstall` | Restore the VPN installer's recorded backups and prior service state |

`upgrade`, installer self-update and panel deployment are separate operations.
Use `upgrade` to add SOCKS5 to an existing managed VPN without regenerating
certificates or replacing users. Panel updates preserve authentication settings.
VPN uninstall does not remove the separately managed Web UI or its quota state.
See [server removal](docs/server.md#uninstall-the-vpn-server) and
[panel rollback](docs/webui.md#install-with-working-service-controls).

Installer logs are root-readable at `/var/log/ikev2-easy-installer.log`.
Connection events are collected after the watcher is enabled. The CLI saves
cumulative per-user traffic in `/var/lib/ikev2-easy-installer/traffic.tsv`
every 30 seconds; this is separate from panel quota accounting. Current-session
counters reset as sessions change. See [traffic limits and accounting](docs/user-limits.md).

## Connect Windows or Linux

### Portable `.ikev` client profiles

Export a user profile from **7) Export Client Profile (.ikev)** on the server.
Transfer `username.ikev` and the appropriate client script privately to the
client computer. CLI exports contain connection settings and public CA material
(or a public-trust marker), never passwords or private keys.

On Windows, run as Administrator:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client.ps1
```

On Ubuntu:

```bash
sudo bash ./ikev2-linux-client.sh
```

Choose **2) Import .ikev Profile**, review the server and trust information,
then connect using the user's VPN password. For manual private-CA setup, keep
`ca-cert.cer` beside the client script. Detailed menus, auto-reconnect and
removal are in the [client guide](docs/clients.md).

Windows **9) Traffic Mode** switches between Full Tunnel and Proxy Mode.
Proxy Mode routes only the private SOCKS5 endpoint through IKEv2; configure
selected applications to use that endpoint and remote DNS. Other traffic stays
direct. The default endpoint is `10.254.254.1:1080`; keep it VPN-only.
Linux can display this proxy metadata but does not configure Proxy Mode.
For downstream routing, see [Linux Gateway Mode](docs/clients.md#linux-gateway--router-mode).

## Optional Telegram bot

Panel installation prepares the bot without enabling an unconfigured account.
Open **Telegram** in the panel to supply the dedicated bot token, allowed numeric
Telegram user IDs, and enable reports. It uses outbound HTTPS polling and needs
no inbound port. Only allowed users receive replies in private chats.

Commands include `/status`, `/sessions [page]`, `/online [page]`, `/usage [page]`,
`/user username` and `/help`. CLI cumulative usage and panel quota accounting
have different semantics. The bot also works independently of the panel;
see [Telegram setup, privacy and removal](docs/telegram-bot.md).

## Security and rollback

Server installation requires root and changes VPN configuration, CA/certificate
material, forwarding, routing, DNS delivery, firewall and NAT. Clients require
admin/root privileges and explicit trust/connection choices. Use disposable VMs
for privileged integration tests.

Keep passwords, private keys, generated profiles with credentials, production
certificates, state backups and unsanitized logs out of Git. Share public CA
certificates only after checking fingerprints. Password-bearing panel links and
QR codes grant account access; they are encoded, not encrypted, and do not expire.

Panel installation creates a dedicated unprivileged app account plus a restricted
root broker and short-lived account/quota helpers. Service controls can disconnect
VPN sessions. Application rollback preserves account/password changes; quota
suspensions need the [explicit limits rollback procedure](docs/user-limits.md#deployment-and-rollback).

## Development and releases

Run from the repository root:

```bash
bash -n ikev2-strongswan-ubuntu.sh ikev2-linux-client.sh
shellcheck --severity=warning ikev2-strongswan-ubuntu.sh ikev2-linux-client.sh packaging/*.sh
python3 -B -m unittest discover -s tests -v
```

Tests use temporary data and mocked privileged commands; some proxy integration
checks require Caddy. See the [Web UI guide](docs/webui.md) for browser checks.
Manually verify provisioning, connect/disconnect, DNS, routing, bad credentials,
bad CA, certificate renewal/rotation and relaunch on supported operating systems.

A suite release uses component tags matching each script's embedded version.
Commit and push changes first, then create all three tags at the same commit.
For the versions currently in this checkout:

```bash
git tag -a linux-v1.9.8 -m "Linux client v1.9.8"
git tag -a windows-v6.4.7 -m "Windows client v6.4.7"
git tag -a server-v6.4.9 -m "Server v6.4.9"
git push origin linux-v1.9.8 windows-v6.4.7
git push origin server-v6.4.9
```

Use new version numbers/tags for subsequent releases. The server tag triggers the
release workflow. It packages the server script, both client scripts,
`ikev2-webui-v<server-version>.tar.gz`, `RELEASE.json` and `SHA256SUMS`.
The Web UI bundle includes only tracked `webui/`, `telegram_bot/` and `packaging/`
files. Local runtime credentials are not packaged.

## License

[MIT](LICENSE).
