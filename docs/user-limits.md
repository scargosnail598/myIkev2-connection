# User traffic quotas and validity

In **Users**, choose **Limits** beside an account. Each setting is independent:

- **Unlimited traffic** or a total traffic allowance in GiB (download + upload,
  shared by all the account's devices; 1 GiB = 1024³ bytes).
- **Unlimited validity** or a validity period in days after the first observed
  connection (1 day = 24 hours). This is elapsed time, including time offline.

Existing and newly created accounts start with both settings unlimited. The
worker begins a new traffic ledger when this feature is installed; it does not
import historical totals from the CLI's separate `traffic.tsv` ledger. Usage
continues to accumulate while traffic is unlimited. Setting a traffic limit
below already recorded usage can suspend the account immediately.

When validity is first enabled, an offline account waits for its next observed
connection. If already online, its validity starts immediately. Subsequent
changes to a finite validity period keep that activation time. Removing validity
clears its activation time; enabling it again starts a new period. The Users
page shows whether activation is pending or displays the expiry date.

**Reset usage** requires confirmation and resets both recorded traffic and the
validity activation, preserving the configured limits. If online, the new
validity period starts at reset; if offline, it starts at the next observed
connection. Current per-CHILD-SA counter baselines are preserved so traffic from
before reset is not counted again. This action does not change the password,
regenerate certificates, or disconnect a user whose new allowance is available.

At either limit, the worker suspends the account's EAP credential, reloads
secrets, then terminates only that account's observed managed IKE sessions. New
authentication is denied after the credential reload. Increasing the exhausted
limit, making it unlimited, or resetting usage restores the same password if
no other limit remains exhausted. Changing a password preserves suspension and
limits. Deleting and recreating an account through the web panel gives the new
account unlimited settings. Links and QR codes keep working with the same
credentials after a reset; sharing a profile does not bypass server limits.

## Accounting and enforcement precision

`ikev2-web-limits.timer` runs a short-lived root worker five seconds after each run,
independently of the browser and broker. It reads all established managed
sessions with fixed `ipsec statusall ikev2-eap` arguments, uses the authenticated
EAP identity, and adds deltas from individual CHILD SAs. Rekeyed CHILD SAs still
present in the snapshot contribute their final observed counters. The daemon's
boot ID, PID and process start time prevent reused SA identifiers from sharing
baselines across daemon/server restarts. Totals and activation times survive
those restarts and web deployments.

This is **sampled quota enforcement, not exact billing or a hard packet cap**.
The final traffic between the last sample and a CHILD SA disappearing may be
missed. Sessions that begin and end between samples may be missed entirely,
including their activation. Encrypted ESP overhead is not added. Traffic can
exceed the allowance before the next successful check and disconnect. At most
five blocked sessions are terminated per cycle, with remaining sessions retried
on later cycles. Scheduling delays and unavailable daemon status can extend the
normal five-second delay. A correct server clock is required for validity.

For exact accounting, rapid reconnect abuse resistance or an invoice-grade
quota, use event-based accounting with a dedicated AAA/RADIUS backend rather
than this sampled ledger. A failed/stopped worker cannot detect new exhaustion;
already suspended credentials remain disabled on disk. The Users page warns
when samples are missing/older than 30 seconds, counters are unknown, or a
reload/disconnect needs another attempt. Unknown counters retain previous
baselines; unavailable snapshots do not clear saved usage. Mutations are never
automatically retried by the browser.

## Storage, privileges and CLI

State is stored in root-owned mode-0600 `/var/lib/ikev2-web/limits.json` inside a
mode-0700 directory. It contains settings, usage, activation times and counter
baselines, **no passwords or private keys**. Account and policy changes use a
shared root lock in that directory. Web mutations require a current account
revision and limits revision, an administrator session, CSRF, allowlisted fields,
and an audit record. `null` means unlimited; zero, negative, Boolean, fractional
and oversized numeric limits are rejected. Validity is capped at 36,600 days.
The frontend and long-lived broker gain no filesystem write permission.

Suspension adds `# ikev2-web-limit ` to an existing EAP entry in
`/etc/ipsec.secrets`; restoring access removes only this exact marker. Other
entries and mode 0600 are preserved. No second copy of the password is stored.
The existing `ikev2-web-users-apply.service` handles administrator changes; the
new `ikev2-web-limits.service` handles periodic accounting and enforcement. Both
are short-lived, have no capabilities or IP sockets, and can atomically replace
files under `/etc` and write only their private state/queue directories. SSH,
sudoers, account databases, systemd configuration and private key directories
remain inaccessible inside their namespaces. Only fixed `ipsec rereadsecrets`
and validated numeric `ikev2-eap[N]` disconnect commands are used.

The current repository CLI recognizes suspended entries and serializes its
account mutations with the worker. Password changes keep the marker and deleting
a suspended user removes its entry normally. Update the CLI script from this
same revision before using it alongside web quotas; the web deployment does not
replace the separately deployed VPN installer script. Older CLI versions ignore
suspended accounts and do not acquire this lock. Avoid concurrent edits with
older CLI versions or direct edits to the secrets/state files.

Missing state for a suspended account, corrupt state and unsafe permissions fail
closed without automatically restoring its credentials. Keep state private in
server backups and do not commit it or unsanitized logs to source control.

## Deployment and rollback

```bash
sudo ./packaging/install-webui.sh install
```

Deployment installs the timer, worker and shared private state directory,
updates the existing account-helper namespace, starts an initial accounting
sample, and verifies the timer. It does not restart StrongSwan or change routing,
DNS, firewall/NAT or CA trust. Existing unlimited accounts stay enabled. The
broker advertises `user_limits`; older brokers leave the controls disabled.

Inspect worker status without printing credentials:

```bash
systemctl status ikev2-web-limits.timer ikev2-web-limits.service
sudo journalctl -u ikev2-web-limits.service -n 20 --no-pager
```

Stopping the web frontend or broker alone does not stop limit enforcement. For
an application rollback, stop enforcement and explicitly restore quota-suspended
credentials **before** replacing the application with an older revision:

```bash
sudo systemctl disable --now ikev2-web-limits.timer
sudo systemctl stop ikev2-web-limits.service
cd /opt/ikev2
sudo python3 -B -m webui.limits --restore-access
```

The restore command refuses to run while the worker/timer is active, removes
only quota markers, and reloads secrets when the VPN is running. Saved policies
and usage remain on disk; re-enabling the timer will enforce them again. Then
redeploy the previous Web UI sources and installer. Keep the private limits
state when rolling back; deleting it is not a safe way to restore account access.
