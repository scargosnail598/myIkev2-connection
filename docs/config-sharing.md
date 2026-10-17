# Configuration links and QR codes

In **Users**, choose **Link & QR** beside an account, optionally select
**Include the current VPN password**, then choose **Generate link & QR**.
Copy the link or download the QR as a PNG. The QR encodes the exact same string
as the link; it does not point to a hosted configuration or require a public
panel URL. Android support for this transport must be implemented separately.
Stock camera apps and existing Windows/Linux clients do not import this URI.

Password inclusion is off by default. If omitted, the receiving app must ask
for the VPN password. If included, the current stored EAP password is read by
the broker only for this explicit export. Lists and account mutation responses
continue to omit credentials. The panel clears its link and canvas on close,
Escape, navigation, sign-out, session expiry responses, or five minutes after
generation. This clears the display; copied links and downloaded images remain.
Clipboard writes require browser permission and a secure context; if unavailable,
the panel selects the link for manual copying.

Links are compressed and encoded, **not encrypted or signed**. They do not
expire. Anyone with a password-bearing link/image can authenticate as the user.
Deliver it privately. Change the password or delete the account to block future
authentication with old credentials; existing sessions are not disconnected.
After CA rotation, regenerate profiles so recipients receive the current CA.

## Version 1 transport contract

```text
ikev://import/v1/<payload>
```

`payload` is URL-safe Base64 (`A-Z a-z 0-9 _ -`), without `=` padding, of a
**zlib-wrapped DEFLATE** stream (not raw DEFLATE or gzip). Inflating it produces
a UTF-8 JSON object:

```json
{
  "format": "ikev-share",
  "version": 1,
  "profile": {
    "format": "ikev-profile",
    "version": 1,
    "name": "alice@vpn.example.com",
    "server": "vpn.example.com",
    "remote_id": "vpn.example.com",
    "username": "alice",
    "authentication": "eap-mschapv2",
    "certificate_trust": "public",
    "connection": { "mode": "full-tunnel", "ipv6": false },
    "server_profile": "secure",
    "proxy": { "enabled": false }
  }
}
```

If requested, the envelope has an additional string field `password`. It is
outside `profile`, preserving the existing password-free `.ikev` version 1
schema. Empty, malformed or unsupported envelopes must be rejected. The full
URI is ASCII and limited to **2953 bytes**, the QR byte-mode capacity at version
40 with low error correction. Exports exceeding that limit fail with a message
suggesting the installer's `.ikev` file export.

The profile uses the same fields as the [CLI export](../README.md#portable-ikev-client-profiles):

| Field | Meaning |
| --- | --- |
| `server`, `remote_id` | Managed `SERVER_ID`, used for endpoint and certificate identity |
| `authentication` | `eap-mschapv2` |
| `certificate_trust` | `public` or `private-ca` |
| `ca_certificate` | Private CA only: `encoding: der-base64`, public DER certificate in `data`, uppercase colon-separated SHA-256 in `sha256` |
| `connection` | `mode: full-tunnel`, Boolean `ipv6` |
| `server_profile` | `secure` or `stock-windows-compatible` |
| `proxy` | `enabled: false`, or `enabled: true`, `type: socks5`, `host` and integer `port` |

For Android, use one decoder for QR scanner text, clipboard text, and a future
`ACTION_VIEW` handler for scheme `ikev`, host `import`, path `/v1/<payload>`.
Trim surrounding whitespace, then accept only that exact URI structure: no
query, fragment, user info, extra path segments, or other version. Restore
Base64 padding if required, URL-safe decode, then inflate with
`java.util.zip.Inflater` using its default zlib wrapper. Bound decompressed data
to **64 KiB**, require the stream to finish with no trailing data, decode UTF-8,
and validate both format/version pairs and the profile fields before importing.
If `password` is absent, prompt for it.

Show the server, identity, username and certificate trust before saving. For
private CAs, parse the DER as X.509, verify its SHA-256 against `sha256`, and
request trust consent. A matching fingerprint checks consistency within the
untrusted link; it does not prove the sender's identity. For public trust, use
the platform's certificate validation. Preserve VPN permission/consent and
server identity validation; importing a link must not disable validation or
connect automatically.

A reference decoder for development (input must be an exported URI):

```python
import base64
import json
import re
import zlib


def decode_share(text):
    text = text.strip()
    prefix = "ikev://import/v1/"
    if len(text) > 2953 or not text.startswith(prefix):
        raise ValueError("Unsupported configuration link")
    payload = text[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", payload):
        raise ValueError("Invalid configuration payload")
    packed = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(packed, 65537)
    if len(raw) > 65536 or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Invalid or oversized configuration stream")
    envelope = json.loads(raw.decode("utf-8"))
    if not isinstance(envelope, dict) or envelope.get("format") != "ikev-share" or type(envelope.get("version")) is not int or envelope["version"] != 1:
        raise ValueError("Unsupported share format")
    profile = envelope.get("profile")
    if not isinstance(profile, dict) or profile.get("format") != "ikev-profile" or type(profile.get("version")) is not int or profile["version"] != 1:
        raise ValueError("Unsupported profile format")
    # Validate all profile fields and obtain trust/permission consent before use.
    return envelope
```

## Deployment and security impact

Redeploy both the web process and root broker with:

```bash
sudo ./packaging/install-webui.sh install
```

The deployment workflow also installs the new module and QR asset. Export is
advertised separately as the `profile_export` broker capability, so a panel
with an older broker disables **Link & QR** until both components are updated.

`POST /api/users/export` requires an administrator session and CSRF token. Its
JSON body accepts only `username`, the user-list `revision`, and optional
Boolean `include_password` (default false). Stale lists are rejected; refresh
users before retrying. Responses use `Cache-Control: no-store`. Export audit
records contain the username, operation and outcome, never the URI or password.
The broker reads only managed state, account storage and the fixed public CA
path; no caller-selected paths or shell commands are accepted. The web process
still cannot read the root-only files directly.

Export changes no account, VPN, CA trust, routing, DNS, firewall or NAT settings,
and invokes no privileged mutation helper. It does allow an authenticated admin
to explicitly retrieve an existing EAP password, a new capability. Rolling back
application files removes export without changing server configuration. It
cannot revoke copies already shared; rotate credentials if those copies should
stop working.

QR generation runs locally in the browser using the vendored
[Project Nayuki QR Code generator v1.8.0](https://github.com/nayuki/QR-Code-generator/releases/tag/v1.8.0)
(`webui/static/qrcodegen.js`, MIT license retained in its header). No CDN or
external QR API receives configuration data, and no runtime package install is
required. Browser tests use `jsqr@1.4.0` to decode the generated pixels.
