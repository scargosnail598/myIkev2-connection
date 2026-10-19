"""Fixed local probes and bounded administrative reports, without credentials."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import tempfile
import time

from webui.metrics import MetricsReader, read_text
from webui.sessions import SessionReader
from telegram_bot.activity import saved_usage, session_activity


SNAPSHOT = Path("/run/ikev2-telegram/status.json")
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_AGE = 90


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class StatusCollector:
    def __init__(self, proc=Path("/proc"), state=Path("/var/lib/ikev2-easy-installer/state.env"),
                 runner=subprocess.run, clock=time.time):
        self.proc, self.state, self.runner, self.clock = proc, state, runner, clock

    def _command(self, argv):
        try:
            result = self.runner(argv, capture_output=True, text=True, check=False, timeout=5,
                                 env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
            if result.returncode == 0 and len(result.stdout) <= 1024 * 1024:
                return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None

    def _installer_version(self):
        try:
            lines = [line.split("=", 1)[1] for line in read_text(self.state, "utf-8").splitlines()
                     if line.startswith("INSTALLER_VERSION=")]
            values = shlex.split(lines[0]) if len(lines) == 1 else []
            if len(values) == 1 and re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,63}", values[0]):
                return values[0]
        except (OSError, UnicodeError, ValueError):
            pass
        return None

    def _addresses(self):
        output = self._command(["/usr/sbin/ip", "-j", "address", "show", "scope", "global"])
        try:
            addresses = {4: set(), 6: set()}
            for interface in json.loads(output):
                for entry in interface.get("addr_info", []):
                    if entry.get("scope") != "global":
                        continue
                    address = ipaddress.ip_address(entry["local"])
                    if not address.is_loopback and not address.is_link_local:
                        addresses[address.version].add(address)
            return {"ipv4": [str(value) for value in sorted(addresses[4])][:20],
                    "ipv6": [str(value) for value in sorted(addresses[6])][:20]}
        except (TypeError, ValueError, KeyError, AttributeError):
            return {"ipv4": None, "ipv6": None}

    def _sockets(self):
        counts = {"tcp": None, "udp": None}
        try:
            v4 = read_text(self.proc / "net/sockstat")
            try:
                v6 = read_text(self.proc / "net/sockstat6")
            except FileNotFoundError:
                v6 = ""  # IPv6 can be disabled in the kernel.
            for protocol in ("TCP", "UDP"):
                matches = re.findall(rf"^{protocol}: inuse (\d+)\b", v4, re.MULTILINE)
                extra = re.findall(rf"^{protocol}6: inuse (\d+)\b", v6, re.MULTILINE)
                if len(matches) == 1 and (not v6 or len(extra) == 1):
                    counts[protocol.lower()] = int(matches[0]) + (int(extra[0]) if extra else 0)
        except (OSError, UnicodeError, ValueError):
            pass
        return counts

    def collect(self):
        metrics = MetricsReader(proc=self.proc, state=self.state).snapshot()
        sessions = SessionReader(runner=self.runner).snapshot()
        known_sessions = sessions.get("ok") is True
        version = self._command(["/usr/sbin/ipsec", "--version"])
        match = re.search(r"strongSwan\s+U?([0-9][A-Za-z0-9.+_-]{0,63})", version or "")
        uptime, load = None, None
        try:
            value = float(read_text(self.proc / "uptime").split()[0])
            uptime = int(value) if number(value) else None
        except (OSError, UnicodeError, ValueError, IndexError):
            pass
        try:
            values = [float(value) for value in read_text(self.proc / "loadavg").split()[:3]]
            if len(values) == 3 and all(number(value) for value in values):
                load = values
        except (OSError, UnicodeError, ValueError):
            pass
        return {
            "version": 1, "collected_at": self.clock(), "host": socket.gethostname()[:64],
            "installer_version": self._installer_version(), "strongswan_version": match[1] if match else None,
            **self._addresses(), "uptime_seconds": uptime, "load": load,
            "memory": metrics["memory"],
            "network": {key: metrics["network"][key] for key in ("interface", "rx_bytes", "tx_bytes")},
            "online_sessions": len(sessions["sessions"]) if known_sessions else None,
            "online_users": len({item["username"] for item in sessions["sessions"]}) if known_sessions else None,
            "vpn_status": ("running" if sessions["running"] else "stopped") if known_sessions else "unknown",
            **session_activity(sessions), **saved_usage(self.state.parent / "traffic.tsv"),
            **self._sockets(),
        }


def write_snapshot(path, payload):
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise ValueError("Snapshot directory must be owned by the collector and not writable by others.")
    descriptor, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o640)
            raw = json.dumps(payload, allow_nan=False)
            if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
                raise ValueError("Status snapshot exceeds its bounded size.")
            output.write(raw)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_snapshot(path=SNAPSHOT, clock=time.time):
    try:
        with path.open(encoding="utf-8") as source:
            raw = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            return None
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
                or not number(data.get("collected_at")) or not 0 <= clock() - data["collected_at"] <= MAX_AGE):
            return None
        return data
    except (OSError, UnicodeError, ValueError):
        return None


def text(value):
    if not isinstance(value, str) or not value:
        return "N/A"
    return "".join(character for character in value[:80] if character.isprintable()) or "N/A"


def size(value):
    if not number(value):
        return "N/A"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.2f} {unit}"
        value /= 1024


def format_status(data, clock=time.time):
    if data is None:
        return "⚠️ Server status unavailable or stale. Check ikev2-telegram-collect.timer on the server."
    uptime = data.get("uptime_seconds")
    duration = "N/A"
    if number(uptime):
        days, remainder = divmod(int(uptime), 86400)
        hours, remainder = divmod(remainder, 3600)
        duration = f"{days}d {hours}h {remainder // 60}m"
    load = data.get("load")
    load_text = ", ".join(f"{value:.2f}" for value in load) if (
        isinstance(load, list) and len(load) == 3 and all(number(value) for value in load)) else "N/A"
    memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
    network = data.get("network") if isinstance(data.get("network"), dict) else {}
    rx, tx = network.get("rx_bytes"), network.get("tx_bytes")
    total = rx + tx if number(rx) and number(tx) else None

    def count(key):
        value = data.get(key)
        return str(value) if type(value) is int and value >= 0 else "N/A"

    def addresses(key):
        values = data.get(key)
        if not isinstance(values, list):
            return "N/A"
        return ", ".join(text(value) for value in values[:20]) if values else "None"

    age = max(0, int(clock() - data["collected_at"]))
    return "\n".join([
        f"🖥 Host: {text(data.get('host'))}",
        f"🚀 IKEv2 Installer: {text(data.get('installer_version'))}",
        f"📡 StrongSwan: {text(data.get('strongswan_version'))}",
        f"🌐 IPv4: {addresses('ipv4')}", f"🌐 IPv6: {addresses('ipv6')}",
        f"⏳ Uptime: {duration}", f"📈 System Load: {load_text}",
        f"📋 RAM: {size(memory.get('used_bytes'))} / {size(memory.get('total_bytes'))}",
        f"👥 Online Clients: {count('online_sessions')} sessions / {count('online_users')} users",
        f"🔹 TCP sockets: {count('tcp')}", f"🔸 UDP sockets: {count('udp')}",
        f"🚦 Traffic ({text(network.get('interface'))}): {size(total)} (↑ {size(tx)}, ↓ {size(rx)})",
        f"ℹ️ VPN Status: {text(data.get('vpn_status'))}", f"🕒 Sample age: {age}s",
        "\nTraffic: all selected-interface traffic since its counters began/reset; not lifetime VPN usage.",
        "IPs: local global-scope addresses (up to 20 per family); public NAT IP is not discovered.",
    ])


def main():
    parser = argparse.ArgumentParser(description="Collect a fixed, read-only IKEv2 Telegram status snapshot")
    parser.add_argument("--output", type=Path, default=SNAPSHOT)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, "The status collector must run as root.\n")
    write_snapshot(args.output, StatusCollector().collect())


if __name__ == "__main__":
    main()
