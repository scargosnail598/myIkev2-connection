# Web UI guide

[Back to README](../README.md) · [Server guide](server.md)

The repository includes an authenticated, local-only management dashboard. It
has no third-party runtime dependencies and always binds to a loopback address.
Passwords are stored as scrypt hashes, sessions expire, login attempts are
throttled, and all POST requests after login require a CSRF token.

## Install with working service controls

During fresh VPN setup, accept **Install the Web UI management panel too? [y/N]**.
The default is **No**. Installation starts after VPN verification and prompts
for a separate panel admin password (at least 12 characters; username `admin`).
Skipping the panel keeps the command-line VPN workflow available.

On an existing server, choose **14) Install / Update Web UI**, or run:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh webui
```

The main script uses the checkout beside it when `packaging/install-webui.sh`
is present. Otherwise it downloads `ikev2-webui-v<server-version>.tar.gz` and
`SHA256SUMS` from the matching `server-v<server-version>` GitHub Release,
verifies the SHA256 checksum, checks archive paths/types, and installs from a
private temporary directory which it removes on exit. It never falls back to
an arbitrary branch or a different version. The standalone route requires
Python 3, curl, CA certificates and coreutils; on a minimal Ubuntu image install
them with `sudo apt-get install python3 curl ca-certificates coreutils`.

The bundle must have been published by the updated release workflow. For an
unreleased script or an older release without the bundle, use a full checkout
of the desired revision and run the dedicated installer from its repository root:

```bash
sudo bash ./packaging/install-webui.sh install
```

The installer creates the `ikev2-web` account, copies application files under
`/opt/ikev2`, preserves existing authentication settings, and enables the Web UI,
root broker and quota worker/timer. It also prepares Telegram components, leaving
an unconfigured bot disabled. These are root/sudo changes; the app itself runs
unprivileged. Installation does not change VPN CA trust, firewall rules, routing
or DNS. Existing quota enforcement continues on redeployment.

Open the loopback-only panel through an SSH tunnel from your own computer:

```bash
ssh -L 8787:127.0.0.1:8787 admin@YOUR_SERVER
```

Then open **http://127.0.0.1:8787** locally and sign in as `admin`. Add `-p PORT`
for a nonstandard SSH port. Keep the SSH session open. No public panel port is
opened. An existing configured reverse proxy remains in use on redeployment;
see the HTTPS section below before enabling `--secure-cookie`.

For advanced installation options, use the main command or dedicated installer:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh webui --secure-cookie
sudo bash ./packaging/install-webui.sh status
```

`--password-stdin` also passes through to the dedicated installer. It reads the
exact password without a trailing newline; keep credentials out of arguments
and logs. Existing authentication config is preserved on update.

A panel failure after VPN setup prints a warning and retry command. It does not
undo VPN provisioning, and it may leave partially installed panel services/files.
The direct `webui` command returns a failure status when installation fails.
Running `python3 -m webui.app` alone starts the frontend without the broker and
cannot control services.

For GitHub Actions deployment, run **Deploy Production Web UI** from the Actions
tab. It uses the existing `production` environment's `PRODUCTION_HOST`,
`PRODUCTION_PORT`, `PRODUCTION_USER`, `PRODUCTION_SSH_PRIVATE_KEY`, and
`PRODUCTION_KNOWN_HOSTS`. Before the first deployment, add the environment secret
`PRODUCTION_WEBUI_PASSWORD` with an admin password of at least 12 characters.
The password is passed over SSH stdin and stored only as a scrypt hash on the
server. Updates preserve the existing password; changing the secret does not
reset it. The SSH user needs passwordless sudo for `/usr/bin/install` and the
fixed, root-owned Web UI installer. See [deployment permissions](../PRODUCTION_DEPLOYMENT.md#web-ui-deployment)
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

To prepare the installed panel for a CDN interactively, choose **15) Configure
Web UI for CDN** or run `sudo bash ./ikev2-strongswan-ubuntu.sh cdn`. The wizard
collects the hostname, HTTP origin port and CIDRs (file, paste or existing list),
validates them, backs up existing settings and asks before applying changes.
New panel installations retain it at `/opt/ikev2/packaging/setup-cdn-proxy.sh`.
The main script uses a checkout, installed helper or verified release bundle.
For provider configuration, verification and rollback, follow the
[provider-neutral CDN guide](../WEBUI_CDN_GUIDE.md).
Keep deployment hostnames and address ranges outside the repository. You can use
an existing HTTPS reverse proxy, or the optional `install-cdn-proxy.sh` helper
when your provider supports HTTP origin connections. The helper uses the
root-owned `/etc/ikev2-web-proxy/origin.json` and `cdn-ips.txt`, accepts requests
only from configured CDN networks and loopback, and enables Secure cookies.
It does not change firewall, VPN, DNS or CA trust settings.

For GitHub Actions, set `PRODUCTION_WEBUI_HOSTNAME`, `PRODUCTION_CDN_ORIGIN_PORT`
and `PRODUCTION_CDN_NETWORKS` in the `production` environment, then select
`publish_cdn`. Normal redeployments preserve the server's origin settings and
local network list; refresh the list from your provider's authoritative source
when its ranges change. Existing proxy installations need the generic
`cdn-ips.txt` before their next deployment. No provider network list is fetched
automatically. The dedicated Caddy service remains unprivileged, listens on a
port above 1023 (default 8080), and uses an APT-authenticated binary. Ordinary
APT upgrades do not update that extracted binary; rerun the helper to refresh it.
Manage it with `sudo systemctl start|stop|restart ikev2-web-proxy`.
To prevent deployments from enabling it, disable the service and remove
`/etc/ikev2-web-proxy/origin.json`; see the guide for cookie and limits rollback.

For unattended initial installation, `install --password-stdin` reads the exact
password from stdin (without a trailing newline). Interactive installation
continues to prompt twice. Keep passwords out of shell arguments and logs.

To roll back a Web UI update, check out the previous revision and rerun its
installer; the existing authentication config is retained. To disable the Web
UI, run `sudo systemctl disable --now ikev2-web.service ikev2-web-broker.service`.
Starting, stopping, or restarting VPN services from the dashboard is a separate
privileged action and can disconnect active VPN sessions; the dashboard asks
for confirmation.

## Dashboard features and security

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
to use `.ikev` files. See [configuration sharing and the Android decoding contract](config-sharing.md).

Use **Limits** to set a per-account traffic allowance in GiB and validity in
days from its first connection. Each can independently be **Unlimited**; all
existing and new accounts start unlimited. Validity continues while offline.
**Reset usage** resets traffic and restarts validity while keeping the limits.
When an allowance is exhausted, the worker disables authentication and disconnects
that user's sessions. The Users page shows usage, pending activation, expiry and
suspension; it refreshes every 30 seconds. Accounting is sampled every five
seconds and can miss traffic/short sessions between samples, so it is unsuitable
for exact billing. See [user limits, enforcement and rollback](user-limits.md).

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
[limits rollback procedure](user-limits.md#deployment-and-rollback) before
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

## Frontend-only development mode

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

## Privileged service broker

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
