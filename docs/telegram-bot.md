# Private Telegram server monitor

This optional bot reports the existing Ubuntu 22.04/24.04 StrongSwan server's
health in a private Telegram chat. It needs Python 3's standard library,
systemd, iproute2 and the existing StrongSwan installation. It has no Python
package dependencies and does not require the Web UI.

## Setup from the Web UI

Install/update both Web UI components from a full checkout with
`sudo ./packaging/install-webui.sh install`, or use the Web UI deployment
workflow. This also prepares the Telegram code, service account and units,
preserving existing configuration and service enablement. A new bot stays
disabled until configured. No terminal token entry is needed.

Create a dedicated bot with @BotFather, then open **Telegram** in the panel.
Enter its token, the allowed numeric Telegram user IDs (comma-separated),
select **Enable Telegram reports** and choose **Save Telegram settings**.
Use the same page to replace the token, change allowed users, view local
bot/timer states or disable the bot. A blank token preserves the saved token.
There is no operation to display or download the saved token. The token input
clears on save, navigation and sign-out; no token is stored in browser storage.
Reports disclose host/IP/version/usage information to Telegram when requested.
After enabling, send `/status` from an allowed private chat to check connectivity.

Saving uses authenticated `POST /api/telegram` with CSRF protection. A private,
root-owned queue invokes the fixed `ikev2-web-telegram-apply.service` oneshot.
The long-lived root broker retains its read-only `/etc` sandbox. Only the
oneshot can replace the private Telegram config and start/stop/enable/disable
the fixed bot/collector units. VPN and Web UI services are not restarted.
Reads and responses omit the token; audit records contain only the operation
and outcome. Revisions are keyed and stale changes are rejected; the helper
rechecks configuration and enablement before applying. Web and CLI writes
share a config lock. Failed activation attempts restore the previous config
and service settings where possible; the panel requires refresh and never
automatically retries a change whose outcome is uncertain.

Deploy the panel, broker, helper and bot together. An older installation
without prepared services shows an unavailable message and disables edits.
The helper needs write access only to `/etc/ikev2-telegram` and its private
runtime request directory; it has no Internet socket access. The existing
network-facing Web UI remains unable to read the root-owned token directly.

## Optional standalone CLI and user ID discovery

1. Create a **dedicated** bot with [@BotFather](https://t.me/BotFather) using
   `/newbot`. Keep its token private; do not put it in a shell command, GitHub
   issue, chat with support, repository file or screenshot.
2. On the VPN server, use a full repository checkout. If you do not know your
   numeric Telegram user ID, run:

   ```bash
   ./packaging/install-telegram-bot.sh identify
   ```

   Enter the token at the hidden prompt. In your own **private** chat with the
   bot, send the one-time `/start <code>` printed by the terminal, then press
   Enter in the terminal. The matching private sender ID is printed locally.
   This does not authorize anyone, save the token or send Telegram replies.
   Verify that you sent that exact challenge from your own account. For
   multiple admins, repeat this step for each account. If no ID appears, retry;
   a busy bot may have more pending updates than the discovery request reads.
   Do discovery before installation, or first stop any bot process using this token.
3. Install from that checkout:

   ```bash
   sudo ./packaging/install-telegram-bot.sh install
   ```

   Enter the token at the hidden prompt and one or more numeric Telegram
   **user** IDs separated by commas. An interactive terminal is required. Usernames
   and group/chat IDs are not accepted as authorization. Later `install` runs
   update code while preserving this configuration.
4. Send `/status` to the bot from an allowed account in a private chat.

Example (illustrative numbers):

```text
🖥 Host: vpn-server
🚀 IKEv2 Installer: 6.4.9-en
📡 StrongSwan: 5.9.5
🌐 IPv4: 192.0.2.10
🌐 IPv6: 2001:db8::10
⏳ Uptime: 12d 4h 20m
📈 System Load: 0.05, 0.03, 0.01
📋 RAM: 584.71 MiB / 1.87 GiB
👥 Online Clients: 3 sessions / 2 users
🔹 TCP sockets: 67
🔸 UDP sockets: 7
🚦 Traffic (ens3): 12.92 TiB (↑ 6.49 TiB, ↓ 6.43 TiB)
ℹ️ VPN Status: running
🕒 Sample age: 8s
```

`/start` and `/help` show commands. `/status@YourBotName` is also accepted in a
private chat. Other users, bots, groups, supergroups and channels receive no
replies. Allowed accounts can get at most one reply every five seconds. Incoming
commands older than five minutes are ignored. The bot sends replies only when
requested; this version does not send scheduled reports or automatic alerts.

## What the numbers mean

- Hostname and uptime refer to the host. Load is the Linux 1/5/15 minute load
  average, not CPU percentage. RAM used is `MemTotal - MemAvailable`.
- IPs are **local global-scope** addresses from `ip -j address show scope global`,
  capped at 20 per family. They can include bridge/private addresses. Behind
  NAT, the externally visible public IP may differ; no external IP lookup runs.
- Installer version is the validated `INSTALLER_VERSION` in the managed state
  file. StrongSwan version comes from `ipsec --version`. Neither state nor any
  message is executed as shell code.
- Online Clients counts established `ikev2-eap` IKE sessions and distinct users
  using the existing session parser. One user can have several sessions.
  Unavailable status is `N/A`/`unknown`; a confirmed stopped VPN has zero clients.
- TCP/UDP are IPv4 plus IPv6 **sockets in use** from `/proc/net/sockstat*`, not
  counts of VPN users, packets or only established TCP connections.
- Traffic is RX/TX on the installer's validated `OUT_IF`, falling back to the
  lowest-metric IPv4 default route only if no interface is configured. It includes
  **all interface traffic**, not just VPN traffic, and is since those interface
  counters began/reset. It is not persisted lifetime or per-user accounting.
  The arrows use the server's perspective: ↑ TX, ↓ RX. Units are binary KiB,
  MiB, GiB and TiB. The report includes this scope reminder.
- VPN Status describes the StrongSwan service/session probe. It does not verify
  public reachability, DNS, routing or whether a client can browse the Internet.
- The root collector refreshes every 30 seconds. Missing, corrupt, future-dated
  or more than 90-second-old snapshots produce an explicit unavailable/stale
  message. Individual unavailable readings stay `N/A` rather than inventing zero.

## Architecture and security impact

The network-facing `ikev2-telegram.service` runs as the separate
`ikev2-telegram` system account, with no capabilities and a read-only system
filesystem. It cannot access the Web UI broker socket, VPN credentials/private
keys, managed installer state or Web UI configuration. It reads only the
aggregate `/run/ikev2-telegram/status.json` snapshot for server monitoring.
This file contains counts, never user identities, peer IPs, passwords or keys.

`ikev2-telegram-collect.service` is a root oneshot, launched by
`ikev2-telegram-collect.timer`. It reuses the existing metrics and session parsers.
Its fixed subprocesses are `systemctl is-active strongswan-starter.service`,
`ipsec statusall ikev2-eap`, `ipsec --version` and
`ip -j address show scope global`, each with a timeout. It also reads fixed
`/proc` files and the managed state as data. Only its own runtime snapshot
directory is writable; it has no IPv4/IPv6 socket access or token access.
Snapshots are replaced atomically, owned by root with group `ikev2-telegram`
and mode 0640 inside a root-owned 0750 directory.

The token and allowlist are stored outside the repository at
`/etc/ikev2-telegram/config.json` (root:root 0600, parent directory 0700).
systemd `LoadCredential` passes a private read-only copy to the bot. The bot
cannot modify its token or allowlist. Telegram requests use verified HTTPS to
the fixed `api.telegram.org` host; redirects and environment proxies are disabled.
Failures log generic error codes, never token-bearing URLs or raw responses.
Tokens must still be rotated with BotFather if exposed.

The bot uses the official [Telegram Bot API](https://core.telegram.org/bots/api#getupdates)
`getUpdates` long polling and `sendMessage`. The server needs outbound DNS and
HTTPS access to Telegram. No listening port, webhook, reverse proxy, TLS
certificate or inbound firewall rule is added. Use one poller per token; an
existing webhook is refused and is never removed automatically.

Telegram receives requested server status reports and the authorized account's
chat ID. This includes hostname, IP addresses, versions and host usage. Treat
those reports as administrative information. Content protection is requested
for replies, but recipients can still record what they see.

Processed update offsets are persisted atomically in
`/var/lib/ikev2-telegram/cursor.json` with mode 0600 **before** sending a reply,
preventing replay after restart. A failed/ambiguous send is not retried; request
`/status` again after five seconds. Poll failures back off up to 60 seconds,
honoring Telegram's `retry_after` when longer. Neither cursor nor report contains
the bot token. Installation copies just the bot and shared read-only parsers to
`/opt/ikev2-telegram`, isolated from Web UI deployments.

This adds root actions to create the service account, install sandboxed units,
manage private bot configuration and periodically collect local status.
Installation and removal do not change CA trust, VPN consent, accounts,
certificates, firewall/NAT, routing or DNS configuration and do not restart VPN.

## Maintenance and troubleshooting

```bash
# Local service health (does not print the token).
./packaging/install-telegram-bot.sh status

# Rotate a token or change the allowlist; restarts only the bot.
# Normally use the panel's Telegram page instead.
sudo ./packaging/install-telegram-bot.sh configure

# Inspect safe bot/collector logs.
sudo journalctl -u ikev2-telegram.service -u ikev2-telegram-collect.service -n 50

# Refresh/check the collector if the report says unavailable or stale.
sudo systemctl start ikev2-telegram-collect.service
systemctl status ikev2-telegram-collect.timer
```

If there is no reply, check the bot service, private chat, **user ID** allowlist,
five-second cooldown and server connectivity to Telegram. Code 401 usually
means the token needs correction; 409 can mean another poller or webhook is
using it; 429 causes a rate-limit delay. A healthy local systemd service alone
does not prove Telegram connectivity. Test one real `/status` after setup and
again after reboot. Also check stopped VPN, unavailable probes, unauthorized
private messages, group messages and restarting the collector and bot.

## Removal and rollback

```bash
sudo ./packaging/install-telegram-bot.sh uninstall
```

Disabling in the panel retains configuration, code and units for re-enabling.
The CLI uninstall stops/disables only the bot and its timer, removes their unit files,
`/opt/ikev2-telegram`, the runtime snapshot and cursor state. It preserves
`/etc/ikev2-telegram/config.json` and the service account for reinstallation.
The Web UI settings helper is retained by the Web UI; its settings page detects
the missing bot units and disables edits. Redeploying the Web UI prepares them again.
If permanently retiring the bot, revoke its token using BotFather and remove
that private config directory deliberately. Reports already delivered to Telegram
are not recalled by removal.

To roll back application code, run the installer from a previous compatible
checkout. The bot is an optional component; disabling/removing it leaves VPN
operation and Web UI deployment independent.

## Offline validation

```bash
python3 -B -m unittest discover -s tests -p test_telegram_bot.py -v
python3 -B -m unittest discover -s tests -p test_telegram_settings.py -v
bash -n packaging/install-telegram-bot.sh
shellcheck packaging/install-telegram-bot.sh
systemd-analyze verify packaging/systemd/ikev2-telegram*.service packaging/systemd/ikev2-telegram*.timer
```

Offline tests use synthetic tokens and mocked Telegram/local command responses.
They exercise allowlists and private-chat checks, safe error handling, rate
limits, cursor persistence, stale snapshots, missing probes and report semantics.
Use a disposable Ubuntu VM for privileged install/reboot/removal tests and a
dedicated real bot for the final end-to-end check.
