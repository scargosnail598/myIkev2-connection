"""Plain-text, single-message reports with explicit paging and accounting scope."""

import re
import time

from telegram_bot.status import format_status, number, size, text
from webui.sessions import IDENTITY

PAGE_SIZE = 5
COMMANDS = [
    {"command": "status", "description": "Server health and resources"},
    {"command": "sessions", "description": "Active VPN sessions, VPN IPs and traffic"},
    {"command": "online", "description": "Online users and current-session traffic"},
    {"command": "usage", "description": "Saved cumulative traffic per user"},
    {"command": "user", "description": "One user's summary: /user username"},
    {"command": "help", "description": "Commands and report usage"},
]
HELP = "\n".join([
    "IKEv2 server monitor",
    "/status — server health and resources",
    "/sessions [page] — active sessions, VPN IPs, age and current traffic",
    "/online [page] — online users and combined current-session traffic",
    "/usage [page] — saved cumulative user traffic, largest first",
    "/user username — one user's connection and usage summary",
    "/help — show commands",
    "\nAliases: /users = /online, /traffic = /usage.",
    "↑ TX / ↓ RX are from the server's perspective. Reports refresh every 30s.",
])


def traffic(row):
    rx, tx = row.get("rx_bytes"), row.get("tx_bytes")
    total = rx + tx if number(rx) and number(tx) else None
    return f"{size(total)} (↑ {size(tx)}, ↓ {size(rx)})"


def sample_age(data, clock):
    return f"Snapshot: {max(0, int(clock() - data['collected_at']))}s ago."


def usage_note(data, clock):
    saved = data.get("usage_saved_at")
    age = f"Saved {max(0, int(clock() - saved))}s ago. " if number(saved) else ""
    return age + "Cumulative CLI totals; may include removed users. Separate from panel quota usage/resets. Sampling can miss short sessions."


def session_note():
    return "Current installed CHILD_SA traffic; counters reset on rekey/reconnect. N/A means unavailable. ↑ TX / ↓ RX: server perspective."


def page_report(data, command, page, clock):
    key = {"sessions": "sessions", "online": "online", "usage": "usage"}[command]
    title = {"sessions": "📡 Active VPN sessions", "online": "👥 Online VPN users", "usage": "🚦 Saved user traffic"}[command]
    rows = data.get(key)
    if not isinstance(rows, list):
        return title + "\nUnavailable. Check the collector" + (" and CLI traffic snapshot timer." if command == "usage" else ".")
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        return f"Page {page} is unavailable. Use /{command} 1 (pages: {pages})."
    total = data.get(key + "_total", len(rows))
    lines = [title, f"{total} total · Page {page}/{pages}"]
    for index, row in enumerate(rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], (page - 1) * PAGE_SIZE + 1):
        name = text(row.get("username"))
        if command == "sessions":
            addresses = row.get("vpn_addresses", [])
            lines += [f"\n{index}. {name} · {text(row.get('id'))}",
                      f"Connected: {text(row.get('duration'))}",
                      "VPN: " + (", ".join(text(value) for value in addresses[:4]) or "N/A"),
                      "Session: " + traffic(row)]
        elif command == "online":
            lines += [f"\n{index}. {name} · {row.get('sessions', 'N/A')} session(s)", "Current: " + traffic(row)]
        else:
            lines += [f"\n{index}. {name}", "Saved: " + traffic(row)]
    if not rows:
        lines.append("No saved traffic records." if command == "usage" else "No active VPN connections.")
    if page < pages:
        lines.append(f"\nNext: /{command} {page + 1}")
    if type(total) is int and total > len(rows):
        lines.append(f"Report limited to {len(rows)} records from this snapshot.")
    lines += ["\n" + sample_age(data, clock), usage_note(data, clock) if command == "usage" else session_note()]
    return "\n".join(lines)


def user_report(data, username, clock):
    online = data.get("online")
    usage = data.get("usage")
    current = next((row for row in online if row.get("username") == username), None) if isinstance(online, list) else None
    saved = next((row for row in usage if row.get("username") == username), None) if isinstance(usage, list) else None
    online_complete = isinstance(online, list) and data.get("online_total", len(online)) == len(online)
    lines = ["👤 VPN user: " + username]
    lines.append("State: " + ("Online" if current else "Offline" if online_complete else "Unknown / not in bounded snapshot"))
    if current:
        lines += [f"Active sessions: {current['sessions']}", "Current: " + traffic(current)]
    else:
        lines.append("Current traffic: no active sessions" if online_complete else "Current traffic: N/A")
    lines.append("Saved cumulative: " + traffic(saved) if saved else "Saved cumulative: N/A (no accessible saved record)")
    lines += ["\n" + sample_age(data, clock), session_note(), usage_note(data, clock)]
    return "\n".join(lines)


def render(command, arguments, read, clock=time.time):
    command = {"users": "online", "traffic": "usage"}.get(command, command)
    if command in {"start", "help"} or command not in {"status", "sessions", "online", "usage", "user"}:
        return HELP
    page = 1
    if command in {"sessions", "online", "usage"}:
        if len(arguments) > 1 or (arguments and not re.fullmatch(r"[1-9][0-9]{0,5}", arguments[0])):
            return f"Usage: /{command} [page], e.g. /{command} 2"
        page = int(arguments[0]) if arguments else 1
    elif command == "user":
        if len(arguments) != 1 or not IDENTITY.fullmatch(arguments[0]):
            return "Usage: /user username (1–64 letters, digits, dot, underscore, @ or hyphen)."
    elif arguments:
        return "Usage: /status"
    data = read()
    if data is None:
        return format_status(None, clock)
    if command == "status":
        return format_status(data, clock)
    if command == "user":
        return user_report(data, arguments[0], clock)
    return page_report(data, command, page, clock)
