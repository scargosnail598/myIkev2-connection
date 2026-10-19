"""Telegram settings privacy, privileged application and authenticated HTTP controls."""

import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from webui.app import Ikev2WebServer
from webui.broker import AuditLogger, BrokerClient, BrokerServer, ServiceController
from webui.security import AuthConfig, AuthManager, hash_password
from webui.telegram import APPLY_SERVICE, BOT, TIMER, TelegramManager, TelegramStore, validate_settings
from webui.users import UserError, atomic_private

TOKEN = "123456:" + "x" * 35
NEW_TOKEN = "123456:" + "y" * 35
PASSWORD = "temporary-panel-fixture-password"


class Services:
    def __init__(self):
        self.enabled = {BOT: False, TIMER: False}
        self.active = {BOT: "inactive", TIMER: "inactive"}
        self.calls, self.fail_restart = [], False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.assert_fixed(argv)
        operation, units = argv[1], argv[2:]
        if operation == "is-enabled":
            value = "enabled" if self.enabled[units[0]] else "disabled"
            return subprocess.CompletedProcess(argv, 0 if value == "enabled" else 1, value + "\n")
        if operation == "is-active":
            value = self.active[units[0]]
            return subprocess.CompletedProcess(argv, 0 if value == "active" else 3, value + "\n")
        if operation == "restart" and self.fail_restart:
            self.fail_restart = False
            return subprocess.CompletedProcess(argv, 1)
        for unit in units:
            if unit == "--now":
                continue
            if unit in self.enabled and operation in {"enable", "disable"}:
                self.enabled[unit] = operation == "enable"
            if unit in self.active and (operation in {"start", "stop", "restart"} or "--now" in units):
                self.active[unit] = "inactive" if operation in {"disable", "stop"} else "active"
        return subprocess.CompletedProcess(argv, 0)

    def assert_fixed(self, argv):
        if argv[0] != "/usr/bin/systemctl" or any("strongswan" in value for value in argv):
            raise AssertionError("Only fixed Telegram service operations are allowed")


class TelegramSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.services = Services()
        self.store = TelegramStore(self.config, self.services, installed=True)
        self.manager = TelegramManager(self.store, self.root / "queue", runner=self.helper)
        self.helper_response = None

    def helper(self, argv, **kwargs):
        self.assertEqual(argv, ["/usr/bin/systemctl", "start", APPLY_SERVICE])
        path = self.manager.directory
        request = json.loads((path / "request.json").read_text())
        self.assertEqual((path / "request.json").stat().st_mode & 0o777, 0o600)
        try:
            result = self.store.apply(request)
        except UserError as error:
            result = error.result()
        atomic_private(path / "result.json", json.dumps({**(self.helper_response or result),
                                                        "request_id": request["request_id"]}).encode())
        return subprocess.CompletedProcess(argv, 0)

    def save(self, **changes):
        payload = {"enabled": True, "allowed_user_ids": [42], "revision": self.manager.settings()["revision"], **changes}
        return self.manager.save(payload)

    def test_first_setup_preserve_token_disable_and_never_return_secret(self):
        self.assertFalse(self.manager.settings()["configured"])
        self.assertEqual(self.save(bot_token=TOKEN), {"ok": True})
        settings = self.manager.settings()
        self.assertTrue(settings["configured"])
        self.assertTrue(settings["enabled"])
        self.assertNotIn(TOKEN, json.dumps(settings))
        self.assertNotIn(hashlib.sha256(self.config.read_bytes()).hexdigest(), settings["revision"])
        self.save(enabled=False, allowed_user_ids=[99])
        self.assertEqual(json.loads(self.config.read_text()), {"bot_token": TOKEN, "allowed_user_ids": [99]})
        self.assertFalse(self.manager.settings()["enabled"])
        self.assertEqual(list(self.manager.directory.iterdir()), [])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_incomplete_initial_setup_does_not_enable_services(self):
        with self.assertRaises(UserError) as caught:
            self.save()
        self.assertEqual(caught.exception.status, 400)
        self.assertFalse(self.config.exists())
        self.assertFalse(any(self.services.enabled.values()))

    def test_conflicting_browser_revision_does_not_overwrite(self):
        self.save(bot_token=TOKEN)
        old_revision = self.manager.settings()["revision"]
        self.save(bot_token=NEW_TOKEN)
        with self.assertRaises(UserError) as caught:
            self.manager.save({"enabled": False, "allowed_user_ids": [42], "revision": old_revision})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(json.loads(self.config.read_text())["bot_token"], NEW_TOKEN)

    def test_helper_rechecks_config_before_apply(self):
        self.save(bot_token=TOKEN)
        original, _, enabled = self.store.snapshot()
        request = {"enabled": False, "allowed_user_ids": [99], "expected_digest": hashlib.sha256(original).hexdigest(),
                   "expected_enabled": enabled, "request_id": "a" * 32}
        atomic_private(self.config, json.dumps({"bot_token": NEW_TOKEN, "allowed_user_ids": [42]}).encode())
        with self.assertRaises(UserError) as caught:
            self.store.apply(request)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(json.loads(self.config.read_text())["bot_token"], NEW_TOKEN)

    def test_service_failure_restores_previous_token_and_enablement(self):
        self.save(bot_token=TOKEN)
        original = self.config.read_bytes()
        self.services.fail_restart = True
        with self.assertRaises(UserError):
            self.save(bot_token=NEW_TOKEN)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertTrue(all(self.services.enabled.values()))
        self.assertEqual(self.services.active[BOT], "active")
        self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_invalid_fields_ids_tokens_and_unsafe_config_are_rejected(self):
        base = {"enabled": True, "allowed_user_ids": [42]}
        for change in ({"enabled": 1}, {"allowed_user_ids": [True]}, {"allowed_user_ids": [-1]},
                       {"bot_token": ""}, {"command": "restart"}, {"path": "/etc/shadow"}):
            with self.assertRaises(UserError):
                validate_settings({**base, **change})
        self.save(bot_token=TOKEN)
        self.config.chmod(0o644)
        with self.assertRaises(UserError):
            self.manager.settings()

    def test_arbitrary_helper_fields_are_not_returned(self):
        self.helper_response = {"ok": True, "bot_token": TOKEN, "debug": TOKEN}
        self.assertEqual(self.save(bot_token=TOKEN), {"ok": True})

    def test_missing_units_and_unknown_enablement_disable_mutations(self):
        self.store._installed = False
        self.assertFalse(self.manager.settings()["installed"])
        with self.assertRaises(UserError):
            self.save(bot_token=TOKEN)
        self.store._installed = True
        self.store.runner = Mock(side_effect=OSError("systemd unavailable"))
        self.assertFalse(self.manager.settings()["state_known"])
        with self.assertRaises(UserError):
            self.save(bot_token=TOKEN)

    def test_broker_socket_audits_outcomes_without_token_or_user_ids(self):
        path = self.root / "broker"
        path.mkdir(mode=0o700)
        audit = self.root / "audit.jsonl"
        server = BrokerServer(path / "socket", os.geteuid(), os.getegid(), ServiceController(),
                              AuditLogger(audit), telegram=self.manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = BrokerClient(path / "socket")
        initial = client.telegram_settings()
        result = client.save_telegram({"enabled": True, "allowed_user_ids": [42], "bot_token": TOKEN,
                                       "revision": initial["revision"]})
        self.assertEqual(result, {"ok": True})
        self.assertNotIn(TOKEN, json.dumps(client.telegram_settings()))
        events = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertEqual([event["stage"] for event in events], ["requested", "completed"])
        self.assertNotIn(TOKEN, audit.read_text())
        self.assertNotIn("allowed_user_ids", audit.read_text())


class TelegramHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(PASSWORD)

    def setUp(self):
        self.broker = Mock()
        self.broker.telegram_settings.return_value = {"ok": True, "installed": True, "configured": True,
            "enabled": False, "allowed_user_ids": [42], "revision": "r", "bot_token": TOKEN}
        self.broker.save_telegram.return_value = {"ok": True, "bot_token": TOKEN}
        auth = AuthManager(AuthConfig("admin", self.password_hash, secure_cookie=False))
        self.server = Ikev2WebServer(("127.0.0.1", 0), provider=Mock(), auth_manager=auth, broker_client=self.broker)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf = None

    def request(self, path, payload=None, csrf=True):
        headers = {"Content-Type": "application/json"}
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.client.open(Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                        headers=headers), timeout=5)

    def login(self):
        with self.request("/api/auth/login", {"username": "admin", "password": PASSWORD}) as response:
            self.csrf = json.load(response)["csrf_token"]

    def test_auth_csrf_cache_control_and_secret_redaction(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/telegram")
        self.assertEqual(caught.exception.code, 401)
        self.login()
        with self.request("/api/telegram") as response:
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertNotIn(TOKEN, response.read().decode())
        payload = {"enabled": True, "allowed_user_ids": [42], "bot_token": TOKEN, "revision": "r"}
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/telegram", payload, csrf=False)
        self.assertEqual(caught.exception.code, 403)
        self.broker.save_telegram.assert_not_called()
        with self.request("/api/telegram", payload) as response:
            self.assertEqual(json.load(response), {"ok": True})
        self.broker.save_telegram.assert_called_once_with(payload)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/telegram", {**payload, "path": "/etc/shadow"})
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
