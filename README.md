# IKEv2 VPN — Ubuntu StrongSwan Server + Native Clients

A complete IKEv2 VPN setup built around StrongSwan, EAP-MSCHAPv2 authentication, a private CA, and native Windows and Linux clients. Windows supports two traffic modes:

- **Full Tunnel** — all IPv4 traffic and, when enabled on the server, IPv6 traffic go through the VPN.
- **Proxy Mode** — only the private IPv4 SOCKS5 proxy endpoint goes through IKEv2; other Windows traffic, including IPv6, stays direct.

The Linux client supports IPv4 full tunnel and optional IPv6 full tunnel.

## Release tags

Every release tags all three components at the same commit. Push the Linux and
Windows tags first, then push the server tag to trigger one complete GitHub
Release. The workflow verifies all three tags and packages the server, Linux
client, and Windows client together.

For the current versions:

```bash
git tag -a linux-v1.9.8 -m "Linux client v1.9.8"
git tag -a windows-v6.4.7 -m "Windows client v6.4.7"
git tag -a server-v6.4.7 -m "Server v6.4.7"

# Publish component tags first; the server tag triggers the release workflow.
git push origin linux-v1.9.8 windows-v6.4.7
git push origin server-v6.4.7
```

All tags must point to the same commit and match their embedded component
versions. The resulting release includes the server installer, both generic
client scripts, `RELEASE.json`, and `SHA256SUMS`.

Commit and push the release changes to `main` before creating these tags.

---

## Included files

| Component | File | Version / Target |
|---|---|---|
| Server | `ikev2-strongswan-ubuntu.sh` | v6.4.9 / Ubuntu 22.04 & 24.04 |
| Windows client | `ikev2-windows-client.ps1` | v6.4.7 / PowerShell 5.1+ |
| Linux client | `ikev2-linux-client.sh` | v1.9.8 / Ubuntu 22.04 & 24.04 |

The server installer manages StrongSwan packages, certificates, users, routing, DNS, NAT, firewall rules, status, uninstall, and the optional private SOCKS5 Proxy Mode.

Installer activity is recorded in the root-readable log file `/var/log/ikev2-easy-installer.log`.
Show the last 100 entries with:

```bash
sudo ./ikev2-strongswan-ubuntu.sh logs
```

Successful VPN authentication, IKEv2 session establishment, and session
disconnection events are also copied from the StrongSwan journal into this log.
On an existing installation, run any installer command once (for example
`sudo ./ikev2-strongswan-ubuntu.sh status`) to enable the event watcher.
Only events after the watcher is enabled are added; older events remain in the
system journal if they have not expired.

To disconnect one user's active VPN session without restarting StrongSwan:

```bash
sudo ./ikev2-strongswan-ubuntu.sh disconnect username
```

Without a username, the installer opens a user-selection prompt. If the user
has multiple active sessions, all of that user's sessions are disconnected.

The `Connected VPN Clients` view also shows each active user's received (RX),
sent (TX), and total traffic. These values are read from the Linux XFRM
counters and represent traffic for the current VPN session; they reset when
the session is removed. If XFRM counters are unavailable, the view displays
`N/A`.

The same view includes cumulative RX, TX, and combined totals for every
configured user, including users who are currently offline.

Per-user cumulative totals are persisted in
`/var/lib/ikev2-easy-installer/traffic.tsv`. A systemd timer named
`ikev2-easy-installer-traffic-snapshot.timer` saves active-session counters
every 30 seconds, so totals survive VPN service and server restarts.

The Windows utility supports both Full Tunnel and Proxy Mode without requiring separate VPN profiles.

## Optional Telegram status bot

Use `/status` in a private Telegram chat to see the hostname, installed server
and StrongSwan versions, local IPv4/IPv6 addresses, uptime, system load, RAM,
active VPN session/user counts, TCP/UDP socket counts, selected-interface traffic
and VPN service status. `/start` and `/help` show the available commands.
Only explicitly allowed numeric Telegram user IDs receive replies.

Install/update the Web UI from a full checkout on the Ubuntu VPN server:

```bash
sudo ./packaging/install-webui.sh install
```

Open **Telegram** in the panel, enter the token from your dedicated
[@BotFather](https://t.me/BotFather) bot and allowed numeric Telegram user IDs,
choose **Enable Telegram reports**, and save. This page also replaces tokens,
edits allowed accounts, shows local service states and disables reports.
Saved tokens are never returned to the browser; a blank token preserves the
existing one. Web UI deployment prepares the bot without prompting for a token
or enabling an unconfigured bot. The standalone CLI remains optional.

The bot uses outbound HTTPS polling with no webhook or inbound port. It runs as
a separate unprivileged user, consumes an aggregate local snapshot every 30
seconds, and cannot control services or read VPN credentials. The private token
stays in `/etc/ikev2-telegram/config.json`, outside the checkout. It works without
the Web UI. See [setup, report semantics, troubleshooting and removal](docs/telegram-bot.md).

## Web UI

The repository includes an authenticated, local-only management dashboard. It
has no third-party runtime dependencies and always binds to a loopback address.
Passwords are stored as scrypt hashes, sessions expire, login attempts are
throttled, and all POST requests after login require a CSRF token.

The dashboard uses a responsive dark interface with persistent workspace
navigation, service icons, compact health cards, and consistent account forms.
Body text uses a 16px default with relative font sizes, brighter secondary text,
and comfortable line spacing; mobile layouts retain readable controls and tables.
Tables scroll horizontally on small screens, and a keyboard skip link moves
focus directly to the workspace. The overview shows running-service counts and
highlights failed or unknown services. It refreshes every 30 seconds while the tab is visible, pausing during
service commands and confirmations. Failed refreshes retain the last received
status with a stale-data notice and disable commands until a successful refresh.
Service commands require a confirmation explaining their connection impact,
show progress, and are never automatically retried. A failed sign-out keeps the
dashboard visible until the server confirms logout.

The Overview's **CPU, memory & network** cards show current host readings and
SVG charts of the last five minutes. Readings refresh every five seconds while
Overview is visible and pause during confirmations, account forms, service
commands, and sign-out. A dedicated collector in the broker records samples
every five seconds even when no browser is connected or Overview is hidden.
HTTP requests read the shared buffer and do not trigger or delay sampling.
Chart history is a rolling five-minute buffer of at most 61 samples in broker
RAM; older samples expire, and restarting the broker clears this history.
It is not written to disk. Missing samples appear as gaps; no historical values
are invented. If the collector's latest sample is over 15 seconds old, the API
reports metrics as unavailable and the UI keeps the last readings as stale.
The first CPU and traffic rates require a second sample. A failed request keeps
the last readings with a stale-data notice; unavailable individual readings show
an em dash rather than zero.

CPU is the aggregate non-idle percentage across logical CPUs, excluding I/O
wait and without double-counting guest time. RAM used is `MemTotal - MemAvailable`,
which allows reclaimable cache. These counters come from Linux's
[/proc interfaces](https://docs.kernel.org/filesystems/proc.html). Network RX/TX
rates use byte-counter deltas from
[/proc/net/dev](https://docs.kernel.org/networking/statistics.html), and totals
are since the interface counters began, not persisted usage accounting. They
include **all interface traffic**, not only VPN clients. The installer state's
validated `OUT_IF` selects the interface; if unset, the lowest-metric IPv4 main
default route is used. State is parsed as data and never executed. If a configured
interface is missing or invalid, network readings are unavailable. Interface
changes, counter resets, and sample gaps longer than 15 seconds restart the
rate baseline. The API requires login, accepts no paths or command arguments,
and uses no new broker privileges, subprocesses, runtime dependencies, or
persistent storage. Installation verifies the metrics endpoint through the
unprivileged broker client and performs an 11-second no-request probe to verify
that samples accumulate independently. Rolling back the application discards
chart history without changing VPN, routing, firewall, DNS, or certificate settings.

The **Diagnostics** page runs eight local, read-only checks when opened or when
**Run checks** is selected: installer-state presence; VPN, firewall, and optional
SOCKS5 service states; the loaded `ikev2-eap` connection; local UDP 500/4500
bindings; IPv4 forwarding; and the managed server certificate's validity dates.
Each check reports Passed, Warning, Failed, or Unknown with a suggested next
step when appropriate. An absent or disabled optional proxy is not a failure.
Certificates expiring within 30 days produce a warning. Missing or inaccessible
information is Unknown, never a successful check. Failed requests retain the
previous report with a stale-data notice, and checks are not retried automatically.

Diagnostics requires the root broker and uses only fixed, allowlisted
`systemctl show`, `ipsec statusall ikev2-eap`, and `openssl x509 -noout -dates`
commands plus fixed `/proc` paths. It does not run the installer, read private
keys or VPN passwords, return raw command output, change settings, or restart
services. No additional broker sandbox permissions are needed. It does not test
Internet connectivity, DNS, provider firewall reachability, client routing,
IPv6 forwarding, NAT rules, certificate trust/hostname, or whether StrongSwan has
loaded the certificate currently on disk. UDP checks establish only that the
ports are locally bound, without attributing them to a process. Use the CLI
diagnostics and a real VPN client for those remaining integration checks.
Deploy both Web UI components through the existing workflow or installer;
application rollback removes this view without altering VPN configuration.
Installation also verifies all eight checks through the unprivileged broker
client, printing only status counts. VPN warnings or failures do not fail a Web
UI deployment; an invalid diagnostics response does.

The **Sessions** page shows established `ikev2-eap` connections with the
VPN username, session ID, assigned IPv4/IPv6 addresses, public peer IP, connection
age, and received/sent traffic from the server's perspective. It prefers the
remote EAP identity when it differs from the IKE identity. Search usernames and
browse 20 connections per page; multiple connections for one user remain separate.
Snapshots refresh every 30 seconds while this section and the tab are visible,
pausing during service commands, account dialogs, and sign-out. A failed refresh
keeps the previous snapshot with a stale-data warning. A stopped service shows
an empty list; unavailable or unrecognized status is reported as an error.

Session monitoring requires the installed root broker. It runs only fixed,
read-only `systemctl is-active` and `ipsec statusall ikev2-eap` commands, returns
selected fields instead of raw output, and keeps the existing broker sandbox.
It changes no VPN, firewall, routing, DNS, or CA settings and cannot disconnect
users. Redeploy both Web UI components with `sudo ./packaging/install-webui.sh install`
to enable it on an existing server. Rolling back application files removes this
view without changing VPN sessions or account credentials.

Traffic values are summed across currently installed CHILD_SAs, as exposed by
[StrongSwan's status output](https://github.com/strongswan/strongswan/blob/master/src/libcharon/plugins/stroke/stroke_list.c).
They reset on rekey or reconnect and are distinct from the CLI's persisted
per-user cumulative totals. Missing counters or unassigned addresses show `N/A`.

The **Users** page lists and searches configured VPN usernames, with 20 accounts
per page. Admins can add accounts, change passwords, and delete accounts after
confirmation. Deleting the last account requires an additional acknowledgement.
Passwords are never returned in lists or mutation responses; generated passwords
are created in the browser and must be saved before closing the form. VPN passwords
accept 12–128 printable ASCII characters except double quotes. Quoted secrets in
the [StrongSwan stroke parser](https://github.com/strongswan/strongswan/blob/master/src/libcharon/plugins/stroke/stroke_cred.c)
are literal, so backslashes are preserved rather than escaped.

Use **Link & QR** beside a user to generate a copyable configuration link and
an offline QR code, with a PNG download. **Include the current VPN password** is
optional and off by default. Password-bearing outputs grant access to the
account; send them privately. Links do not expire and are encoded, not encrypted.
The new `ikev://import/v1/…` transport wraps the existing `.ikev` profile and is
ready for future Android QR/clipboard import; existing desktop clients continue
to use `.ikev` files. See [configuration sharing and the Android decoding contract](docs/config-sharing.md).

Use **Limits** to set a per-account traffic allowance in GiB and validity in
days from its first connection. Each can independently be **Unlimited**; all
existing and new accounts start unlimited. Validity continues while offline.
**Reset usage** resets traffic and restarts validity while keeping the limits.
When an allowance is exhausted, the worker disables authentication and disconnects
that user's sessions. The Users page shows usage, pending activation, expiry and
suspension; it refreshes every 30 seconds. Accounting is sampled every five
seconds and can miss traffic/short sessions between samples, so it is unsuitable
for exact billing. See [user limits, enforcement and rollback](docs/user-limits.md).

Account changes update the installer's existing `/etc/ipsec.secrets` atomically,
preserving other entries and file mode 0600. The EAP credentials remain in the
root-only format required by the existing installation; the Web UI process
cannot read that file. The same accounts remain visible in the CLI installer.
Duplicate identities, includes, and nonstandard EAP entries require manual
management and are refused by this page. Avoid editing accounts through the CLI
and Web UI simultaneously: Web UI writes are serialized and stale snapshots are
rejected. The current CLI recognizes suspended users and uses the shared lock;
update the separately deployed CLI script before using it alongside quotas.
Older CLI versions do not participate in this lock.

After a credential change, the helper runs only `ipsec rereadsecrets` when StrongSwan is
active. It does not restart the VPN or disconnect existing sessions; password
changes/deletion apply at the next authentication. If the VPN is stopped, the
file is saved for its next start. Reload failure restores the previous file and
reports that runtime state could not be confirmed. Mutations are never retried
automatically. Rolling back the application does not undo account changes.

The long-lived broker keeps its existing read-only `/etc` sandbox. A new root
oneshot unit, `ikev2-web-users-apply.service`, reads a mode-0600 request in a
root-only runtime directory and applies only validated create/reset/delete
operations to the fixed secrets file, plus validated limits edits and usage resets.
The short-lived `ikev2-web-limits.service` also suspends/restores credentials and
disconnects accounts at their limits. Both units have write access to their
private limits state directory. Atomic replacement requires write access to
parent `/etc`, granted only to these short-lived units. It has no capabilities,
IP network sockets, shell command input, or write access to home directories; SSH,
sudoers, account databases, systemd configuration and private key directories
are inaccessible inside its namespace. Requests/results are deleted after use;
audit logs contain operation and username but never passwords. The deployment
runs a read-only helper check without changing credentials or reloading VPN
secrets. To block user mutations while retaining monitoring, move
`/etc/systemd/system/ikev2-web-users-apply.service` to the same path with a
`.disabled` suffix and run `sudo systemctl daemon-reload`. A subsequent Web UI
deployment reinstalls the unit. Existing accounts survive application rollback
or removal of this helper unit. Quota suspensions require the explicit
[limits rollback procedure](docs/user-limits.md#deployment-and-rollback) before
deploying an older application.

Optional browser regression checks run against a local static server with mocked
APIs, so no VPN service commands or production credentials are used:

```bash
npm install --prefix /tmp/ikev2-ui-tools --ignore-scripts --no-audit --no-fund playwright@1.64.1 jsqr@1.4.0
PLAYWRIGHT_BROWSERS_PATH=/tmp/ikev2-ui-browsers node /tmp/ikev2-ui-tools/node_modules/playwright/cli.js install firefox
NODE_PATH=/tmp/ikev2-ui-tools/node_modules PLAYWRIGHT_BROWSERS_PATH=/tmp/ikev2-ui-browsers node --test tests/test_webui_browser.cjs
```

Run the browser checks as your normal desktop user. Set `UI_SCREENSHOT_DIR` to a
temporary directory if desktop and mobile screenshots are needed.

### Install with working service controls

Running `python3 -m webui.app` by itself starts only the unprivileged frontend;
service commands remain disabled because the root broker is not running. On the
Ubuntu VPN server, install or update both components with:

```bash
sudo ./packaging/install-webui.sh install
```

The installer prompts for the initial admin password, creates the dedicated
`ikev2-web` system account, installs only the required Web UI files under
`/opt/ikev2`, enables both systemd services, and verifies the socket and HTTP
health endpoint. Existing authentication configuration is preserved on update.

For GitHub Actions deployment, run **Deploy Production Web UI** from the Actions
tab. It uses the existing `production` environment's `PRODUCTION_HOST`,
`PRODUCTION_PORT`, `PRODUCTION_USER`, `PRODUCTION_SSH_PRIVATE_KEY`, and
`PRODUCTION_KNOWN_HOSTS`. Before the first deployment, add the environment secret
`PRODUCTION_WEBUI_PASSWORD` with an admin password of at least 12 characters.
The password is passed over SSH stdin and stored only as a scrypt hash on the
server. Updates preserve the existing password; changing the secret does not
reset it. The SSH user needs passwordless sudo for `/usr/bin/install` and the
fixed, root-owned Web UI installer. See [deployment permissions](PRODUCTION_DEPLOYMENT.md#web-ui-deployment)
for the exact sudoers rule.

The workflow's `inspect_origin` option only reports web listeners and installed
web-server tools on the production server; it skips installation and credential
changes. Use it before configuring an HTTPS reverse proxy.

If login fails because an older authentication config was preserved, run the
workflow with **reset_admin_password** enabled. This explicitly sets the username
to `admin` and replaces its password with `PRODUCTION_WEBUI_PASSWORD`, retaining
cookie and session settings. It restarts only the Web UI, signs out existing
sessions, clears login throttling, and verifies login plus authenticated dashboard
access. Normal deployments continue to preserve credentials.

To reset the Web UI password, run the main server script without arguments and
choose **13) Reset Web UI Password**. You can also run it directly:

```bash
sudo ./ikev2-strongswan-ubuntu.sh reset-webui-password
```

Both use the installed CLI tool (and require the Web UI to be installed):

```bash
sudo /opt/ikev2/reset-webui-password.sh
```

From a repository checkout, the equivalent command is
`sudo ./packaging/install-webui.sh reset-password`. Update the Web UI with the
installer first if the installed reset script is missing. Password reset has no
button or HTTP endpoint in the panel; it requires root/sudo access to the server.

Enter the new password twice without echo (minimum 12 characters). The script
preserves the current username, cookie security, session lifetime and extra
settings, removes the two-factor secret and replay counter, atomically replaces the configuration with mode `0600` and retains its
owner. It then restarts only `ikev2-web.service`, signing out panel sessions and
clearing login throttling. Passwords are never accepted as command arguments or
printed. For automation, `--password-stdin` reads the exact password from stdin
without a trailing newline; it skips confirmation. If the service restart fails,
the CLI reports that the new password is saved and requires starting the service.
Rolling back application files does not restore the previous password.

To enable two-factor authentication, sign in and open **Security**. Enter your
current panel password, scan the QR with an Authenticator app (or enter the setup
key), then confirm a six-digit code. Setup expires after 10 minutes and does not
change sign-in until confirmed. Sign-in then requires username, password and a
fresh **Authenticator code**. To disable it from Security, enter the current
password and a fresh code. Changing two-factor settings signs out other panel
sessions while retaining the session used to make the change.

TOTP uses SHA-1, six digits and 30-second steps as described in
[RFC 6238](https://www.rfc-editor.org/rfc/rfc6238), with one step of clock skew.
Keep the phone and server clocks synchronized. Accepted codes cannot be reused,
even after restarting the Web UI; wait for the next code if one was just used
for setup or sign-in. Wrong codes share the existing five-attempt login throttle.
QR generation stays in the browser and sends no key to an external QR service.
The key is stored in the private `0600` authentication config, never returned
again after setup. The Web UI service can write only its configuration directory
in addition to its existing sandbox access, to persist settings and consumed
codes. Normal deployments and cookie changes preserve two-factor protection.

If you lose the Authenticator, use **13) Reset Web UI Password** in the server's
main CLI menu (or either reset command above). Every successful CLI password
reset also disables two-factor authentication and removes its key. After signing
in with the new password, set it up again in **Security**. There are no recovery
codes or HTTP password-reset endpoints. Rolling back to code predating 2FA removes
its enforcement; deploy a 2FA-capable version to keep protection.

This workflow installs the Web UI, its broker and its limits worker/timer. It starts neither a VPN
installation nor a VPN restart. The frontend stays unprivileged, and the broker
checks the root-only installer state. Without broker access in development mode,
the dashboard reports `Unknown` if it cannot inspect that state.

Check it at any time with:

```bash
./packaging/install-webui.sh status
```

The HTTP listener remains on loopback. From an administration workstation, use:

```bash
ssh -L 8787:127.0.0.1:8787 user@server
```

Then open `http://127.0.0.1:8787`. Pass `--secure-cookie` to the install command
when an HTTPS reverse proxy will be used instead of a loopback HTTP/SSH tunnel.

To publish `p.mahanfreenet.ir` through Arvan, run the Web UI workflow with
`publish_arvan=true`. It installs a dedicated Caddy service as `ikev2-proxy` on
HTTP port **8080**, restricted to actual Arvan peer IPs and loopback, and proxies
to `127.0.0.1:8787`. It enables Secure cookies while preserving credentials.
Configure Arvan's origin as HTTP port 8080, enable browser HTTPS and disable
caching for this hostname. The CDN-to-origin leg remains unencrypted.
See [the Persian deployment and rollback guide](WEBUI_ARVAN_GUIDE.md).

The proxy uses only the Caddy binary extracted from the distribution's
authenticated APT package; it does not install or start the package's default
service. The dedicated service needs no root privileges or capabilities and
does not listen on 80/443. Installation requires root, creates a service account
and systemd unit, and downloads the current Arvan network list over HTTPS.
It does not change firewall, VPN, DNS or CA trust settings. Existing firewall
policy must permit Arvan to reach TCP 8080. Subsequent Web UI deployments retain
the proxy configuration, refresh its binary/network list, and restart it.
The binary is managed by this installer, so ordinary APT upgrades alone do not
update it. Manage it with `sudo systemctl start|stop|restart ikev2-web-proxy`.
Removing `/etc/ikev2-web-proxy/origin.json` and disabling its service stops
subsequent deployments from enabling the proxy again.

For unattended initial installation, `install --password-stdin` reads the exact
password from stdin (without a trailing newline). Interactive installation
continues to prompt twice. Keep passwords out of shell arguments and logs.

To roll back a Web UI update, check out the previous revision and rerun its
installer; the existing authentication config is retained. To disable the Web
UI, run `sudo systemctl disable --now ikev2-web.service ikev2-web-broker.service`.
Starting, stopping, or restarting VPN services from the dashboard is a separate
privileged action and can disconnect active VPN sessions; the dashboard asks
for confirmation.

### Frontend-only development mode

To preview the frontend without installing system services, create a local
config and run the unprivileged process from the repository root:

```bash
python3 -m webui.manage create-config \
  --output ./webui-config.json \
  --username admin \
  --allow-http

python3 -m webui.app --config ./webui-config.json
```

In this mode, service buttons are intentionally disabled because no privileged
broker is running. Use the installer above when commands must work.

### Privileged service broker

The Web UI process remains unprivileged. A separate root broker accepts only
fixed start, stop, and restart operations for the managed StrongSwan, firewall,
and proxy systemd units, plus allowlisted account, profile-export and limits operations. It does not invoke a shell or accept arbitrary command
arguments. Linux peer credentials restrict the Unix socket to the configured
Web UI user, and requested/completed operations are appended to
`/var/log/ikev2-web/audit.log` with mode `0600`.

The installer deploys the reference units from `packaging/systemd/`, installs
the application at `/opt/ikev2`, stores authentication configuration at
`/etc/ikev2-web/config.json`, and creates the `ikev2-web` system account.

Run the Web UI tests with:

```bash
python3 -m unittest tests.test_webui
```

---

# 1. Server requirements

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

# 2. Fresh server installation

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
Password** changes the panel password from the terminal; **14) Exit** closes the
menu.

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

### Optional IPv6 full tunnel

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

### Public Let’s Encrypt certificate

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
by the Windows and Linux clients. Linux v1.6 and Windows v6.1 import this same
frozen format.

Private-CA profiles embed the public CA certificate as single-line Base64-encoded
DER and include its OpenSSL SHA-256 fingerprint. Let’s Encrypt profiles instead
contain `"certificate_trust": "public"` and do not embed a CA certificate. VPN
passwords, private keys, and other secret material are never included in CLI
file exports. Web UI sharing can optionally include a password in a separate
envelope; see [configuration sharing](docs/config-sharing.md). CLI profiles are written with mode
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

# 7. Linux Gateway / Router Mode

The Linux client can also act as a routed L3 gateway for one defined downstream IPv4 prefix. All IP protocols from that prefix are forwarded through the existing IKEv2 tunnel; the server and Linux client must use the same connected VPN profile.

## How to enable it

After the Linux client is installed and the IKEv2 profile is connected:

```bash
sudo ikev2 gateway status
sudo ikev2 gateway diagnose
sudo ikev2 gateway enable
```

Gateway Diagnostics checks the local interface, forwarding setting, connected
VPN profile, endpoint route, XFRM policy/state, fail-closed guard, and Gateway
Mode firewall/NAT rules without changing them. It cannot inspect routes
configured on downstream hosts or routers, so end-to-end forwarding must still
be tested from a client.

To disable it again:

```bash
sudo ikev2 gateway disable
```

The command is idempotent and removes only the Gateway Mode rules it created when disabled. Existing installations with broad RFC1918 rules are migrated automatically; Gateway Mode asks for the specific downstream prefix.

## Required values

The client detects the VPN endpoint from the single connected managed profile and discovers its next hop and routing table from the current kernel route. The LAN interface defaults to the interface on the default route. Gateway Mode prompts for the exact downstream network prefix; it does not select or broaden the prefix automatically. For the Ansible network shown in the troubleshooting examples, use `192.168.119.0/24` if that is the intended downstream LAN.

```bash
export GATEWAY_LAN_IFACE=ens160
export GATEWAY_DOWNSTREAM_SUBNET=192.168.119.0/24
export GATEWAY_VPN_VIRTUAL_IP=10.10.10.1

sudo -E ikev2 gateway enable
```

If more than one managed profile is connected, set `GATEWAY_REMOTE_VPN_SERVER` to the active profile's IPv4 endpoint. `GATEWAY_LAN_GATEWAY` can also be set explicitly when automatic next-hop discovery is unsuitable. These values must match the live host and upstream router layout.

To change the selected prefix later, disable and re-enable Gateway Mode with the new prefix.

## Why SNAT is used

The XFRM CHILD_SA uses the client virtual IP (for example `10.10.10.1`) as its local source selector. Forwarded packets arrive with their original downstream source, so they do not match that selector. Gateway Mode therefore SNATs traffic from the configured downstream prefix in `POSTROUTING` before XFRM policy lookup, changing its source to the assigned VPN virtual IP.

Do not add `-m policy --dir out --pol ipsec` to these SNAT rules: before translation, the forwarded source does not match the IPsec selector, so that condition prevents the rule from matching. The VPN endpoint is excluded so its outer IKE/ESP traffic stays on the physical LAN route.

```bash
iptables -t nat -A POSTROUTING -s "$GATEWAY_DOWNSTREAM_SUBNET" -o ens160 ! -d "$GATEWAY_REMOTE_VPN_SERVER/32" -j SNAT --to-source "$GATEWAY_VPN_VIRTUAL_IP"
```

The server's existing full-tunnel selector and VPN-client NAT then carry all traffic from the selected prefix through the tunnel. Check the `POSTROUTING` packet counter before and after a downstream test; a zero counter means the packets are not matching the SNAT rule.

## Routing safety

The gateway protects the IKEv2 endpoint route in the routing table selected by the kernel (StrongSwan commonly uses table `220`) so tunnel control traffic continues to leave through the normal LAN gateway instead of looping back into the VPN:

```bash
sudo ikev2 gateway status
sudo ip route show table 220
```

This keeps the tunnel maintenance path separate from forwarded traffic and avoids routing loops. Two lower-precedence XFRM block policies (`0.0.0.0/1` and `128.0.0.0/1`) cover the VPN virtual IP's destinations without colliding with the active `0.0.0.0/0` CHILD_SA selector. They prevent forwarded packets from falling back to cleartext if the CHILD_SA disappears; the active IPsec policy takes precedence while the tunnel is up.

## Validation checklist

Use the following checks after enabling Gateway Mode:

```bash
sudo sysctl net.ipv4.ip_forward
sudo ip -br addr
sudo ip route
sudo ip route show table 220
sudo ip xfrm policy
sudo iptables -S | grep ikev2-client-gateway-mode
sudo iptables -t nat -S | grep ikev2-client-gateway-mode
sudo tcpdump -ni any
```

Expected behavior:

- IPv4 forwarding is enabled
- the protected VPN endpoint route uses the underlying LAN gateway, not the VPN tunnel
- forwarded packets from the selected downstream prefix enter the IPsec tunnel
- NAT rules mention the gateway rule comment and only target the downstream subnet
- existing SOCKS5 mode is unchanged and still responds on `192.168.98.6:4443` if configured

## Disable and rollback

To remove only the Gateway Mode changes:

```bash
sudo ikev2 gateway disable
```

This removes the added forwarding/NAT rules and the protected route while leaving the existing normal IKEv2 profile and SOCKS5 setup intact.

---

# 8. Prepare client files

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
ikev2-windows-client.ps1
ca-cert.cer
```

Windows portable-profile import:

```text
ikev2-windows-client.ps1
username.ikev
```

Linux manual setup:

```text
ikev2-linux-client.sh
ca-cert.cer
```

Linux portable-profile import:

```text
ikev2-linux-client.sh
username.ikev
```

---

# 8. Windows client v6.3

## Requirements

- Windows with built-in VPN PowerShell cmdlets
- PowerShell 5.1+
- Administrator privileges
- `ikev2-windows-client.ps1`
- either `ca-cert.cer` for manual setup or an exported `username.ikev` for portable setup

The script automatically requests Administrator privileges through UAC when required.

## Run the utility

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client.ps1
```

A sibling `ca-cert.cer` is still validated and trusted at startup when present.
When no local `.cer` exists, v6.3 starts normally and displays:

```text
Trusted CA : no local CA loaded
Credentials: Windows native EAP dialog
```

This allows a directory containing only the script and `username.ikev` to
provision the VPN. After a successful portable import, the menu displays the
embedded CA's name as utility status.

The menu is:

```text
IKEv2 Windows VPN Utility v6.3.0
===============================

1) Install / Update IKEv2 VPN
2) Import .ikev Profile
3) Status
4) Connect
5) Disconnect
6) Auto-reconnect (toggle)
7) Update Client
8) About
9) Traffic Mode (Full Tunnel / Proxy Mode)
10) Exit
```

Choose **Update Client** to download the latest Windows script from the
project's GitHub `main` branch. The updater validates PowerShell syntax,
refuses older versions, and replaces the running script and any
auto-reconnect copy. Restart the utility after updating.

Choose **Auto-reconnect** and select a profile to enable or disable its
scheduled task. It retries while the Windows user is logged in and starts
again at the next logon. Windows must have saved the VPN credentials from a
successful connection; the task cannot answer an interactive credential prompt.
Manual **Disconnect** stops and removes the task.

---

# 9. Create or import a Windows VPN profile

## Manual setup

Keep together:

```text
ikev2-windows-client.ps1
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
ikev2-windows-client.ps1
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
uses the VPN while connected. IPv6 also uses the VPN when the server has IPv6
enabled and negotiates the IPv6 traffic selector.

```text
Windows IPv4/IPv6 traffic -> IKEv2 -> VPN Server -> Internet
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
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client.ps1
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
1.9.0
```

Linux uses **Full Tunnel IPv4**, with optional IPv6 when the server supports it.
Imported profiles read the server's `connection.ipv6` setting. Manual profile
creation asks whether to request IPv6, which must also be enabled on the server.
Version 1.9 can display advertised SOCKS5
Proxy Mode metadata during import, but it does not configure or use the proxy,
add proxy routes, or enable split tunneling.

Run:

```bash
chmod +x ikev2-linux-client.sh
sudo ./ikev2-linux-client.sh
```

Menu:

```text
IKEv2 Linux VPN Utility
=======================
Version: 1.8.0

1) Install / Update IKEv2 VPN
2) Import .ikev Profile
3) List / Status
4) Connect
5) Disconnect
6) Auto-reconnect (toggle)
7) Remove Profile
8) Uninstall Utility
9) Update Client
10) About
11) Exit
```

Choose **Auto-reconnect** and select a profile to enable or disable its
systemd service. An enabled profile is retried every 15 seconds and the service
starts at boot. Manual **Disconnect**, profile removal, and utility uninstall
stop and remove the service.

Choose **Update Client** to download the latest Linux script from the project's
GitHub `main` branch. The updater validates Bash syntax, refuses older versions,
and updates both the running script and the installed copy used by
auto-reconnect. Restart the utility after updating.

## Manual setup

Keep these files together:

```text
ikev2-linux-client.sh
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

The easier v1.8 workflow is:

1. Export `username.ikev` on the VPN server.
2. Copy the `.ikev` file to Ubuntu.
3. Run the Linux client.
4. Choose `Import .ikev Profile`.
5. Enter the path to the file.
6. Review the server, remote identity, username, CA fingerprint, and proxy
   availability.
7. Confirm trust and enter the VPN password.
8. Connect.

Linux v1.8 supports `.ikev` schema version 1 only. It parses JSON with
Python 3's standard-library `json` module, verifies the embedded DER CA and
SHA-256 fingerprint, and then uses the same StrongSwan profile, secrets, DNS,
reload, and connection architecture as manual setup.

**`.ikev` files never contain the VPN password or private keys.** The
importer asks for the password using hidden input and stores it only through
the existing root-only StrongSwan secrets mechanism. The original `.ikev`
file and embedded Base64 data are not copied into managed runtime state.

Linux v1.8 uses one managed VPN CA at a time. A profile using the existing CA
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
sudo ./ikev2-linux-client.sh
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
sudo ./ikev2-strongswan-ubuntu.sh uninstall
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
chmod +x ikev2-strongswan-ubuntu.sh
sudo ./ikev2-strongswan-ubuntu.sh install
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
ikev2-windows-client.ps1
ca-cert.cer
```

Portable setup:

```text
ikev2-windows-client.ps1
username.ikev
```

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client.ps1
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
chmod +x ikev2-linux-client.sh
sudo ./ikev2-linux-client.sh
```

---

# 25. Quick start — upgrade existing installation to Proxy Mode

## Server

```bash
chmod +x ikev2-strongswan-ubuntu.sh
sudo ./ikev2-strongswan-ubuntu.sh upgrade
```

Accept the defaults unless you need custom values:

```text
Proxy IP   : 10.254.254.1
Proxy Port : 1080
```

Verify:

```bash
sudo ./ikev2-strongswan-ubuntu.sh status
sudo systemctl status ikev2-vpn-proxy
sudo ss -lntp | grep 1080
```

## Windows

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client.ps1
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

## ACME regression checks

Run `python3 -B -m unittest discover -s tests -v` from the repository root.
These tests use Python 3, Bash, OpenSSL, and temporary test certificates; Certbot,
StrongSwan, and systemd commands are simulated. They cover RSA provisioning,
ECDSA migration, invalid certificate rejection, credential/connection reload,
stopped services, runtime certificate verification, and deployment rollback.
They do not contact an ACME server, change host configuration, or require root.
