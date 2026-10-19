"""Offline checks for Telegram authorization, delivery failures and local status probes."""

import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from telegram_bot.app import ApiError, Bot, BotConfig, NoRedirects, TelegramAPI
from telegram_bot.status import StatusCollector, format_status, read_snapshot, write_snapshot


TOKEN = "123456:" + "x" * 35  # Synthetic, never a real Telegram credential.


def message(update_id=1, user=42, chat_type="private", command="/status", date=1000):
    return {"update_id": update_id, "message": {"date": date, "text": command,
            "chat": {"id": user, "type": chat_type}, "from": {"id": user, "is_bot": False}}}


class ConfigTests(unittest.TestCase):
    def test_private_config_validates_ids_and_never_reprs_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            payload = {"bot_token": TOKEN, "allowed_user_ids": [42, 2**40]}
            path.write_text(json.dumps(payload))
            path.chmod(0o600)
            config = BotConfig.from_file(path)
            self.assertEqual(config.allowed_user_ids, {42, 2**40})
            self.assertNotIn(TOKEN, repr(config))
            for invalid in ([], [True], [-1], [0], ["42"], [2**52]):
                payload["allowed_user_ids"] = invalid
                path.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    BotConfig.from_file(path)
            payload["allowed_user_ids"] = [42]
            path.write_text(json.dumps(payload))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                BotConfig.from_file(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                BotConfig.from_file(link)


class ApiTests(unittest.TestCase):
    def api(self, data):
        opener = Mock()
        opener.open.return_value = io.BytesIO(data)
        return TelegramAPI(TOKEN, opener=opener), opener

    def test_fixed_https_json_endpoint_and_bounded_response(self):
        api, opener = self.api(b'{"ok":true,"result":[]}')
        self.assertEqual(api.call("getUpdates", {"timeout": 25}), [])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.telegram.org/bot{TOKEN}/getUpdates")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"timeout": 25})
        with self.assertRaises(ValueError):
            api.call("deleteWebhook")
        api, _ = self.api(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(ApiError):
            api.call("getMe")

    def test_http_rate_limit_and_network_errors_do_not_leak_token(self):
        api, opener = self.api(b"")
        opener.open.side_effect = HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 429,
                                            TOKEN, {}, io.BytesIO(json.dumps({"ok": False,
                                                "error_code": 429, "description": TOKEN,
                                                "parameters": {"retry_after": 37}}).encode()))
        with self.assertRaises(ApiError) as caught:
            api.call("sendMessage")
        self.assertEqual((caught.exception.code, caught.exception.retry_after), (429, 37))
        self.assertNotIn(TOKEN, str(caught.exception))
        opener.open.side_effect = URLError(TOKEN)
        with self.assertRaises(ApiError) as caught:
            api.call("getMe")
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_malformed_payloads_and_redirects_are_rejected(self):
        for raw in (b"null", b"[]", b"not json", b'{"ok":true}', b'{"ok":false,"description":"secret"}'):
            api, _ = self.api(raw)
            with self.assertRaises(ApiError):
                api.call("getMe")
        with self.assertRaises(ApiError):
            NoRedirects().redirect_request(None, None, 302, "", {}, "https://example.com/")


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = Mock()
        self.now = 100.0
        self.bot = Bot(BotConfig(TOKEN, frozenset({42})), self.api,
                       cursor=self.root / "cursor.json", snapshot=self.root / "status.json",
                       clock=lambda: 1000, monotonic=lambda: self.now)
        self.bot.username = "monitor_bot"

    def test_authorization_precedes_snapshot_access(self):
        invalid = [message(user=43), message(chat_type="group"), message(chat_type="supergroup"),
                   message(chat_type="channel"), message(date=100), message(date=2000)]
        spoof = message()
        spoof["message"]["chat"]["id"] = 43
        invalid.append(spoof)
        for update in ({"update_id": 1}, {"message": None}, {"message": {"from": []}}, *invalid):
            with patch("telegram_bot.app.read_snapshot") as read:
                self.bot.handle(update)
                read.assert_not_called()
        self.api.call.assert_not_called()

    def test_status_missing_snapshot_and_commands(self):
        self.bot.handle(message(command="/status@Monitor_Bot"))
        method, payload = self.api.call.call_args.args
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], 42)
        self.assertIn("unavailable or stale", payload["text"])
        self.assertNotIn("parse_mode", payload)
        self.assertTrue(payload["link_preview_options"]["is_disabled"])
        self.assertTrue(payload["protect_content"])
        for command in ("/status@another_bot", "   ", "hello", "/status;reboot"):
            self.now += 5
            self.api.reset_mock()
            self.bot.handle(message(command=command))
            self.api.call.assert_not_called()
        self.now += 5
        self.bot.handle(message(command="/start"))
        self.assertIn("/status", self.api.call.call_args.args[1]["text"])

    def test_per_user_rate_limit(self):
        self.bot.handle(message())
        self.bot.handle(message(update_id=2))
        self.assertEqual(self.api.call.call_count, 1)
        self.now += 5
        self.bot.handle(message(update_id=3))
        self.assertEqual(self.api.call.call_count, 2)

    def test_failed_send_is_checkpointed_and_not_replayed_after_restart(self):
        def call(method, payload):
            if method == "getUpdates":
                return [message(update_id=10), message(update_id=11, user=43)]
            self.assertEqual(json.loads(self.bot.cursor.read_text())["offset"], 11)
            raise ApiError(500)
        self.api.call.side_effect = call
        with self.assertRaises(ApiError):
            self.bot.poll_once()
        restarted = Bot(self.bot.config, self.api, self.bot.cursor, self.bot.snapshot,
                        clock=lambda: 1000, monotonic=lambda: self.now)
        self.assertEqual(restarted.offset, 11)
        self.api.call.side_effect = None
        self.api.call.return_value = [message(update_id=10), message(update_id=11, user=43)]
        restarted.poll_once()
        self.assertEqual(restarted.offset, 12)
        self.assertEqual(stat.S_IMODE(self.bot.cursor.stat().st_mode), 0o600)

    def test_failed_checkpoint_does_not_send_and_can_be_retried(self):
        self.api.call.return_value = [message()]
        with patch("telegram_bot.app.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.bot.poll_once()
        self.assertEqual(self.bot.offset, 0)
        self.assertEqual(self.api.call.call_count, 1)

    def test_existing_webhook_is_not_deleted(self):
        self.api.call.side_effect = [{"id": 123456, "username": "monitor_bot"}, {"url": "https://example.com/hook"}]
        with self.assertRaises(ValueError):
            self.bot.prepare()
        self.assertEqual([call.args[0] for call in self.api.call.call_args_list], ["getMe", "getWebhookInfo"])

    def test_poll_backoff_respects_retry_after_without_logging_token(self):
        stop = Mock(spec=threading.Event)
        stop.is_set.side_effect = [False, False, True]
        with patch.object(self.bot, "prepare", side_effect=[ApiError(429, 37), ApiError(503)]):
            with self.assertLogs("ikev2-telegram", level="WARNING") as logs:
                self.bot.run(stop)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [37, 2])
        self.assertNotIn(TOKEN, " ".join(logs.output))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "net").mkdir()
        (self.root / "state.env").write_text("OUT_IF=ens3\nINSTALLER_VERSION='6.4.9-en'\nPASSWORD=do-not-export\n")
        (self.root / "stat").write_text("cpu 1 0 1 8 0 0 0 0\ncpu0 1 0 1 8 0 0 0 0\n")
        (self.root / "meminfo").write_text("MemTotal: 2048 kB\nMemAvailable: 1024 kB\n")
        (self.root / "net/dev").write_text("ens3: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n")
        (self.root / "net/sockstat").write_text("TCP: inuse 10 orphan 0\nUDP: inuse 3 mem 0\n")
        (self.root / "net/sockstat6").write_text("TCP6: inuse 2\nUDP6: inuse 1\n")
        (self.root / "uptime").write_text("188820.0 10000\n")
        (self.root / "loadavg").write_text("0.10 0.20 0.30 1/20 100\n")
        self.commands = []

        def runner(argv, **kwargs):
            self.commands.append(argv)
            self.assertLessEqual(kwargs["timeout"], 5)
            if argv[0] == "/usr/bin/systemctl":
                return subprocess.CompletedProcess(argv, 0, "active\n", "")
            if argv[:2] == ["/usr/sbin/ipsec", "--version"]:
                return subprocess.CompletedProcess(argv, 0, "Linux strongSwan U5.9.5/K6.8.0", "")
            if argv[0] == "/usr/sbin/ip":
                entries = [{"addr_info": [{"local": "192.0.2.1", "scope": "global"},
                                          {"local": "2001:db8::1", "scope": "global"},
                                          {"local": "fe80::1", "scope": "link"}]}]
                return subprocess.CompletedProcess(argv, 0, json.dumps(entries), "")
            output = """Security Associations (2 up, 0 connecting):
ikev2-eap[1]: ESTABLISHED 1 minute ago, 192.0.2.1[server]...192.0.2.2[alice]
ikev2-eap{1}: INSTALLED, TUNNEL
ikev2-eap[2]: ESTABLISHED 1 minute ago, 192.0.2.1[server]...192.0.2.3[alice]
ikev2-eap{2}: INSTALLED, TUNNEL
"""
            return subprocess.CompletedProcess(argv, 0, output, "")
        self.collector = StatusCollector(self.root, self.root / "state.env", runner=runner, clock=lambda: 1000)

    def test_counts_versions_and_report_do_not_expose_accounts_or_secrets(self):
        data = self.collector.collect()
        self.assertEqual((data["online_sessions"], data["online_users"]), (2, 1))
        self.assertEqual((data["tcp"], data["udp"]), (12, 4))
        self.assertEqual(data["strongswan_version"], "5.9.5")
        self.assertEqual(data["installer_version"], "6.4.9-en")
        self.assertEqual(data["ipv6"], ["2001:db8::1"])
        self.assertEqual(data["memory"]["used_bytes"], 1048576)
        report = format_status(data, clock=lambda: 1005)
        self.assertIn("2d 4h 27m", report)
        self.assertIn("2 sessions / 1 users", report)
        self.assertIn("VPN Status: running", report)
        self.assertIn("↑ 1.95 KiB, ↓ 1000.00 B", report)
        self.assertNotIn("alice", json.dumps(data))
        self.assertNotIn("do-not-export", json.dumps(data))
        self.assertEqual(self.commands, [["/usr/bin/systemctl", "is-active", "strongswan-starter.service"],
                                        ["/usr/sbin/ipsec", "statusall", "ikev2-eap"],
                                        ["/usr/sbin/ipsec", "--version"],
                                        ["/usr/sbin/ip", "-j", "address", "show", "scope", "global"]])

    def test_unavailable_probes_never_become_zero_or_running(self):
        self.collector.runner = Mock(side_effect=OSError("unavailable"))
        for path in self.root.iterdir():
            if path.is_file():
                path.unlink()
        (self.root / "net/sockstat").unlink()
        data = self.collector.collect()
        self.assertIsNone(data["online_sessions"])
        self.assertEqual(data["vpn_status"], "unknown")
        self.assertIsNone(data["ipv4"])
        self.assertIsNone(data["tcp"])
        self.assertIsNone(data["memory"]["used_bytes"])
        self.assertIn("Online Clients: N/A", format_status(data, clock=lambda: 1000))

    def test_stopped_vpn_is_zero_and_has_no_statusall_call(self):
        self.collector.runner = Mock(return_value=subprocess.CompletedProcess([], 3, "inactive\n", ""))
        data = self.collector.collect()
        self.assertEqual(data["vpn_status"], "stopped")
        self.assertEqual(data["online_sessions"], 0)
        self.assertEqual(self.collector.runner.call_count, 3)

    def test_atomic_snapshot_age_permissions_and_failure_retains_old_data(self):
        path = self.root / "snapshot.json"
        data = self.collector.collect()
        write_snapshot(path, data)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertIsNotNone(read_snapshot(path, clock=lambda: 1090))
        self.assertIsNone(read_snapshot(path, clock=lambda: 1091))
        self.assertIsNone(read_snapshot(path, clock=lambda: 999))
        with patch("telegram_bot.status.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_snapshot(path, {"version": 2})
        self.assertEqual(json.loads(path.read_text()), data)
        self.assertEqual(list(self.root.glob(".status-*")), [])
        for invalid in ({"version": 1, "collected_at": float("nan")}, [], {"version": True, "collected_at": 1000}):
            path.write_text(json.dumps(invalid))
            self.assertIsNone(read_snapshot(path, clock=lambda: 1000))
        path.write_text("x" * 32769)
        self.assertIsNone(read_snapshot(path))

    def test_state_is_data_and_duplicate_versions_are_unknown(self):
        marker = self.root / "executed"
        (self.root / "state.env").write_text(f"OUT_IF=$(touch {marker})\nINSTALLER_VERSION='$(touch {marker})'\n")
        data = self.collector.collect()
        self.assertIsNone(data["installer_version"])
        self.assertIsNone(data["network"]["interface"])
        self.assertFalse(marker.exists())
        (self.root / "state.env").write_text("INSTALLER_VERSION=1\nINSTALLER_VERSION=2\n")
        self.assertIsNone(self.collector.collect()["installer_version"])

    def test_large_address_lists_still_fit_one_telegram_message(self):
        data = self.collector.collect()
        data["ipv6"] = [f"2001:db8:ffff:ffff:ffff:ffff:ffff:{value:04x}" for value in range(100)]
        data["ipv4"] = ["192.0.2.200"] * 100
        data["host"] = "<b>host</b>\nforged line"
        report = format_status(data, clock=lambda: 1000)
        self.assertLess(len(report.encode("utf-16-le")) // 2, 4096)
        self.assertIn("Host: <b>host</b>forged line", report)


if __name__ == "__main__":
    unittest.main()
