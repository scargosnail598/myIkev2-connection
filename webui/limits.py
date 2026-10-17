"""Persistent traffic quotas and first-connection validity; only the short-lived root helper changes secrets."""

from __future__ import annotations

from contextlib import contextmanager
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

from webui.sessions import SessionReader
from webui.users import (SUSPENDED_PREFIX, USERNAME, UserError, UserStore,
                         atomic_private, parse_users, read_private)

LIMITS_FILE = Path("/var/lib/ikev2-web/limits.json")
MAX_LIMIT = 2 ** 53 - 1
SAMPLE_INTERVAL = 5
MAX_VALIDITY_SECONDS = 36600 * 86400


def validate_limits(request):
    operation = request.get("operation")
    if operation not in {"limits_set", "usage_reset"}:
        raise UserError("invalid_operation", "Unsupported limits operation.")
    allowed = {"version", "action", "operation", "username", "revision", "limits_revision",
               "expected_digest", "request_id"}
    if operation == "limits_set":
        allowed |= {"byte_limit", "validity_seconds"}
        for key in ("byte_limit", "validity_seconds"):
            value = request.get(key)
            if value is not None and (type(value) is not int or not 1 <= value <= (MAX_VALIDITY_SECONDS if key == "validity_seconds" else MAX_LIMIT)):
                raise UserError("invalid_limit", "Limits must be positive whole numbers or null for unlimited.")
        if not {"byte_limit", "validity_seconds"} <= set(request):
            raise UserError("invalid_limit", "Specify both limits; use null for unlimited.")
    if set(request) - allowed:
        raise UserError("invalid_fields", "Unsupported limits fields.")
    if not isinstance(request.get("username"), str) or not USERNAME.fullmatch(request["username"]):
        raise UserError("invalid_username", "Invalid VPN username.")
    if not isinstance(request.get("limits_revision"), str) or not re.fullmatch(r"[0-9a-f]{32}", request["limits_revision"]):
        raise UserError("conflict", "Limits changed. Refresh users before trying again.", 409)


def empty_user(now):
    return {"byte_limit": None, "validity_seconds": None, "used_bytes": 0,
            "first_connected_at": None, "reset_at": now}


def blocked_reason(user, now=None):
    now = time.time() if now is None else now
    if user["byte_limit"] is not None and user["used_bytes"] >= user["byte_limit"]:
        return "traffic"
    if (user["validity_seconds"] is not None and user["first_connected_at"] is not None
            and now >= user["first_connected_at"] + user["validity_seconds"]):
        return "expired"
    return None


class LimitsStore:
    def __init__(self, path=LIMITS_FILE):
        self.path = path

    def load(self):
        try:
            raw = read_private(self.path)
        except FileNotFoundError:
            # A fixed initial revision permits first configuration before any timer sample.
            return {"version": 1, "revision": "0" * 32, "users": {}, "samples": {},
                    "online": [], "epoch": "", "sampled_at": None,
                    "reload_pending": False, "enforcement_error": False}
        try:
            data = json.loads(raw)
            if (data.get("version") != 1 or not isinstance(data.get("users"), dict)
                    or not isinstance(data.get("samples"), dict)
                    or not re.fullmatch(r"[0-9a-f]{32}", data.get("revision", ""))
                    or not isinstance(data.get("online"), list) or not isinstance(data.get("epoch"), str)
                    or type(data.get("reload_pending")) is not bool
                    or type(data.get("enforcement_error")) is not bool):
                raise ValueError("Invalid limits state")
            for timestamp in (data.get("sampled_at"),):
                if timestamp is not None and (type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0):
                    raise ValueError("Invalid sample timestamp")
            for name, user in data["users"].items():
                if not USERNAME.fullmatch(name) or not isinstance(user, dict):
                    raise ValueError("Invalid identity")
                for key in ("byte_limit", "validity_seconds"):
                    value = user[key]
                    if value is not None and (type(value) is not int or not 1 <= value <= (MAX_VALIDITY_SECONDS if key == "validity_seconds" else MAX_LIMIT)):
                        raise ValueError("Invalid limit")
                for key in ("used_bytes", "reset_at"):
                    value = user[key]
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("Invalid usage")
                if type(user["used_bytes"]) is not int:
                    raise ValueError("Invalid traffic counter")
                started = user["first_connected_at"]
                if started is not None and (type(started) not in (int, float) or not math.isfinite(started) or started < 0):
                    raise ValueError("Invalid activation timestamp")
            for record in data["samples"].values():
                if (not isinstance(record, dict) or not USERNAME.fullmatch(record["username"])
                        or any(type(record[key]) is not int or record[key] < 0 for key in ("rx", "tx"))):
                    raise ValueError("Invalid sample")
            if not all(isinstance(name, str) and USERNAME.fullmatch(name) for name in data["online"]):
                raise ValueError("Invalid online list")
            return data
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise UserError("limits_unavailable", "Saved limits cannot be read safely. Check the limits service.", 503) from error

    def save(self, data):
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise UserError("unsafe_limits", "Private limits storage is unavailable.", 503)
        raw = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 2 * 1024 * 1024:
            raise UserError("limits_storage_limit", "Limits storage exceeds the supported size.", 503)
        atomic_private(self.path, raw)

    def forget(self, username):
        if not self.path.exists():
            return
        data = self.load()
        data["users"].pop(username, None)
        data["samples"] = {key: value for key, value in data["samples"].items() if value["username"] != username}
        data["online"] = [name for name in data["online"] if name != username]
        data["revision"] = secrets.token_hex(16)
        self.save(data)

    def catalog(self, names, lines, indices):
        data = self.load()
        now = time.time()
        return {"limits_revision": data["revision"], "limits_sampled_at": data["sampled_at"],
                "limits_healthy": (data["sampled_at"] is not None and 0 <= now - data["sampled_at"] < 30
                                   and not data["enforcement_error"] and not data["reload_pending"]),
                "limits": {name: {**data["users"].get(name, empty_user(now)),
                                  "blocked": blocked_reason(data["users"].get(name, empty_user(now)), now),
                                  "suspended": lines[indices[name]].startswith(SUSPENDED_PREFIX)} for name in names}}


def daemon_epoch():
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    try:
        pid = Path("/run/charon.pid").read_text().strip()
        if not re.fullmatch(r"[0-9]{1,10}", pid):
            raise ValueError("Invalid daemon pid")
        # /proc stat's command may contain spaces, hence split after its last ')'.
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        return f"{boot}:{pid}:{start}"
    except FileNotFoundError:
        return f"{boot}:stopped"


@contextmanager
def account_lock(directory=LIMITS_FILE.parent, blocking=True):
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise UserError("unsafe_queue", "Private account operation storage is unavailable.", 503)
    descriptor = os.open(directory / "apply.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


class LimitsController:
    def __init__(self, store=None, users=None, reader=None, runner=subprocess.run,
                 clock=time.time, epoch=daemon_epoch):
        self.store, self.users = store or LimitsStore(), users or UserStore()
        self.reader, self.runner = reader or SessionReader(), runner
        self.clock, self.epoch = clock, epoch

    def sample(self, data, configured, snapshot, epoch):
        now = self.clock()
        first = data["sampled_at"] is None
        same_epoch = data["epoch"] == epoch
        previous = data["samples"] if same_epoch else {}
        new_accounts = set(configured) - data["users"].keys()
        data["users"] = {name: data["users"].get(name, empty_user(now)) for name in configured}
        samples, online = {}, set()
        complete = True
        for session in snapshot["sessions"]:
            name = session["username"]
            if name not in configured:
                continue
            online.add(name)
            user = data["users"][name]
            if user["validity_seconds"] is not None and user["first_connected_at"] is None:
                user["first_connected_at"] = now
            for child in session["children"]:
                rx, tx = child["rx_bytes"], child["tx_bytes"]
                key = f"{session['id']}:{child['id']}"
                if type(rx) is not int or type(tx) is not int or rx < 0 or tx < 0:
                    complete = False
                    if key in previous:
                        samples[key] = previous[key]
                    continue
                old = previous.get(key)
                # First installation starts at zero even for already connected clients.
                # New CHILD SAs after this baseline contribute their whole observed totals.
                if not first and name not in new_accounts:
                    if old and old["username"] == name:
                        delta = (rx - old["rx"] if rx >= old["rx"] else rx) + (tx - old["tx"] if tx >= old["tx"] else tx)
                    else:
                        delta = rx + tx
                    data["users"][name]["used_bytes"] += delta
                samples[key] = {"username": name, "rx": rx, "tx": tx}
        data.update(samples=samples, online=sorted(online), epoch=epoch,
                    sampled_at=now, enforcement_error=not complete)

    def _snapshot(self):
        snapshot = self.reader.snapshot(accounting=True)
        if snapshot.get("ok") is not True:
            raise UserError("accounting_unavailable", "VPN accounting is unavailable. Refresh and check the limits service.", 503)
        return snapshot

    def enforce(self, data, raw, snapshot):
        lines, configured = parse_users(raw)
        if self.epoch() != data["epoch"]:
            raise UserError("accounting_unavailable", "The VPN daemon restarted during accounting. Retry after refreshing users.", 503)
        blocked = {name for name, user in data["users"].items() if name in configured and blocked_reason(user, self.clock())}
        changed = False
        for name, index in configured.items():
            line = lines[index]
            suspended = line.startswith(SUSPENDED_PREFIX)
            if name in blocked and not suspended:
                lines[index] = SUSPENDED_PREFIX + line
                changed = True
            elif name not in blocked and suspended:
                lines[index] = line[len(SUSPENDED_PREFIX):]
                changed = True
        if changed:
            # Persist the retry flag before changing /etc, including interrupted reloads.
            data["reload_pending"] = True
        self.store.save(data)
        if changed:
            if self.users.snapshot() != raw:
                raise UserError("conflict", "Accounts changed during enforcement. The next sample will retry.", 409)
            atomic_private(self.users.path, "".join(lines).encode())
        if data["reload_pending"]:
            if snapshot["running"]:
                result = self.runner(["/usr/sbin/ipsec", "rereadsecrets"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, check=False, timeout=5)
                if result.returncode != 0:
                    raise UserError("reload_failed", "Limits were saved; credential reload needs another attempt.", 503)
                data["reload_pending"] = False
                self.store.save(data)
        errors = data["enforcement_error"]
        targets = [session["id"] for session in snapshot["sessions"] if session["username"] in blocked]
        for identifier in targets[:5]:
            if self.epoch() != data["epoch"]:
                data["reload_pending"] = True
                errors = True
                break
            if not re.fullmatch(r"ikev2-eap\[[0-9]+\]", identifier):
                raise UserError("invalid_session", "The session identifier is unavailable.", 503)
            try:
                result = self.runner(["/usr/sbin/ipsec", "down", identifier], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, check=False, timeout=3)
                errors = errors or result.returncode != 0
            except (OSError, subprocess.TimeoutExpired):
                errors = True
        data["enforcement_error"] = errors or len(targets) > 5
        self.store.save(data)

    def cycle(self, request=None):
        data = self.store.load()
        raw = self.users.snapshot()
        lines, configured = parse_users(raw)
        if any(lines[index].startswith(SUSPENDED_PREFIX) and name not in data["users"]
               for name, index in configured.items()):
            raise UserError("limits_missing", "A suspended account has no saved limits. Restore limits storage before continuing.", 503)
        if request:
            validate_limits(request)
            if hashlib.sha256(raw).hexdigest() != request.get("expected_digest") or data["revision"] != request["limits_revision"]:
                raise UserError("conflict", "Accounts or limits changed. Refresh users before trying again.", 409)
            if request["username"] not in configured:
                raise UserError("not_found", "This VPN user no longer exists.", 404)
        epoch = self.epoch()
        snapshot = self._snapshot()
        if self.epoch() != epoch or (snapshot["running"] and epoch.endswith(":stopped")):
            raise UserError("accounting_unavailable", "The VPN daemon changed during accounting. Refresh and retry.", 503)
        self.sample(data, configured, snapshot, epoch)
        if request:
            user = data["users"][request["username"]]
            if request["operation"] == "limits_set":
                if request["validity_seconds"] is None:
                    user["first_connected_at"] = None
                elif user["validity_seconds"] is None:
                    user["first_connected_at"] = self.clock() if request["username"] in data["online"] else None
                user.update(byte_limit=request["byte_limit"], validity_seconds=request["validity_seconds"])
            else:
                # Keep fresh per-CHILD baselines; existing sessions cannot re-add old usage.
                user.update(used_bytes=0, reset_at=self.clock(),
                            first_connected_at=self.clock() if user["validity_seconds"] is not None and request["username"] in data["online"] else None)
            data["revision"] = secrets.token_hex(16)
        self.enforce(data, raw, snapshot)
        return {"ok": True, "operation": request["operation"] if request else "sample",
                "username": request["username"] if request else None,
                "enforcement_pending": data["reload_pending"] or data["enforcement_error"]}


def restore_access(users=None, runner=subprocess.run):
    """Explicit rollback operation, after stopping the timer and worker."""
    users = users or UserStore()
    for unit in ("ikev2-web-limits.timer", "ikev2-web-limits.service"):
        result = runner(["/usr/bin/systemctl", "is-active", "--quiet", unit],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=3)
        if result.returncode != 3:
            raise UserError("limits_running", "Stop the limits timer and service before restoring access.", 409)
    active = runner(["/usr/bin/systemctl", "is-active", "--quiet", "strongswan-starter.service"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=3)
    if active.returncode not in (0, 3):
        raise UserError("accounting_unavailable", "VPN service state is unavailable.", 503)
    raw = users.snapshot()
    lines, configured = parse_users(raw)
    for index in configured.values():
        if lines[index].startswith(SUSPENDED_PREFIX):
            lines[index] = lines[index][len(SUSPENDED_PREFIX):]
    updated = "".join(lines).encode()
    if users.snapshot() != raw:
        raise UserError("conflict", "Account storage changed. Retry after reviewing the server.", 409)
    if updated != raw:
        atomic_private(users.path, updated)
    if active.returncode == 0:
        result = runner(["/usr/sbin/ipsec", "rereadsecrets"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=False, timeout=5)
        if result.returncode != 0:
            raise UserError("reload_failed", "Access was restored on disk; confirm the VPN credential reload.", 503)


def main():
    if os.geteuid() != 0:
        raise SystemExit("The limits service must run as root.")
    parser = argparse.ArgumentParser(description="Account for VPN usage and enforce user limits")
    parser.add_argument("--restore-access", action="store_true", help="remove quota suspensions after stopping the timer and service")
    args = parser.parse_args()
    if not UserStore().state.is_file():
        return
    try:
        with account_lock(blocking=args.restore_access):
            if args.restore_access:
                restore_access()
                print("Quota suspensions removed. Saved limit policies are unchanged.")
            else:
                LimitsController().cycle()
    except BlockingIOError:
        return  # An administrator change owns the lock. Sample again next timer tick.
    except UserError as error:
        raise SystemExit(f"{error.code}: {error}") from None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise SystemExit("VPN limit accounting or enforcement failed; inspect service state and refresh users.") from None


if __name__ == "__main__":
    main()
