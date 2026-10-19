"""Read-only report sources, traffic semantics, pagination and private command access."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from telegram_bot.activity import MAX_RECORDS, saved_usage, session_activity
from telegram_bot.app import ApiError, Bot, BotConfig
from telegram_bot.reports import PAGE_SIZE, render
from telegram_bot.status import write_snapshot


def session(name="alice", number=1, rx=1024, tx=2048):
    return {"username": name, "id": f"ikev2-eap[{number}]", "duration": "2 minutes ago",
            "vpn_addresses": ["10.10.10.1", "fd00::1"], "public_ip": "198.51.100.10",
            "rx_bytes": rx, "tx_bytes": tx}


def data():
    activity = session_activity({"ok": True, "sessions": [session(), session(number=2, rx=3072, tx=4096), session("bob", 3)]})
    return {"version": 1, "collected_at": 1000, **activity,
            "usage": [{"username": "alice", "rx_bytes": 10240, "tx_bytes": 20480},
                      {"username": "offline", "rx_bytes": 1024, "tx_bytes": 2048}],
            "usage_total": 2, "usage_saved_at": 950}


class ActivityTests(unittest.TestCase):
    def test_distinct_users_sum_multiple_sessions_but_unknown_counters_stay_unknown(self):
        result = session_activity({"ok": True, "sessions": [session(), session(number=2, tx=None), session("bob", 3)]})
        self.assertEqual((result["sessions_total"], result["online_total"]), (3, 2))
        self.assertEqual(result["online"][0], {"username": "alice", "sessions": 2, "rx_bytes": 2048, "tx_bytes": None})
        self.assertNotIn("public_ip", json.dumps(result))
        self.assertNotIn("198.51.100.10", json.dumps(result))
        self.assertIsNone(session_activity({"ok": False})["online"])
        self.assertEqual(session_activity({"ok": True, "sessions": []})["online"], [])

    def test_session_storage_is_bounded_with_truthful_total(self):
        result = session_activity({"ok": True, "sessions": [session(number=index) for index in range(MAX_RECORDS + 5)]})
        self.assertEqual(len(result["sessions"]), MAX_RECORDS)
        self.assertEqual(result["sessions_total"], MAX_RECORDS + 5)
        self.assertEqual(result["online"][0]["sessions"], MAX_RECORDS + 5)

    def test_cumulative_totals_ignore_session_baselines_and_keep_saved_age(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "traffic.tsv"
            path.write_text("TOTAL\talice\t1024\t2048\nSESSION\tikev2-eap[1]\t9999\t9999\nTOTAL\tbob\t10000\t20000\n")
            path.chmod(0o600)
            original = path.read_bytes()
            result = saved_usage(path)
            self.assertEqual(result["usage"][0]["username"], "bob")
            self.assertEqual(result["usage"][1]["rx_bytes"], 1024)
            self.assertEqual(result["usage_saved_at"], path.stat().st_mtime)
            self.assertEqual(result["usage_total"], 2)
            self.assertNotIn("ikev2-eap", json.dumps(result))
            self.assertEqual(path.read_bytes(), original)

    def test_missing_unsafe_and_malformed_usage_is_unavailable_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "traffic.tsv"
            self.assertIsNone(saved_usage(path)["usage"])
            for content in ("TOTAL\talice\t-1\t2\n", "TOTAL\talice\t1\t2\nTOTAL\talice\t3\t4\n",
                            "TOTAL\t../../etc/shadow\t1\t2\n", "TOTAL\talice\t18446744073709551616\t2\n"):
                path.write_text(content)
                path.chmod(0o600)
                self.assertIsNone(saved_usage(path)["usage"])
            path.write_text("TOTAL\talice\t1\t2\n")
            path.chmod(0o644)
            self.assertIsNone(saved_usage(path)["usage"])
            path.chmod(0o600)
            alias = Path(directory) / "alias"
            alias.symlink_to(path)
            self.assertIsNone(saved_usage(alias)["usage"])
            path.write_text("")
            self.assertEqual(saved_usage(path)["usage"], [])


class ReportTests(unittest.TestCase):
    def report(self, command, args=None, source=None):
        return render(command, args or [], lambda: source if source is not None else data(), clock=lambda: 1005)

    def test_sessions_online_and_cumulative_usage_have_distinct_meanings(self):
        sessions = self.report("sessions")
        self.assertIn("3 total", sessions)
        self.assertIn("ikev2-eap[2]", sessions)
        self.assertIn("10.10.10.1", sessions)
        self.assertIn("2 minutes ago", sessions)
        self.assertIn("reset on rekey/reconnect", sessions)
        online = self.report("online")
        self.assertIn("2 total", online)
        self.assertIn("alice · 2 session(s)", online)
        self.assertIn("10.00 KiB (↑ 6.00 KiB, ↓ 4.00 KiB)", online)
        usage = self.report("usage")
        self.assertIn("30.00 KiB (↑ 20.00 KiB, ↓ 10.00 KiB)", usage)
        self.assertIn("Saved 55s ago", usage)
        self.assertIn("Separate from panel quota", usage)
        self.assertEqual(online, self.report("users"))
        self.assertEqual(usage, self.report("traffic"))

    def test_explicit_paging_and_message_size_for_long_session_details(self):
        snapshot = data()
        row = session("x" * 64)
        row.update(id="a" * 64, duration="b" * 64,
                   vpn_addresses=["2001:db8:ffff:ffff:ffff:ffff:ffff:ffff"] * 4)
        snapshot.update(session_activity({"ok": True, "sessions": [copy.deepcopy(row) for _ in range(PAGE_SIZE + 1)]}))
        report = self.report("sessions", source=snapshot)
        self.assertIn("Page 1/2", report)
        self.assertIn("Next: /sessions 2", report)
        self.assertLessEqual(len(report.encode("utf-16-le")) // 2, 4096)
        self.assertIn("Page 2/2", self.report("sessions", ["2"], snapshot))
        self.assertIn("unavailable", self.report("sessions", ["3"], snapshot))

    def test_invalid_arguments_and_help_do_not_read_snapshot(self):
        read = Mock(side_effect=AssertionError("Must not read private snapshot"))
        for command, args in (("user", ["../../etc/shadow"]), ("sessions", ["0"]), ("usage", ["-1"]),
                              ("online", ["1", "2"]), ("status", ["extra"]), ("user", [])):
            self.assertIn("Usage:", render(command, args, read))
        for command in ("start", "help", "restart"):
            self.assertIn("/sessions", render(command, [], read))
        read.assert_not_called()

    def test_stale_missing_old_and_empty_snapshots_are_explicit(self):
        self.assertIn("stale", render("online", [], lambda: None))
        old = {"collected_at": 1000}
        self.assertIn("Unavailable", self.report("sessions", source=old))
        empty = {"collected_at": 1000, **session_activity({"ok": True, "sessions": []})}
        self.assertIn("No active", self.report("online", source=empty))
        unavailable = {"collected_at": 1000, **session_activity({"ok": False})}
        self.assertIn("Unavailable", self.report("online", source=unavailable))

    def test_user_summary_distinguishes_online_offline_and_omitted_users(self):
        self.assertIn("State: Online", self.report("user", ["alice"]))
        offline = self.report("user", ["offline"])
        self.assertIn("State: Offline", offline)
        self.assertIn("Saved cumulative: 3.00 KiB", offline)
        truncated = data()
        truncated["online_total"] = 1001
        self.assertIn("State: Unknown", self.report("user", ["missing"], truncated))
        self.assertIn("N/A", self.report("user", ["missing"]))

    def test_private_authorization_and_dispatch_for_new_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "status.json"
            write_snapshot(snapshot, data())
            api = Mock()
            bot = Bot(BotConfig("123456:" + "x" * 35, frozenset({42})), api,
                      cursor=Path(directory) / "cursor.json", snapshot=snapshot,
                      clock=lambda: 1005, monotonic=lambda: 100)
            update = {"message": {"date": 1005, "text": "/online", "chat": {"id": 43, "type": "private"},
                                  "from": {"id": 43, "is_bot": False}}}
            bot.handle(update)
            api.call.assert_not_called()
            update["message"].update(chat={"id": 42, "type": "group"}, **{"from": {"id": 42, "is_bot": False}})
            bot.handle(update)
            api.call.assert_not_called()
            update["message"]["chat"]["type"] = "private"
            for command in ("/online", "/sessions", "/usage", "/user alice"):
                bot.last_reply.clear()
                update["message"]["text"] = command
                bot.handle(update)
                method, payload = api.call.call_args.args
                self.assertEqual(method, "sendMessage")
                self.assertEqual(payload["chat_id"], 42)
                self.assertIn("alice", payload["text"])
                self.assertNotIn("198.51.100.10", payload["text"])


    def test_menu_uses_private_scope_and_failure_does_not_block_typed_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Mock()
            bot = Bot(BotConfig("123456:" + "x" * 35, frozenset({42})), api,
                      cursor=Path(directory) / "cursor.json")
            api.call.side_effect = [{"id": 123456, "username": "monitor_bot"}, {"url": ""}, True]
            bot.prepare()
            method, payload = api.call.call_args.args
            self.assertEqual(method, "setMyCommands")
            self.assertEqual(payload["scope"], {"type": "all_private_chats"})
            self.assertIn("sessions", [command["command"] for command in payload["commands"]])
            self.assertNotIn("42", json.dumps(payload))
            api.call.side_effect = [{"id": 123456, "username": "monitor_bot"}, {"url": ""}, ApiError(503)]
            with self.assertLogs("ikev2-telegram", level="WARNING"):
                bot.prepare()
            self.assertEqual(bot.username, "monitor_bot")


if __name__ == "__main__":
    unittest.main()
