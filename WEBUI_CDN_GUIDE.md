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
500/4500. Use a separate hostname for the panel if needed.

## 1. Prepare the panel and hostname

Install the panel on the Ubuntu VPN server:

```bash
sudo bash ./ikev2-strongswan-ubuntu.sh webui
```

Select a panel hostname such as `panel.example.com`, add it to your CDN account,
and configure its origin address using the server's public IP or an origin-only
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

Obtain the **origin-facing egress IP ranges** from your chosen provider's
authoritative documentation/API. Save the current IPv4 and IPv6 CIDRs in a local
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
Update the network file when the provider changes its ranges, then rerun the
helper. Ordinary APT upgrades do not update the extracted binary. Existing
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
settings. `inspect_origin` only reports service/listener state;
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

Restore your prior proxy/CDN configuration if publication fails. For the managed
helper, stop future automatic proxy activation by removing its trigger config:

```bash
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
