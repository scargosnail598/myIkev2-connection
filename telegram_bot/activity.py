"""Bounded read-only VPN session details and saved CLI traffic; never read secrets."""

import os
from pathlib import Path
import re
import stat

from webui.sessions import IDENTITY

MAX_RECORDS = 1000
MAX_TRAFFIC_FILE = 2 * 1024 * 1024
MAX_COUNTER = 2**64 - 1


def counter(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNTER else None


def session_activity(snapshot):
    if snapshot.get("ok") is not True:
        return {"sessions": None, "online": None, "sessions_total": None, "online_total": None}
    sessions, users = [], {}
    for item in snapshot["sessions"]:
        name = item["username"]
        if not isinstance(name, str) or not IDENTITY.fullmatch(name):
            continue
        row = {"username": name, "id": item["id"][:64], "duration": item["duration"][:64],
               "vpn_addresses": item["vpn_addresses"][:4],
               "rx_bytes": counter(item.get("rx_bytes")), "tx_bytes": counter(item.get("tx_bytes"))}
        sessions.append(row)
        user = users.setdefault(name, {"username": name, "sessions": 0, "rx_bytes": 0, "tx_bytes": 0})
        user["sessions"] += 1
        for key in ("rx_bytes", "tx_bytes"):
            user[key] = user[key] + row[key] if user[key] is not None and row[key] is not None else None
    sessions.sort(key=lambda item: (item["username"].casefold(), item["username"], item["id"]))
    online = sorted(users.values(), key=lambda item: (item["username"].casefold(), item["username"]))
    return {"sessions": sessions[:MAX_RECORDS], "online": online[:MAX_RECORDS],
            "sessions_total": len(sessions), "online_total": len(online)}


def saved_usage(path=Path("/var/lib/ikev2-easy-installer/traffic.tsv")):
    unavailable = {"usage": None, "usage_total": None, "usage_saved_at": None}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                return unavailable
            raw = source.read(MAX_TRAFFIC_FILE + 1)
        if len(raw) > MAX_TRAFFIC_FILE:
            return unavailable
        users = {}
        for line in raw.decode("ascii").splitlines():
            if not line:
                continue
            fields = line.split("\t")
            if fields[0] == "SESSION":
                continue  # These are accounting baselines, not additional traffic.
            if (len(fields) != 4 or fields[0] != "TOTAL" or not IDENTITY.fullmatch(fields[1])
                    or fields[1] in users or any(not re.fullmatch(r"[0-9]{1,20}", value) for value in fields[2:])):
                return unavailable
            rx, tx = map(int, fields[2:])
            if counter(rx) is None or counter(tx) is None:
                return unavailable
            users[fields[1]] = {"username": fields[1], "rx_bytes": rx, "tx_bytes": tx}
        rows = sorted(users.values(), key=lambda item: (-(item["rx_bytes"] + item["tx_bytes"]),
                                                       item["username"].casefold(), item["username"]))
        return {"usage": rows[:MAX_RECORDS], "usage_total": len(rows), "usage_saved_at": info.st_mtime}
    except (OSError, UnicodeError, ValueError):
        return unavailable
