"""Fixed, read-only server health checks; raw command output stays in the broker."""

from datetime import datetime, timezone
from pathlib import Path
import re
import ssl
import subprocess


STATE_FILE = Path("/var/lib/ikev2-easy-installer/state.env")
SERVER_CERT = Path("/etc/ipsec.d/certs/ikev2-installer-server-cert.pem")
FORWARDING = Path("/proc/sys/net/ipv4/ip_forward")
UDP_FILES = (Path("/proc/net/udp"), Path("/proc/net/udp6"))
COMMAND_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def check(identifier, label, status, detail, guidance=""):
    return {"id": identifier, "label": label, "status": status, "detail": detail, "guidance": guidance}


class DiagnosticsReader:
    def __init__(self, runner=subprocess.run, state=STATE_FILE, certificate=SERVER_CERT,
                 forwarding=FORWARDING, udp_files=UDP_FILES, clock=None):
        self.runner = runner
        self.state = state
        self.certificate = certificate
        self.forwarding = forwarding
        self.udp_files = udp_files
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _run(self, command, timeout=3):
        try:
            result = self.runner(command, capture_output=True, text=True, check=False,
                                 timeout=timeout, env=COMMAND_ENV)
            if result.returncode != 0 or len(result.stdout) > 2 * 1024 * 1024:
                return None
            return result.stdout
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            return None

    def snapshot(self):
        checks = []
        try:
            installed = self.state.is_file()
            checks.append(check("installation", "Managed installation", "pass" if installed else "warn",
                                "Installer state is present." if installed else "No managed installation was found.",
                                "Use the server installer to provision or restore the VPN." if not installed else ""))
        except OSError:
            checks.append(check("installation", "Managed installation", "unknown", "Installer state could not be inspected."))
        vpn_active = False
        for identifier, label, unit, optional in (
            ("vpn_service", "VPN service", "strongswan-starter.service", False),
            ("firewall_service", "Firewall service", "ikev2-vpn-firewall.service", False),
            ("proxy_service", "SOCKS5 service", "ikev2-vpn-proxy.service", True),
        ):
            result, active = self._service(identifier, label, unit, optional)
            checks.append(result)
            if identifier == "vpn_service":
                vpn_active = active
        checks.extend((self._connection(vpn_active), self._udp(vpn_active), self._forwarding(), self._certificate()))
        return {"ok": True, "checked_at": self.clock().isoformat(), "checks": checks}

    def _service(self, identifier, label, unit, optional):
        output = self._run(["/usr/bin/systemctl", "show", "--property=LoadState,ActiveState,UnitFileState", unit])
        properties = dict(line.split("=", 1) for line in (output or "").splitlines() if "=" in line)
        load, active = properties.get("LoadState"), properties.get("ActiveState")
        if load == "not-found":
            return check(identifier, label, "pass" if optional else "fail",
                         "Optional proxy is not installed." if optional else "The required service is not installed.",
                         "Run the server installer to restore the managed service." if not optional else ""), False
        if load != "loaded" or active not in {"active", "inactive", "failed", "activating", "deactivating"}:
            return check(identifier, label, "unknown", "Service state could not be determined."), False
        if optional and active == "inactive" and properties.get("UnitFileState") in {"disabled", "masked"}:
            return check(identifier, label, "pass", "Optional proxy is disabled."), False
        status = "pass" if active == "active" else "fail" if active == "failed" else "warn"
        return check(identifier, label, status, f"Service state: {active}.",
                     "Review service health in Overview and start the service if it is needed." if status != "pass" else ""), active == "active"

    def _connection(self, active):
        if not active:
            return check("connection", "IKEv2 configuration", "unknown", "The VPN service is not running.",
                         "Start the VPN service to inspect its loaded connection.")
        output = self._run(["/usr/sbin/ipsec", "statusall", "ikev2-eap"], timeout=5)
        if output is None or "Security Associations (" not in output:
            return check("connection", "IKEv2 configuration", "unknown", "StrongSwan did not return a usable status.",
                         "Refresh this page, then inspect StrongSwan with the server installer if the problem persists.")
        loaded = re.search(r"^\s*ikev2-eap:\s", output, re.MULTILINE) is not None
        return check("connection", "IKEv2 configuration", "pass" if loaded else "fail",
                     "The managed ikev2-eap connection is loaded." if loaded else "The managed ikev2-eap connection was not found.",
                     "Run the server installer's diagnostics to review the VPN configuration." if not loaded else "")

    def _udp(self, active):
        ports, readable = set(), False
        try:
            for path in self.udp_files:
                try:
                    with path.open(encoding="ascii") as source:
                        content = source.read(2 * 1024 * 1024 + 1)
                except FileNotFoundError:
                    continue
                if len(content) > 2 * 1024 * 1024 or "local_address" not in content:
                    raise ValueError("Invalid socket table")
                readable = True
                for line in content.splitlines()[1:]:
                    fields = line.split()
                    if len(fields) >= 4:
                        match = re.fullmatch(r"(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32}):([0-9A-Fa-f]{4})", fields[1])
                        if match:
                            ports.add(int(match[1], 16))
            if not readable:
                raise ValueError("No socket tables")
        except (OSError, UnicodeError, ValueError):
            return check("udp_ports", "IKEv2 UDP ports", "unknown", "Local UDP socket information is unavailable.")
        missing = {500, 4500} - ports
        if not missing:
            return check("udp_ports", "IKEv2 UDP ports", "pass", "UDP 500 and 4500 are bound locally.",
                         "Local binding does not verify public reachability or identify the owning process.")
        return check("udp_ports", "IKEv2 UDP ports", "fail" if active else "warn",
                     "No local binding for UDP " + ", ".join(map(str, sorted(missing))) + ".",
                     "Check the VPN service, then verify UDP 500 and 4500 in the provider firewall from a VPN client.")

    def _forwarding(self):
        try:
            value = self.forwarding.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            value = None
        if value not in {"0", "1"}:
            return check("ipv4_forwarding", "IPv4 forwarding", "unknown", "The forwarding setting could not be read.")
        return check("ipv4_forwarding", "IPv4 forwarding", "pass" if value == "1" else "fail",
                     "IPv4 forwarding is enabled." if value == "1" else "IPv4 forwarding is disabled.",
                     "Use the server installer's diagnostics to review routing and managed forwarding settings." if value == "0" else "")

    def _certificate(self):
        output = self._run(["/usr/bin/openssl", "x509", "-in", str(self.certificate), "-noout", "-dates"])
        try:
            dates = dict(line.split("=", 1) for line in (output or "").splitlines() if "=" in line)
            start, end = (datetime.fromtimestamp(ssl.cert_time_to_seconds(dates[key]), timezone.utc)
                          for key in ("notBefore", "notAfter"))
            now = self.clock()
        except (KeyError, ValueError, OverflowError):
            return check("server_certificate", "Server certificate dates", "unknown", "The managed server certificate could not be inspected.",
                         "Run the server installer's certificate diagnostics. No private keys are read by this check.")
        if now < start or now >= end:
            return check("server_certificate", "Server certificate dates", "fail",
                         "The certificate is not valid yet." if now < start else "The server certificate has expired.",
                         "Check the server clock and renew or rotate the certificate using the installer.")
        remaining = (end - now).days
        return check("server_certificate", "Server certificate dates", "warn" if remaining <= 30 else "pass",
                     f"Certificate expires on {end.date().isoformat()} ({remaining} days remaining).",
                     "Review certificate renewal before it expires." if remaining <= 30 else "")
