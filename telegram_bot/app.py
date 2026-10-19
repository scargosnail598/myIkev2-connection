"""Long-poll Telegram without third-party dependencies or privileged commands."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import fcntl
import getpass
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import ssl
import stat
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from telegram_bot.status import SNAPSHOT, format_status, read_snapshot


CONFIG = Path("/etc/ikev2-telegram/config.json")
CURSOR = Path("/var/lib/ikev2-telegram/cursor.json")
TOKEN = re.compile(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,128}")
HELP = "IKEv2 server monitor\n/status — server health, resources and active connection counts\n/help — show commands"
MAX_API_BYTES = 1024 * 1024
LOG = logging.getLogger("ikev2-telegram")


@dataclass(frozen=True)
class BotConfig:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]

    @classmethod
    def from_file(cls, path):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027 or info.st_size > 4096:
                raise ValueError("Bot config must be a private regular file (0600 or 0640).")
            data = json.load(source)
        if not isinstance(data, dict) or set(data) != {"bot_token", "allowed_user_ids"}:
            raise ValueError("Bot config requires bot_token and allowed_user_ids only.")
        token, ids = data["bot_token"], data["allowed_user_ids"]
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError("Invalid Telegram bot token.")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(type(value) is not int or not 0 < value < 2**52 for value in ids)):
            raise ValueError("Provide 1–100 positive numeric Telegram user IDs.")
        return cls(token, frozenset(ids))


class ApiError(Exception):
    def __init__(self, code=0, retry_after=0):
        self.code = code if type(code) is int else 0
        self.retry_after = retry_after if type(retry_after) is int and 0 <= retry_after <= 86400 else 0
        # Never retain a URL, response body or remote description: these may contain the token.
        super().__init__(f"Telegram request failed (code {self.code}).")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError(code)


class TelegramAPI:
    def __init__(self, token, opener=None):
        self._token = token
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirects(),
                                            HTTPSHandler(context=ssl.create_default_context()))

    def call(self, method, payload=None):
        if method not in {"getMe", "getWebhookInfo", "getUpdates", "sendMessage"}:
            raise ValueError("Unsupported Telegram method.")
        request = Request(f"https://api.telegram.org/bot{self._token}/{method}",
                          data=json.dumps(payload or {}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        http_code = 0
        try:
            try:
                response = self.opener.open(request, timeout=40)
            except HTTPError as error:
                http_code, response = error.code, error
            with response:
                raw = response.read(MAX_API_BYTES + 1)
            if len(raw) > MAX_API_BYTES:
                raise ApiError(http_code)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ApiError(http_code)
            if http_code or data.get("ok") is not True:
                parameters = data.get("parameters")
                delay = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
                raise ApiError(data.get("error_code", http_code), delay)
            if "result" not in data:
                raise ApiError()
            return data["result"]
        except (OSError, URLError, HTTPException, ValueError, UnicodeError):
            raise ApiError(http_code) from None


def write_json(path, payload):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(payload, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Bot:
    def __init__(self, config, api, cursor=CURSOR, snapshot=SNAPSHOT,
                 clock=time.time, monotonic=time.monotonic):
        self.config, self.api, self.cursor, self.snapshot = config, api, cursor, snapshot
        self.clock, self.monotonic = clock, monotonic
        self.bot_id = int(config.token.split(":", 1)[0])
        self.username = ""
        self.last_reply = {}
        self.offset = 0
        try:
            data = json.loads(cursor.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("Invalid Telegram cursor; stop the bot before removing the cursor file.")
        if data.get("bot_id") == self.bot_id:
            if type(data.get("offset")) is not int or data["offset"] < 0:
                raise ValueError("Invalid Telegram cursor offset.")
            self.offset = data["offset"]

    def prepare(self):
        me = self.api.call("getMe")
        webhook = self.api.call("getWebhookInfo")
        if (not isinstance(me, dict) or me.get("id") != self.bot_id or not isinstance(me.get("username"), str)
                or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", me["username"])):
            raise ApiError()
        if not isinstance(webhook, dict) or webhook.get("url") != "":
            raise ValueError("This bot has a webhook. Use a dedicated bot with no webhook for this monitor.")
        self.username = me["username"].casefold()

    def handle(self, update):
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat, sender = message.get("chat"), message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        user_id = sender.get("id")
        if (type(user_id) is not int or user_id not in self.config.allowed_user_ids
                or sender.get("is_bot") is not False or chat.get("type") != "private"
                or type(chat.get("id")) is not int or chat["id"] != user_id):
            return
        date = message.get("date")
        if type(date) is not int or not -30 <= self.clock() - date <= 300:
            return
        content = message.get("text")
        if not isinstance(content, str) or not content.strip() or len(content) > 4096:
            return
        command = re.fullmatch(r"/([a-z]+)(?:@([A-Za-z0-9_]+))?", content.split()[0])
        if command is None or (command[2] and command[2].casefold() != self.username):
            return
        now = self.monotonic()
        if now - self.last_reply.get(user_id, float("-inf")) < 5:
            return
        self.last_reply[user_id] = now
        reply = format_status(read_snapshot(self.snapshot, self.clock), self.clock) if command[1] == "status" else HELP
        self.api.call("sendMessage", {"chat_id": user_id, "text": reply,
                                      "link_preview_options": {"is_disabled": True}, "protect_content": True})

    def poll_once(self):
        updates = self.api.call("getUpdates", {"offset": self.offset, "timeout": 25,
                                               "limit": 20, "allowed_updates": ["message"]})
        if (not isinstance(updates, list) or any(not isinstance(item, dict)
                or type(item.get("update_id")) is not int or item["update_id"] < 0 for item in updates)):
            raise ApiError()
        for update in sorted(updates, key=lambda item: item["update_id"]):
            if update["update_id"] < self.offset:
                continue
            # Persist before sending: do not replay replies after a crash or ambiguous send failure.
            next_offset = update["update_id"] + 1
            write_json(self.cursor, {"bot_id": self.bot_id, "offset": next_offset})
            self.offset = next_offset
            self.handle(update)

    def run(self, stop):
        delay = 1
        prepared = False
        while not stop.is_set():
            try:
                if not prepared:
                    self.prepare()
                    prepared = True
                self.poll_once()
                delay = 1
            except ApiError as error:
                LOG.warning("Telegram unavailable (code %s); retrying.", error.code)
                stop.wait(max(delay, error.retry_after))
                delay = min(delay * 2, 60)


def configure(path):
    if not sys.stdin.isatty():
        raise ValueError("An interactive terminal is required for hidden token entry.")
    if path.is_symlink():
        raise ValueError("Config must not be a symlink.")
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    raw = input("Allowed Telegram user IDs (comma-separated numeric IDs): ").strip()
    if not re.fullmatch(r"[0-9]+(?:\s*,\s*[0-9]+)*", raw):
        raise ValueError("Expected comma-separated numeric user IDs.")
    ids = [int(value.strip()) for value in raw.split(",")]
    if not TOKEN.fullmatch(token) or not 1 <= len(ids) <= 100 or any(not 0 < value < 2**52 for value in ids):
        raise ValueError("Invalid token or allowed user IDs.")
    descriptor = os.open(path.parent / ".config.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        write_json(path, {"bot_token": token, "allowed_user_ids": ids})
    finally:
        os.close(descriptor)
    print("Private bot config saved. No token was printed or sent.")


def identify():
    if not sys.stdin.isatty():
        raise ValueError("An interactive terminal is required for hidden token entry.")
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    if not TOKEN.fullmatch(token):
        raise ValueError("Invalid Telegram token.")
    api = TelegramAPI(token)
    me = api.call("getMe")
    challenge = "/start " + secrets.token_hex(8)
    print(f"Send {challenge} in a PRIVATE chat to @{me['username']}, then press Enter here.")
    input()
    updates = api.call("getUpdates", {"timeout": 25, "limit": 100, "allowed_updates": ["message"]})
    ids = set()
    for update in updates:
        message = update.get("message", {})
        chat, sender = message.get("chat", {}), message.get("from", {})
        if (message.get("text") == challenge and chat.get("type") == "private" and type(sender.get("id")) is int
                and chat.get("id") == sender["id"] and sender.get("is_bot") is False):
            ids.add(sender["id"])
    print("Private sender IDs: " + (", ".join(map(str, sorted(ids))) or "none; send /start and retry"))
    print("Only allow your own verified ID(s); discovery does not authorize anyone or send replies.")


def main():
    parser = argparse.ArgumentParser(description="Private IKEv2 Telegram status bot")
    parser.add_argument("command", choices=("run", "configure", "identify"))
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "configure":
            configure(args.config)
        elif args.command == "identify":
            identify()
        else:
            stop = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
            config = BotConfig.from_file(args.config)
            Bot(config, TelegramAPI(config.token)).run(stop)
    except (OSError, ValueError, ApiError):
        # Exception details from HTTP clients, JSON input or OS errors can contain secrets.
        parser.exit(1, "Bot failed: check private config, cursor permissions, Telegram access and webhook/other poller conflicts.\n")


if __name__ == "__main__":
    main()
