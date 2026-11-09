# Publish the Web UI through a CDN

This guide applies to any CDN that can proxy a custom HTTPS hostname to an
HTTP or HTTPS origin. Configure the provider's equivalent settings rather than
relying on a vendor preset. All hostnames, addresses and accounts shown here
are examples; keep real deployment settings outside the repository.

The traffic path is:

```text
Browser --HTTPS--> CDN --HTTPS preferred / HTTP optional--> origin proxy
                                                        --> 127.0.0.1:8787
```

The Web UI itself remains on loopback. A CDN cannot carry the IKEv2 VPN through
this HTTP proxy: clients still connect directly to the VPN endpoint on UDP
500/4500. The panel and VPN must use distinct hostnames for this setup.
Only the panel hostname is proxied; keep the VPN hostname DNS-only/direct.

## Separate the panel and VPN hostnames

| Role | Example | DNS / transport |
| --- | --- | --- |
| VPN endpoint and certificate identity | `vpn.example.com` | DNS-only A/AAAA to the VPN server; direct UDP 500/4500 |
| Panel | `panel.example.com` | CDN-proxied record; HTTP/HTTPS origin proxy |

The records can share the same origin IP. The CDN in this project handles only
the panel's web requests. It does not relay IKEv2, ESP or the private SOCKS5
endpoint. Profile `server` and `remote_id` come exclusively from managed
`SERVER_ID` in `/var/lib/ikev2-easy-installer/state.env`, not the browser URL,
HTTP Host/forwarded headers or `/etc/ikev2-web-proxy/origin.json`.

Starting in **6.4.13**, the wizard displays the managed VPN identity and refuses
the same hostname for the panel. Direct proxy deployments and scheduled refresh
apply the same guard. State is read as data; DNS, StrongSwan settings, client
exports, CA trust and certificates are not rewritten. Without managed VPN state,
panel-only deployments can still configure a hostname. This equality check does
not discover CDN proxying or aliases in your DNS provider; keep the VPN name and
any CNAME targets direct too.

If you already proxied the VPN hostname, preserve the name in `SERVER_ID` and
its certificate: switch **that record back to DNS-only**, resolving to your VPN
server. Create a different panel hostname, enable its CDN proxy/HTTPS settings,
and rerun `cdn` with that panel name. Update the provider's origin Host header
to match the new panel hostname and open the panel at its new URL. DNS caches
may keep the prior CDN addresses until their TTL expires. Existing VPN profiles
remain valid when their hostname/identity is preserved.

Do not work around the failure by blindly replacing exported hostnames with an
IP, disabling certificate validation or changing `SERVER_ID` alone. The managed
identity is also used by StrongSwan and its certificate. If the originally
chosen VPN identity really must change, that requires a coordinated certificate
and VPN/client configuration change, separate from panel CDN setup.

Read only the current identity, without exporting secrets:

```bash
sudo grep '^SERVER_ID=' /var/lib/ikev2-easy-installer/state.env
```

To migrate an existing collision, update the complete sources and run `cdn`
with a separate panel hostname **before** a panel redeployment that would apply
the old proxy settings. There is no need to uninstall/reinstall the VPN.

## Interactive server setup

During a fresh installation, **6.4.14** and later collect the **VPN address /
certificate identity** first. If you select optional Web UI installation, you
can also select CDN setup and enter a separate **Web UI / CDN hostname** before
the final installation summary. The VPN uses only its direct identity; the
CDN wizard runs after successful VPN and panel installation and uses only the
selected panel name. It still requests an origin port, network source and
confirmation. Skipping CDN setup keeps SSH-tunnel panel access.

If the Web UI is already installed, open the server installer's main menu and
choose **15) Configure Web UI for CDN**, or run:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh cdn
```

To pass a panel hostname selected earlier, use
`sudo bash ./ikev2-strongswan-ubuntu.sh cdn --hostname panel.example.com`.
This skips only the hostname question; validation and confirmation still apply.

This wizard prepares the **managed HTTP origin** described below. It checks the
local panel and asks for a hostname, origin port and network source. Choose
**4) ArvanCloud**, **5) Cloudflare** or **6) another CDN's public HTTPS text feed**
for automatic retrieval. Local CIDR files and multiline paste remain available.
Existing hostname/port values are defaults; keeping an existing list also keeps
its automatic source when one is configured. It validates all inputs, checks port
availability and shows a summary before an explicit confirmation (default No).
Cancellation or invalid inputs leave existing settings unchanged.

After confirmation it saves `origin.json` and `cdn-ips.txt` under
`/etc/ikev2-web-proxy`, then installs/restarts the proxy and panel and enables
Secure cookies. Existing `origin.json`, `cdn-ips.txt` and `Caddyfile` are copied
to a root-only `backups/setup.*` directory there before replacement. Failed
installation returns an error and leaves new settings saved for retry; packages
and services may be partially installed. It does not automatically roll back.

The wizard is retained at `/opt/ikev2/packaging/setup-cdn-proxy.sh` by new panel
installations. The main script uses checkout/installed tools only if `packaging/TOOL_VERSION`
matches its server version; otherwise it downloads the verified matching release
bundle. An older
panel/release without the wizard needs a Web UI update or a full checkout.
No real deployment values are written into the repository.

You still configure the provider's DNS/CDN record, HTTPS, cache bypass and
provider/server firewall yourself. The wizard prints the required origin
hostname and port. It does not change the VPN, DNS, CA trust or firewall rules.
For an encrypted CDN-to-origin leg use your own HTTPS reverse proxy below;
this wizard's origin transport is HTTP.

If release downloads fail with HTTP 504, installer 6.4.11 and later automatically
try the public GitHub asset API, without a token. The exact release and asset
are checked, any API digest is verified, and the release SHA256SUMS check stays
mandatory. API failures/rate limits still stop setup safely. On 6.4.10 or older,
manually update the installer first if its direct-only updater is also blocked.
A gateway error does not mean the Web UI package is absent from the release.

## Automatic CDN network feeds

Available in server **6.4.12** with the matching Web UI/CDN sources. No API token
is required for the built-in public sources:

| Wizard option | Source |
| --- | --- |
| 4 — ArvanCloud | [Official text feed](https://www.arvancloud.ir/fa/ips.txt) |
| 5 — Cloudflare | [Official API](https://developers.cloudflare.com/api/resources/ips/), including IPv4 and IPv6 |
| 6 — Other CDN | Public HTTPS text URL, one CIDR per line |

Custom feeds must use HTTPS, port 443, without credentials, query parameters or
fragments. HTTPS redirects are validated too. Feed downloads are bounded in
time and size; empty lists, private/loopback addresses, `/0`, non-aligned CIDRs,
HTML, unsuccessful API replies and incomplete Cloudflare lists are rejected.
No provider ranges or production settings are committed to this repository.
The source selection is stored in the root-owned `origin.json` as, for example:

```json
{"hostname": "panel.example.com", "port": 8080, "network_source": {"provider": "cloudflare"}}
```

The initial wizard fetch must succeed before settings can be applied. Subsequent
refreshes run daily via `ikev2-web-cdn-networks.timer`, with up to 30 minutes of
random delay and a persistent missed-run check after boot. The root worker
fetches/validates ranges, renders and validates a candidate Caddyfile, then
atomically replaces the range/config files and restarts **only the CDN proxy**.
The Caddy admin API remains disabled. The proxy's local health is verified;
restart/health failure restores the previous files and attempts to restart and
verify the prior configuration. An unsuccessful rollback is reported as an error.
Unchanged ranges/config do not restart the proxy. A stopped proxy is not started
by the worker. VPN services, panel credentials, CA trust, DNS and firewall rules
are not changed. A restart can briefly interrupt in-flight panel requests.

Network/parse/validation failure keeps the current allowlist; there is no
fallback to all peers. When retrieval keeps failing, that list becomes stale
and may eventually block new CDN egress ranges. Inspect the service journal and
fix the feed or select a different source through the wizard. The worker shares
a configuration lock with the wizard and proxy installer. Its sandbox permits
writes only to proxy settings and private runtime storage; VPN secrets/keys and
panel/Telegram authentication settings are inaccessible. The worker has no
Linux capabilities; namespace creation and mount syscalls are blocked. Full
privileged integration should be verified on a disposable Ubuntu VM.

```bash
systemctl status ikev2-web-cdn-networks.timer
sudo journalctl -u ikev2-web-cdn-networks.service -n 30 --no-pager
sudo systemctl start ikev2-web-cdn-networks.service
```

Choosing manual file/paste mode removes the automatic source and disables the
timer. Keeping an existing list retains its source and refresh policy. To pause
automation while preserving settings:

```bash
sudo systemctl disable --now ikev2-web-cdn-networks.timer
sudo systemctl stop ikev2-web-cdn-networks.service
```

Redeploying an automatically configured proxy enables its timer again. To
persistently switch off automatic refresh, rerun the wizard and choose manual
file/paste input. Sources without `network_source` remain manual on upgrade.
Any IP allowlists in your provider firewall or OS firewall are managed separately;
this feature updates Caddy's peer restrictions only.

For an existing server, update the complete project sources at the permanent
installer checkout (including `packaging/`), then redeploy the panel and rerun
the CDN wizard. Do not reinstall the VPN. The version guard also lets an updated
standalone server script fetch matching tools rather than execute an old wizard.
Full sources remain useful when GitHub release/API routes are unavailable:

```bash
sudo bash /opt/ikev2-installer/packaging/install-webui.sh install
sudo bash /opt/ikev2-installer/ikev2-strongswan-ubuntu.sh cdn
```

## 1. Prepare the panel and hostname

Install the panel on the Ubuntu VPN server:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh webui
```

Select a panel hostname such as `panel.example.com`, different from the managed
VPN identity, and add it to your CDN account.
Configure its origin address using the server's public IP or an origin-only
hostname. Enable the provider's DNS proxy/CDN mode for the panel record. Keep
the origin destination separate from the proxied record to avoid routing loops.

Set browser-facing HTTPS, require HTTPS visitors and disable caching for the
**entire panel hostname**, including `/api/*`, login, authenticated responses,
errors and static assets. Preserve cookies, query strings and request methods;
forward `Host: panel.example.com` and a trustworthy
`X-Forwarded-Proto: https` for HTTPS visitors. Do not let callers choose these
headers unchecked at the origin. Apply suitable CDN access controls to this
management hostname. Do not cache or transform API responses.

## 2. Choose the origin transport

### Recommended: HTTPS to an existing origin proxy

If your provider requires HTTPS origins, or you need encryption on both legs,
use your existing Caddy, nginx or equivalent HTTPS reverse proxy. Configure its
certificate and private key on the server, verify the certificate hostname and
chain at the CDN, and require strict origin verification. Never commit origin
keys, certificates, real hostnames or provider account exports.

The proxy must:

- Terminate TLS for the panel hostname and forward to `http://127.0.0.1:8787`.
- Accept only the intended hostname and overwrite forwarded headers.
- Forward `Host` and set `X-Forwarded-Proto` to `https` for the backend.
- Return `Cache-Control: private, no-store` and disable proxy caching.
- Restrict origin access to the CDN's current published egress networks, or use
  the provider's authenticated origin mechanism.

Integrate this into the existing server configuration and validate/reload it
using that proxy's normal procedure. Certificate issuance, renewal, ports and
service permissions belong to that proxy's setup; this repository's optional
HTTP helper does not manage TLS certificates.

Enable Secure cookies after HTTPS is ready:

```bash
(
  cd /opt/ikev2
  sudo python3 -B -m webui.manage configure-cookie \
    --config /etc/ikev2-web/config.json
)
sudo systemctl restart ikev2-web.service
```

This restarts panel sessions only. Preserve VPN configuration and check that
browser login works through HTTPS before ending your administrative SSH session.

### Optional: managed HTTP origin proxy

Use this helper only if your CDN supports an HTTP origin on an unprivileged
port and you accept that the CDN-to-origin leg is unencrypted. Browser HTTPS
alone does not encrypt that leg. If your provider supports only HTTPS origins,
use the preceding setup instead.

From a full checkout, prepare a root-owned origin configuration. Replace the
example hostname with your own; choose a port your CDN supports (default 8080,
allowed range 1024–65535 except backend port 8787):

```bash
sudo install -d -o root -g root -m 0755 /etc/ikev2-web-proxy
sudo tee /etc/ikev2-web-proxy/origin.json >/dev/null <<'JSON'
{"hostname": "panel.example.com", "port": 8080}
JSON
sudo chown root:root /etc/ikev2-web-proxy/origin.json
sudo chmod 0644 /etc/ikev2-web-proxy/origin.json
```

The interactive wizard can obtain **origin-facing egress IP ranges**
automatically as described above. For manual setup, obtain them from your
provider's authoritative documentation/API. Save the current IPv4 and IPv6 CIDRs in a local
text file with one network per line, without comments, commas or HTML. The
helper validates public, network-aligned CIDRs and rejects an empty list,
private/loopback ranges and `/0`. No provider list or live address is bundled.
Install your prepared file:

```bash
sudo install -o root -g root -m 0644 /path/to/provider-ranges.txt \
  /etc/ikev2-web-proxy/cdn-ips.txt
sudo bash ./packaging/install-cdn-proxy.sh
```

The helper extracts Caddy from the distribution's authenticated APT package,
creates the `ikev2-proxy` system account and dedicated systemd service, enables
Secure cookies, and restarts only the panel/proxy. It permits actual socket peers
from the configured networks and loopback; forwarded client IP headers cannot
bypass this allowlist. It does not install Caddy's default package service or
change public ports 80/443, VPN services, CA trust, DNS, routing or firewall rules.
An occupied origin port is rejected when the managed proxy is not running.

In the CDN, select **HTTP** for the origin, the chosen origin port and the panel
hostname as its Host header. Configure the provider/server firewall to permit
that TCP port **only from the provider's egress networks**; include IPv6 rules
when relevant. Do not expose backend port 8787. Loopback access stays available
for diagnostics. Some providers share egress IPs between customers; an IP
allowlist does not authenticate your particular CDN account.

Subsequent Web UI deployments preserve `origin.json`, regenerate the proxy from
`cdn-ips.txt`, refresh the extracted Caddy binary and restart the managed proxy.
Automatic sources refresh daily; manual mode requires updating the network
file and rerunning the helper when the provider changes its ranges. Ordinary APT upgrades do not update the extracted binary. Existing
proxy installations must prepare `cdn-ips.txt` before their next deployment.
Missing/invalid input stops proxy setup; it never opens access to all peers.

## 3. Deploy with GitHub Actions

Keep deployment values in the GitHub `production` environment, outside Git:

| Variable | Purpose |
| --- | --- |
| `PRODUCTION_HOST`, `PRODUCTION_PORT`, `PRODUCTION_USER` | Origin SSH connection |
| `PRODUCTION_WEBUI_HOSTNAME` | Public panel hostname |
| `PRODUCTION_CDN_ORIGIN_PORT` | HTTP origin port; blank defaults to 8080 |
| `PRODUCTION_CDN_NETWORKS` | Current provider egress CIDRs, one per line |

SSH credentials, verified known-hosts data and the panel password belong in the
[deployment secrets](PRODUCTION_DEPLOYMENT.md#6-add-environment-secrets), not
source files. Run **Deploy Production Web UI** with `publish_cdn=true` for the
managed HTTP helper. It validates these values, copies them to the fixed origin
paths and installs the panel/proxy. No repository file contains your hostname or
network list. Normal deployments with `publish_cdn=false` retain existing origin
settings. Selecting `publish_cdn=true` supplies a manual network list from the
environment and disables automatic refresh; configure automatic mode through
the wizard afterwards. `inspect_origin` only reports service/listener state;
`verify_public_only` checks the public HTTPS endpoint using the server's
`origin.json`, without installing anything or sending login credentials.

For an independently managed HTTPS proxy, deploy the app normally, keep Secure
cookies enabled and manage TLS/proxy settings separately. The managed CDN
publication option is specifically for the HTTP helper.

## 4. Verify access

For the managed HTTP helper, check locally using your configured hostname/port:

```bash
sudo systemctl status ikev2-web.service ikev2-web-proxy.service
curl --fail --header 'Host: panel.example.com' \
  --header 'X-Forwarded-Proto: https' \
  http://127.0.0.1:8080/api/health
```

From a permitted visitor network, check `https://panel.example.com/api/health`.
It should return status `ok` with `Cache-Control` containing `no-store`. The
unauthenticated `/api/dashboard` request must return 401. Test real login,
sign-out, 2FA if enabled and a page refresh. Verify CDN cache misses/bypass and
that an untrusted source cannot reach the origin directly. Never send panel
passwords in diagnostic command arguments or publish unsanitized logs/HAR files.

If the origin server's outbound probe is blocked by CDN visitor restrictions,
the workflow fails the check. Verify from an allowed network; keep the access
restriction in place. A successful local health check alone does not prove
public DNS, TLS, CDN policy or provider firewall connectivity.

## 5. Roll back or return to SSH access

If the interactive wizard replaced an existing setup, its `backups/setup.*`
directory holds the previous origin, CIDRs and Caddyfile. Stop the automatic
refresh timer/worker before restoring settings. Restore the relevant
files from the reported backup into `/etc/ikev2-web-proxy/`, preserving root
ownership and mode 0644, then restart the proxy after validating the restored
configuration. Only files that existed before setup are backed up. These
backups do not undo package installation or the panel's Secure-cookie change.

Restore your prior proxy/CDN configuration if publication fails. For the managed
helper, stop future automatic proxy activation by removing its trigger config:

```bash
sudo systemctl disable --now ikev2-web-cdn-networks.timer
sudo systemctl stop ikev2-web-cdn-networks.service
sudo systemctl disable --now ikev2-web-proxy.service
sudo rm -- /etc/ikev2-web-proxy/origin.json
```

Secure cookies remain enabled after disabling HTTPS publication. Before returning
to the HTTP SSH tunnel, switch cookie mode and restart only the panel:

```bash
(
  cd /opt/ikev2
  sudo python3 -B -m webui.manage configure-cookie \
    --config /etc/ikev2-web/config.json --allow-http
)
sudo systemctl restart ikev2-web.service
ssh -L 8787:127.0.0.1:8787 admin@YOUR_SERVER
```

Run `ssh` from your own computer and open `http://127.0.0.1:8787` locally. Close
any former origin firewall allowance and remove or disable the CDN record as
appropriate. Disabling the proxy does not undo VPN accounts, panel credentials
or quota suspensions; follow the [limits rollback](docs/user-limits.md#deployment-and-rollback)
when removing quota enforcement or deploying an older application.
