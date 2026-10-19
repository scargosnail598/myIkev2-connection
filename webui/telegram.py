"""Private Telegram settings and the fixed-path root apply helper."""

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import threading

from telegram_bot.app import BotConfig, TOKEN
from webui.users import UserError, atomic_private, read_private

CONFIG = Path("/etc/ikev2-telegram/config.json")
QUEUE = Path("/run/ikev2-web/telegram")
APPLY_SERVICE = "ikev2-web-telegram-apply.service"
BOT = "ikev2-telegram.service"
TIMER = "ikev2-telegram-collect.timer"
COLLECTOR = "ikev2-telegram-collect.service"


def validate_settings(request, internal=False):
    allowed = {"enabled", "allowed_user_ids", "bot_token"}
    allowed |= {"expected_digest", "expected_enabled", "request_id"} if internal else {"version", "action", "revision"}
    if set(request) - allowed or type(request.get("enabled")) is not bool:
        raise UserError("invalid_settings", "Supply only the Telegram settings fields.")
    ids = request.get("allowed_user_ids")
    if (not isinstance(ids, list) or len(ids) > 100
            or any(type(value) is not int or not 0 < value < 2**52 for value in ids)):
        raise UserError("invalid_ids", "Use at most 100 positive numeric Telegram user IDs.")
    if "bot_token" in request and (not isinstance(request["bot_token"], str) or not TOKEN.fullmatch(request["bot_token"])):
        raise UserError("invalid_token", "Enter a valid bot token, or leave it blank to preserve the saved token.")


class TelegramStore:
    def __init__(self, config=CONFIG, runner=subprocess.run, installed=None):
        self.config, self.runner, self._installed = config, runner, installed

    def installed(self):
        if self._installed is not None:
            return self._installed
        return (Path("/opt/ikev2-telegram/telegram_bot/app.py").is_file()
                and all(Path("/etc/systemd/system/" + unit).is_file()
                        for unit in (APPLY_SERVICE, BOT, TIMER, COLLECTOR)))

    def raw(self):
        try:
            raw = read_private(self.config, 4096)
        except FileNotFoundError:
            return b""
        BotConfig.from_file(self.config)
        return raw

    def states(self):
        result = {}
        for key, unit in (("bot", BOT), ("collector", TIMER)):
            for field, verb in (("enabled", "is-enabled"), ("state", "is-active")):
                try:
                    response = self.runner(["/usr/bin/systemctl", verb, unit], capture_output=True,
                                           text=True, check=False, timeout=3)
                    value = response.stdout.strip()
                except (OSError, subprocess.TimeoutExpired):
                    value = "unknown"
                if field == "enabled":
                    result[key + "_enabled"] = {"enabled": True, "disabled": False}.get(value)
                else:
                    result[key + "_state"] = value if value in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"
        return result

    def snapshot(self):
        if not self.installed():
            return b"", {"ok": True, "installed": False, "configured": False, "enabled": False,
                          "allowed_user_ids": [], "bot_state": "unknown", "collector_state": "unknown"}, {}
        raw = self.raw()
        config = json.loads(raw) if raw else {}
        states = self.states()
        enabled = {key: states[key + "_enabled"] for key in ("bot", "collector")}
        return raw, {"ok": True, "installed": True, "configured": bool(raw),
                     "enabled": all(value is True for value in enabled.values()),
                     "allowed_user_ids": config.get("allowed_user_ids", []),
                     "bot_state": states["bot_state"], "collector_state": states["collector_state"],
                     "state_known": all(type(value) is bool for value in enabled.values())}, enabled

    def command(self, *arguments):
        response = self.runner(["/usr/bin/systemctl", *arguments], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False, timeout=50)
        if response.returncode != 0:
            raise OSError("Telegram service operation failed")

    def activate(self, enabled):
        if enabled:
            self.command("start", COLLECTOR)
            self.command("enable", "--now", TIMER)
            self.command("enable", BOT)
            self.command("restart", BOT)
        else:
            self.command("disable", "--now", BOT, TIMER)
            self.command("stop", COLLECTOR)

    @contextmanager
    def exclusive(self):
        info = self.config.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise UserError("unsafe_config", "Private Telegram configuration directory is unavailable.", 503)
        descriptor = os.open(self.config.parent / ".config.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def apply(self, request):
        validate_settings(request, internal=True)
        with self.exclusive():
            original, public, enabled = self.snapshot()
            if not public["installed"] or not public.get("state_known"):
                raise UserError("not_installed", "Telegram services are unavailable. Update the Web UI installation.", 503)
            if (hashlib.sha256(original).hexdigest() != request.get("expected_digest")
                    or enabled != request.get("expected_enabled")):
                raise UserError("conflict", "Telegram settings changed. Refresh before saving.", 409)
            config = json.loads(original) if original else {}
            token = request.get("bot_token", config.get("bot_token"))
            ids = sorted(set(request["allowed_user_ids"]))
            if request["enabled"] or token or ids:
                if not token or not ids:
                    raise UserError("incomplete_settings", "Provide a bot token and at least one allowed Telegram user ID.")
                updated = json.dumps({"bot_token": token, "allowed_user_ids": ids}).encode()
            else:
                updated = b""
            try:
                if updated:
                    atomic_private(self.config, updated)
                self.activate(request["enabled"])
            except (OSError, subprocess.TimeoutExpired):
                # Restore private storage even if service control itself is unavailable.
                try:
                    if original:
                        atomic_private(self.config, original)
                    else:
                        self.config.unlink(missing_ok=True)
                    self.activate(False)
                    for key, unit in (("bot", BOT), ("collector", TIMER)):
                        self.command("enable" if enabled[key] else "disable", unit)
                    if public["collector_state"] == "active":
                        self.command("start", TIMER)
                    if public["bot_state"] == "active":
                        self.command("restart", BOT)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise UserError("apply_failed", "Telegram update failed. Check settings and runtime state before retrying.", 503) from None
            return {"ok": True}


class TelegramManager:
    def __init__(self, store=None, directory=QUEUE, runner=subprocess.run):
        self.store, self.directory, self.runner = store or TelegramStore(), directory, runner
        self._key, self._lock = secrets.token_bytes(32), threading.Lock()

    def _revision(self, raw, enabled):
        return hmac.new(self._key, raw + json.dumps(enabled, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def settings(self):
        raw, public, enabled = self.store.snapshot()
        return {**public, "revision": self._revision(raw, enabled)}

    def save(self, request):
        validate_settings(request)
        if not self._lock.acquire(blocking=False):
            raise UserError("busy", "Another Telegram update is running. Refresh before retrying.", 409)
        try:
            raw, public, enabled = self.store.snapshot()
            if not public["installed"] or not public.get("state_known"):
                raise UserError("not_installed", "Telegram services are unavailable. Update the Web UI installation.", 503)
            revision = request.get("revision")
            if (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision)
                    or not hmac.compare_digest(revision, self._revision(raw, enabled))):
                raise UserError("conflict", "Telegram settings changed. Refresh before saving.", 409)
            self.directory.mkdir(mode=0o700, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise UserError("unsafe_queue", "Private Telegram request storage is unavailable.", 503)
            payload = {key: request[key] for key in ("enabled", "allowed_user_ids", "bot_token") if key in request}
            payload.update(expected_digest=hashlib.sha256(raw).hexdigest(), expected_enabled=enabled, request_id=secrets.token_hex(16))
            request_path, result_path = self.directory / "request.json", self.directory / "result.json"
            result_path.unlink(missing_ok=True)
            try:
                atomic_private(request_path, json.dumps(payload).encode())
                result = self.runner(["/usr/bin/systemctl", "start", APPLY_SERVICE], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=110, check=False)
                if result.returncode != 0:
                    raise OSError("Telegram helper failed")
                response = json.loads(read_private(result_path, 4096))
                if not isinstance(response, dict) or response.get("request_id") != payload["request_id"]:
                    raise ValueError("Unmatched helper response")
                if response.get("ok") is True:
                    return {"ok": True}
                if response.get("status") == 409:
                    raise UserError("conflict", "Telegram settings changed. Refresh before saving.", 409)
                if response.get("status") == 400:
                    raise UserError("invalid_settings", "Provide a valid token and at least one allowed Telegram user ID.")
                raise UserError("apply_failed", "Telegram change was not confirmed. Refresh settings and check services before retrying.", 503)
            finally:
                request_path.unlink(missing_ok=True)
                result_path.unlink(missing_ok=True)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise UserError("apply_failed", "Telegram change was not confirmed. Refresh before retrying.", 503) from None
        finally:
            self._lock.release()


def apply_queued_request():
    if os.geteuid() != 0:
        raise SystemExit("Telegram settings helper must run as root.")
    info = QUEUE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit("Private Telegram queue is unavailable.")
    request = None
    try:
        request = json.loads(read_private(QUEUE / "request.json", 4096))
        if not isinstance(request, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(request.get("request_id", ""))):
            raise ValueError("Invalid request")
        result = TelegramStore().apply(request)
    except UserError as error:
        result = error.result()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result = {"ok": False, "status": 503}
    if isinstance(request, dict) and re.fullmatch(r"[0-9a-f]{32}", str(request.get("request_id", ""))):
        atomic_private(QUEUE / "result.json", json.dumps({**result, "request_id": request["request_id"]}).encode())


if __name__ == "__main__":
    apply_queued_request()
