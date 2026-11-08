# Client guide

[Back to README](../README.md) · [Server guide](server.md)

## Prepare client files

Manual client setup needs:

- the appropriate client utility
- the server-generated `ca-cert.cer`
- the VPN server IP or hostname
- a VPN username/password created on the server

Portable `.ikev` setup needs the client utility, the exported `username.ikev`,
and the user's VPN password. Private-CA profiles embed the public CA certificate and server settings;
public-trust profiles use the operating system trust store.

Example certificate copy:

```bash
scp root@YOUR_SERVER:/root/ikev2-client/ca-cert.cer .
```

Do **not** copy the CA private key to clients. For manual setup, keep
`ca-cert.cer` next to the client utility. Portable `.ikev` profiles do not require a separate certificate file: they
embed the public CA certificate or request system public trust.

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

## Windows client

### Requirements

- Windows with built-in VPN PowerShell cmdlets
- PowerShell 5.1+
- Administrator privileges
- `ikev2-windows-client.ps1`
- either `ca-cert.cer` for manual setup or an exported `username.ikev` for portable setup

The script automatically requests Administrator privileges through UAC when required.

### Run the utility

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client.ps1
```

A sibling `ca-cert.cer` is still validated and trusted at startup when present.
When no local `.cer` exists, the utility starts normally and displays:

```text
Trusted CA : no local CA loaded
Credentials: Windows native EAP dialog
```

This allows a directory containing only the script and `username.ikev` to
provision the VPN. After a successful portable import, the menu displays the
embedded CA's name as utility status.

The menu is:

```text
IKEv2 Windows VPN Utility v6.4.7
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

## Create or import a Windows VPN profile

### Manual setup

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

### Import a portable `.ikev` profile

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

The Windows client requires imported `remote_id` to match `server` because
the PowerShell/RAS profile API used here has no safe independent equivalent of
StrongSwan's `rightid` setting. Profiles where they differ are rejected.

---

## Windows Full Tunnel mode

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

## Windows Proxy Mode

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

## Configure an application to use Proxy Mode

Connecting the VPN in Proxy Mode does **not** automatically proxy applications.
Configure only selected SOCKS5-capable applications with the endpoint shown by
the utility. For the default server configuration:

```text
SOCKS5 10.254.254.1:1080
```

When available, enable remote/proxy DNS in the application to reduce DNS
leakage outside the proxy path.

---

## Change Windows traffic mode later

Run the utility and choose:

```text
9) Traffic Mode (Full Tunnel / Proxy Mode)
```

The existing `Apply-TrafficMode` path removes utility-managed stale Proxy Mode
state when switching to Full Tunnel and creates only the selected `/32` route
when switching to Proxy Mode. If connected, disconnect and reconnect before
relying on the changed routing state.

---

## Upgrade an existing Windows profile

Windows v6.1 can configure the traffic mode of existing IKEv2 profiles. Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ikev2-windows-client.ps1
```

Then choose option 6 and select the existing profile. Manual creation still
uses `ca-cert.cer`; portable import uses its embedded CA.

---

## Windows VPN credentials and IPsec policy

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

## Windows status

Choose:

```text
3) Status
```

Full Tunnel reports split tunneling disabled. Proxy Mode reports split
tunneling enabled, its saved SOCKS5 endpoint, and the single VPN-only `/32`
route.

---

## Ubuntu/Linux client

The Linux client is designed for Ubuntu 22.04 and 24.04.

Current Linux client version:

```text
1.9.8
```

Linux uses **Full Tunnel IPv4**, with optional IPv6 when the server supports it.
Imported profiles read the server's `connection.ipv6` setting. Manual profile
creation asks whether to request IPv6, which must also be enabled on the server.
The Linux client can display advertised SOCKS5
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
Version: 1.9.8

1) Install / Update IKEv2 VPN
2) Import .ikev Profile
3) List / Status
4) Connect
5) Disconnect
6) Auto-reconnect (toggle)
7) Remove Profile
8) Uninstall Utility
9) Update Client
10) Gateway Mode
11) About
12) Exit
```

Choose **Auto-reconnect** and select a profile to enable or disable its
systemd service. An enabled profile is retried every 15 seconds and the service
starts at boot. Manual **Disconnect**, profile removal, and utility uninstall
stop and remove the service.

Choose **Update Client** to download the latest Linux script from the project's
GitHub `main` branch. The updater validates Bash syntax, refuses older versions,
and updates both the running script and the installed copy used by
auto-reconnect. Restart the utility after updating.

### Manual setup

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

### Import a portable `.ikev` profile

To import a profile:

1. Export `username.ikev` on the VPN server.
2. Copy the `.ikev` file to Ubuntu.
3. Run the Linux client.
4. Choose `Import .ikev Profile`.
5. Enter the path to the file.
6. Review the server, remote identity, username, CA fingerprint, and proxy
   availability (or public system trust for ACME profiles).
7. Confirm trust and enter the VPN password.
8. Connect.

The Linux client supports `.ikev` schema version 1 only. It parses JSON with
Python 3's standard-library `json` module, verifies the embedded DER CA and
SHA-256 fingerprint, and then uses the same StrongSwan profile, secrets, DNS,
reload, and connection architecture as manual setup.

**`.ikev` files never contain the VPN password or private keys.** The
importer asks for the password using hidden input and stores it only through
the existing root-only StrongSwan secrets mechanism. The original `.ikev`
file and embedded Base64 data are not copied into managed runtime state.

The Linux client uses one managed VPN CA at a time. A profile using the existing CA
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

## Optional Linux system-wide command

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

## Linux Gateway / Router Mode

The Linux client can also act as a routed L3 gateway for one defined downstream IPv4 prefix. All IP protocols from that prefix are forwarded through the existing IKEv2 tunnel; the server and Linux client must use the same connected VPN profile.

### How to enable it

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

### Required values

The client detects the VPN endpoint from the single connected managed profile and discovers its next hop and routing table from the current kernel route. The LAN interface defaults to the interface on the default route. Gateway Mode prompts for the exact downstream network prefix; it does not select or broaden the prefix automatically. For example, use `192.168.119.0/24` only if that is the intended downstream LAN.

```bash
export GATEWAY_LAN_IFACE=ens160
export GATEWAY_DOWNSTREAM_SUBNET=192.168.119.0/24
export GATEWAY_VPN_VIRTUAL_IP=10.10.10.1

sudo -E ikev2 gateway enable
```

If more than one managed profile is connected, set `GATEWAY_REMOTE_VPN_SERVER` to the active profile's IPv4 endpoint. `GATEWAY_LAN_GATEWAY` can also be set explicitly when automatic next-hop discovery is unsuitable. These values must match the live host and upstream router layout.

To change the selected prefix later, disable and re-enable Gateway Mode with the new prefix.

### Why SNAT is used

The XFRM CHILD_SA uses the client virtual IP (for example `10.10.10.1`) as its local source selector. Forwarded packets arrive with their original downstream source, so they do not match that selector. Gateway Mode therefore SNATs traffic from the configured downstream prefix in `POSTROUTING` before XFRM policy lookup, changing its source to the assigned VPN virtual IP.

Do not add `-m policy --dir out --pol ipsec` to these SNAT rules: before translation, the forwarded source does not match the IPsec selector, so that condition prevents the rule from matching. The VPN endpoint is excluded so its outer IKE/ESP traffic stays on the physical LAN route.

```bash
iptables -t nat -A POSTROUTING -s "$GATEWAY_DOWNSTREAM_SUBNET" -o ens160 ! -d "$GATEWAY_REMOTE_VPN_SERVER/32" -j SNAT --to-source "$GATEWAY_VPN_VIRTUAL_IP"
```

The server's existing full-tunnel selector and VPN-client NAT then carry all traffic from the selected prefix through the tunnel. Check the `POSTROUTING` packet counter before and after a downstream test; a zero counter means the packets are not matching the SNAT rule.

### Routing safety

The gateway protects the IKEv2 endpoint route in the routing table selected by the kernel (StrongSwan commonly uses table `220`) so tunnel control traffic continues to leave through the normal LAN gateway instead of looping back into the VPN:

```bash
sudo ikev2 gateway status
sudo ip route show table 220
```

This keeps the tunnel maintenance path separate from forwarded traffic and avoids routing loops. Two lower-precedence XFRM block policies (`0.0.0.0/1` and `128.0.0.0/1`) cover the VPN virtual IP's destinations without colliding with the active `0.0.0.0/0` CHILD_SA selector. They prevent forwarded packets from falling back to cleartext if the CHILD_SA disappears; the active IPsec policy takes precedence while the tunnel is up.

### Validation checklist

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
- any separately configured SOCKS5 service still responds on its configured endpoint

### Disable and rollback

To remove only the Gateway Mode changes:

```bash
sudo ikev2 gateway disable
```

This removes the added forwarding/NAT rules and the protected route while leaving the existing normal IKEv2 profile and SOCKS5 setup intact.

---

## Uninstall the Linux client utility

Run the Linux utility:

```bash
sudo ./ikev2-linux-client.sh
```

Choose:

```text
8) Uninstall Utility
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
