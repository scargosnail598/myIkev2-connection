"""Read-only snapshots of the installer's managed StrongSwan sessions."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
import re
import subprocess


PAGE_SIZE = 20
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
HEADER = re.compile(r"^([^\s:]+)\[(\d+)\]:\s+(ESTABLISHED|CONNECTING|REKEYING|REKEYED|DELETING|DESTROYING)\b\s*(.*)$")
CHILD = re.compile(r"^ikev2-eap\{(\d+)\}:\s+(.*)$")
IDENTITY = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def address(value: str) -> str | None:
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def parse_sessions(output: str) -> list[dict]:
    """Return established managed SAs only; never expose raw daemon output."""
    sessions = []
    current = None
    children = {}

    def finish():
        if current is None or current["username"] is None:
            return
        installed = [child for child in children.values() if child.get("installed")]
        for key in ("rx_bytes", "tx_bytes"):
            values = [child.get(key) for child in installed]
            current[key] = sum(values) if values and all(value is not None for value in values) else None
        current["vpn_addresses"] = sorted({ip for child in installed for ip in child.get("addresses", [])})
        sessions.append(current)

    for raw in output.splitlines():
        line = raw.strip()
        header = HEADER.match(line)
        if header:
            finish()
            current, children = None, {}
            name, number, state, detail = header.groups()
            if name != "ikev2-eap" or state != "ESTABLISHED":
                continue
            remote = re.search(r"\.\.\.([^\[\s]+)\[([^\]]+)\]", detail)
            if not remote:
                continue
            current = {"id": f"ikev2-eap[{number}]", "username": remote[2] if IDENTITY.fullmatch(remote[2]) else None,
                       "public_ip": address(remote[1]), "duration": detail.split(",", 1)[0][:64]}
            continue
        if current is not None:
            prefix = f"{current['id']}: Remote EAP identity: "
            if line.startswith(prefix):
                identity = line[len(prefix):]
                current["username"] = identity if IDENTITY.fullmatch(identity) else None
                continue
        child_match = CHILD.match(line)
        if current is None or child_match is None:
            continue
        number, detail = child_match.groups()
        child = children.setdefault(number, {})
        if detail.startswith("INSTALLED,"):
            child["installed"] = True
        counters = re.search(r"(\d+) bytes_i\b.*?([0-9]+) bytes_o\b", detail)
        if counters:
            child["rx_bytes"], child["tx_bytes"] = map(int, counters.groups())
        if "===" in detail:
            addresses = []
            for selector in detail.split("===", 1)[1].split():
                try:
                    network = ip_network(selector.rstrip(","), strict=False)
                except ValueError:
                    continue
                if network.prefixlen == network.max_prefixlen:
                    addresses.append(str(network.network_address))
            child["addresses"] = addresses
    finish()
    return sessions


class SessionReader:
    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def list_sessions(self, search: str = "", page: int = 0) -> dict:
        if not isinstance(search, str) or len(search) > 64 or type(page) is not int or not 0 <= page <= 100000:
            return {"ok": False, "status": 400, "message": "Invalid session search or page."}
        try:
            state = self.runner(["/usr/bin/systemctl", "is-active", "strongswan-starter.service"],
                                capture_output=True, text=True, check=False, timeout=3)
            if state.stdout.strip() in {"inactive", "failed"}:
                sessions, running = [], False
            elif state.returncode == 0 and state.stdout.strip() == "active":
                result = self.runner(["/usr/sbin/ipsec", "statusall", "ikev2-eap"],
                                     capture_output=True, text=True, check=False, timeout=5,
                                     env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
                if result.returncode != 0 or len(result.stdout.encode()) > MAX_OUTPUT_BYTES or "Security Associations (" not in result.stdout:
                    raise ValueError("Invalid session status")
                sessions, running = parse_sessions(result.stdout), True
            else:
                raise ValueError("Unknown service status")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {"ok": False, "status": 503, "message": "VPN session status is unavailable. Try refreshing."}
        sessions.sort(key=lambda item: (item["username"].casefold(), item["id"]))
        matched = [item for item in sessions if search.casefold() in item["username"].casefold()]
        pages = max(1, (len(matched) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        return {"ok": True, "sessions": matched[page * PAGE_SIZE:(page + 1) * PAGE_SIZE],
                "total": len(sessions), "matched": len(matched), "page": page, "pages": pages,
                "running": running}
