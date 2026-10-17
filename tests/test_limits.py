import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from webui.limits import (LimitsController, LimitsStore, account_lock, blocked_reason,
                          restore_access, validate_limits)
from webui.sessions import parse_sessions
from webui.users import SUSPENDED_PREFIX, UserError, UserManager, UserStore, atomic_private, parse_users
from webui.broker import AuditLogger, BrokerClient, BrokerServer, ServiceController

SECRET = "disposable-test-password-only"
ORIGINAL = f'# untouched\n: RSA "server-key.pem"\nalice : EAP "{SECRET}"\nbob : EAP "another-disposable-password"\n'.encode()


def session(username="alice", identifier="ikev2-eap[7]", children=None):
    return {"id": identifier, "username": username, "children": children if children is not None else
            [{"id": "9", "rx_bytes": 100, "tx_bytes": 200}]}


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.env"
        atomic_private(self.state, b"SERVER_ID=vpn.example.test\nCERT_MODE=public\n")
        self.path = self.root / "secrets"
        atomic_private(self.path, ORIGINAL)
        self.store = LimitsStore(self.root / "limits.json")
        self.calls = []
        self.codes = {}
        self.tick = 100
        self.epoch = "boot-A:daemon-A"
        self.snapshot = {"ok": True, "running": True, "sessions": [session()]}

        def runner(command, **kwargs):
            self.calls.append(command)
            self.assertNotIn(SECRET, repr(command) + repr(kwargs))
            return subprocess.CompletedProcess(command, self.codes.get(command[1], 0))

        self.users = UserStore(self.path, self.state, runner, self.store.path)
        self.reader = type("Reader", (), {"snapshot": lambda _, **kwargs: self.snapshot})()
        self.controller = LimitsController(self.store, self.users, self.reader, runner,
                                           clock=lambda: 1792238400 + self.tick,
                                           epoch=lambda: self.epoch)

    def run_cycle(self):
        return self.controller.cycle()

    def change(self, operation="limits_set", **values):
        request = {"operation": operation, "username": "alice", "limits_revision": self.store.load()["revision"],
                   "expected_digest": hashlib.sha256(self.path.read_bytes()).hexdigest()}
        if operation == "limits_set":
            request.update(byte_limit=None, validity_seconds=None)
        return self.controller.cycle({**request, **values})

    def usage(self, username="alice"):
        return self.store.load()["users"][username]

    def advance(self, rx=130, tx=250, seconds=5):
        self.tick += seconds
        self.snapshot["sessions"] = [session(children=[{"id": "9", "rx_bytes": rx, "tx_bytes": tx}])]
        self.run_cycle()

    def test_existing_accounts_start_unlimited_and_first_sample_is_baseline(self):
        self.run_cycle()
        self.assertIsNone(self.usage()["byte_limit"])
        self.assertIsNone(self.usage()["validity_seconds"])
        self.assertEqual(self.usage()["used_bytes"], 0)
        self.assertIsNone(self.usage()["first_connected_at"])
        self.advance()
        self.assertEqual(self.usage()["used_bytes"], 80)
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_limit_blocks_credentials_before_terminating_only_matching_sessions(self):
        self.run_cycle()
        self.advance()
        self.snapshot["sessions"].append(session("bob", "ikev2-eap[8]", []))
        self.change(byte_limit=80)
        lines, users = parse_users(self.path.read_bytes())
        self.assertTrue(lines[users["alice"]].startswith(SUSPENDED_PREFIX))
        self.assertFalse(lines[users["bob"]].startswith(SUSPENDED_PREFIX))
        self.assertEqual(self.calls, [["/usr/sbin/ipsec", "rereadsecrets"], ["/usr/sbin/ipsec", "down", "ikev2-eap[7]"]])
        self.assertEqual(blocked_reason(self.usage()), "traffic")
        self.assertIn(SECRET, self.path.read_text())
        before = self.path.read_bytes()
        self.run_cycle()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls.count(["/usr/sbin/ipsec", "rereadsecrets"]), 1)

    def test_reset_preserves_limits_restores_same_password_and_keeps_fresh_baselines(self):
        self.run_cycle()
        self.advance()
        self.change(byte_limit=80, validity_seconds=60)
        self.change("usage_reset")
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual((self.usage()["byte_limit"], self.usage()["validity_seconds"]), (80, 60))
        self.assertEqual(self.usage()["used_bytes"], 0)
        self.assertEqual(self.usage()["first_connected_at"], self.controller.clock())
        self.advance(rx=140, tx=260)
        self.assertEqual(self.usage()["used_bytes"], 20)

    def test_limits_are_independently_unlimited_and_increasing_limit_restores_access(self):
        self.run_cycle()
        self.advance()
        self.change(byte_limit=1, validity_seconds=None)
        self.change(byte_limit=1000, validity_seconds=None)
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual(self.usage()["used_bytes"], 80)
        self.change(byte_limit=None, validity_seconds=5)
        self.tick += 5
        self.snapshot["sessions"] = []
        self.run_cycle()
        self.assertEqual(blocked_reason(self.usage(), self.controller.clock()), "expired")
        self.change(byte_limit=None, validity_seconds=None)
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual(self.usage()["used_bytes"], 80)

    def test_simultaneous_devices_share_traffic_and_a_single_validity_period(self):
        self.run_cycle()
        self.change(validity_seconds=100)
        activated = self.usage()["first_connected_at"]
        self.tick += 5
        self.snapshot["sessions"] = [session(children=[{"id": "9", "rx_bytes": 110, "tx_bytes": 210}]),
                                    session(identifier="ikev2-eap[8]", children=[{"id": "10", "rx_bytes": 30, "tx_bytes": 40}])]
        self.run_cycle()
        self.assertEqual(self.usage()["used_bytes"], 90)
        self.assertEqual(self.usage()["first_connected_at"], activated)
        self.tick += 10
        self.snapshot["sessions"] = []
        self.run_cycle()
        self.assertEqual(self.usage()["first_connected_at"], activated)

    def test_rekey_child_deltas_are_not_subtracted_or_double_counted(self):
        self.run_cycle()
        self.tick += 5
        self.snapshot["sessions"] = [session(children=[{"id": "9", "rx_bytes": 120, "tx_bytes": 230},
                                                       {"id": "10", "rx_bytes": 30, "tx_bytes": 40}])]
        self.run_cycle()
        self.assertEqual(self.usage()["used_bytes"], 120)
        self.tick += 5
        self.snapshot["sessions"] = [session(children=[{"id": "10", "rx_bytes": 35, "tx_bytes": 50}])]
        self.run_cycle()
        self.assertEqual(self.usage()["used_bytes"], 135)

    def test_process_and_daemon_restart_preserve_usage_and_validity_activation(self):
        self.run_cycle()
        self.change(validity_seconds=2000)
        activated = self.usage()["first_connected_at"]
        self.advance()
        another = LimitsController(self.store, self.users, self.reader, self.controller.runner,
                                   clock=self.controller.clock, epoch=self.controller.epoch)
        another.cycle()
        self.assertEqual(self.usage()["used_bytes"], 80)
        self.tick += 1000
        self.epoch = "boot-A:daemon-B"
        self.snapshot["sessions"] = [session(children=[{"id": "9", "rx_bytes": 5, "tx_bytes": 10}])]
        another.cycle()
        self.assertEqual(self.usage()["used_bytes"], 95)
        self.assertEqual(self.usage()["first_connected_at"], activated)

    def test_validity_waits_for_first_connection_expires_offline_and_reset_defers_next_period(self):
        self.snapshot["sessions"] = []
        self.run_cycle()
        self.change(validity_seconds=30 * 86400)
        self.tick += 60 * 86400
        self.run_cycle()
        self.assertIsNone(self.usage()["first_connected_at"])
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.snapshot["sessions"] = [session()]
        self.run_cycle()
        started = self.usage()["first_connected_at"]
        self.assertEqual(started, self.controller.clock())
        self.snapshot["sessions"] = []
        self.tick += 30 * 86400
        self.run_cycle()
        self.assertEqual(blocked_reason(self.usage(), self.controller.clock()), "expired")
        self.assertIn(SUSPENDED_PREFIX, self.path.read_text())
        self.change("usage_reset")
        self.assertIsNone(self.usage()["first_connected_at"])
        self.assertEqual(self.usage()["validity_seconds"], 30 * 86400)
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.tick += 86400
        self.snapshot["sessions"] = [session()]
        self.run_cycle()
        self.assertEqual(self.usage()["first_connected_at"], self.controller.clock())

    def test_failed_reload_is_persisted_and_retried_even_when_file_is_already_suspended(self):
        self.run_cycle()
        self.advance()
        self.codes["rereadsecrets"] = 1
        with self.assertRaises(UserError):
            self.change(byte_limit=1)
        self.assertTrue(self.store.load()["reload_pending"])
        self.assertIn(SUSPENDED_PREFIX, self.path.read_text())
        self.codes.clear()
        self.run_cycle()
        self.assertFalse(self.store.load()["reload_pending"])
        self.assertIn(["/usr/sbin/ipsec", "down", "ikev2-eap[7]"], self.calls)

    def test_failure_to_disconnect_is_visible_and_retried(self):
        self.run_cycle()
        self.advance()
        self.codes["down"] = 1
        self.change(byte_limit=1)
        self.assertTrue(self.store.load()["enforcement_error"])
        self.codes.clear()
        self.run_cycle()
        self.assertFalse(self.store.load()["enforcement_error"])

    def test_missing_counters_retain_baselines_and_unknown_status_does_not_reset_usage(self):
        self.run_cycle()
        self.advance()
        self.tick += 5
        self.snapshot["sessions"] = [session(children=[{"id": "9", "rx_bytes": None, "tx_bytes": None}])]
        self.run_cycle()
        self.assertTrue(self.store.load()["enforcement_error"])
        self.advance(rx=140, tx=260)
        self.assertEqual(self.usage()["used_bytes"], 100)
        before = self.store.path.read_bytes()
        self.snapshot = {"ok": False}
        with self.assertRaises(UserError):
            self.run_cycle()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_limits_stale_revisions_and_missing_user_do_not_modify_storage(self):
        self.run_cycle()
        before = (self.store.path.read_bytes(), self.path.read_bytes())
        for changes in ({"byte_limit": 0}, {"validity_seconds": -1}, {"byte_limit": True}, {"byte_limit": 1.5},
                        {"byte_limit": 2 ** 53}, {"validity_seconds": "10"}, {"command": "restart"},
                        {"limits_revision": "a" * 32}, {"username": "../root"}, {"username": "missing"}):
            with self.subTest(changes=changes), self.assertRaises(UserError):
                self.change(**changes)
            self.assertEqual((self.store.path.read_bytes(), self.path.read_bytes()), before)
        with self.assertRaises(UserError):
            self.controller.cycle({"operation": "usage_reset", "username": "alice", "limits_revision": "0" * 32,
                                   "expected_digest": "stale"})

    def test_corrupt_missing_and_unsafe_limits_fail_without_unsuspending_users(self):
        self.run_cycle()
        self.advance()
        self.change(byte_limit=1)
        suspended = self.path.read_bytes()
        saved = self.store.path.read_bytes()
        for data in (b"{broken", b'{"version":99}', b'{"version":1,"users":null}'):
            atomic_private(self.store.path, data)
            with self.assertRaises(UserError):
                self.run_cycle()
            self.assertEqual(self.path.read_bytes(), suspended)
        atomic_private(self.store.path, saved)
        self.store.path.chmod(0o644)
        with self.assertRaises(UserError):
            self.run_cycle()
        self.store.path.unlink()
        with self.assertRaises(UserError):
            self.run_cycle()
        self.assertEqual(self.path.read_bytes(), suspended)

    def test_password_reset_cannot_bypass_suspension_and_delete_recreate_is_unlimited(self):
        self.run_cycle()
        self.advance()
        self.change(byte_limit=1)
        request = {"operation": "reset", "username": "alice", "password": "new-disposable-password",
                   "expected_digest": hashlib.sha256(self.path.read_bytes()).hexdigest()}
        self.users.apply(request)
        lines, indices = parse_users(self.path.read_bytes())
        self.assertTrue(lines[indices["alice"]].startswith(SUSPENDED_PREFIX))
        self.assertEqual(self.usage()["used_bytes"], 80)
        self.users.apply({"operation": "delete", "username": "alice", "expected_digest": hashlib.sha256(self.path.read_bytes()).hexdigest()})
        self.users.apply({"operation": "create", "username": "alice", "password": "replacement-fixture-password",
                          "expected_digest": hashlib.sha256(self.path.read_bytes()).hexdigest()})
        self.run_cycle()
        self.assertIsNone(self.usage()["byte_limit"])
        self.assertEqual(self.usage()["used_bytes"], 0)
        self.assertNotIn(SUSPENDED_PREFIX, self.path.read_text())

    def test_private_lock_serializes_timer_and_mutations(self):
        with account_lock(self.root):
            with self.assertRaises(BlockingIOError):
                with account_lock(self.root, blocking=False):
                    self.fail("Concurrent root mutation acquired the lock")
        with account_lock(self.root, blocking=False):
            pass

    def test_daemon_restart_during_status_does_not_suspend_or_terminate_stale_session_ids(self):
        self.run_cycle()
        self.advance()
        before = self.path.read_bytes()
        with patch.object(self.controller, "epoch", side_effect=["old-daemon", "new-daemon"]):
            with self.assertRaises(UserError):
                self.change(byte_limit=1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_stopped_service_keeps_reload_pending_until_running_again(self):
        self.run_cycle()
        self.advance()
        self.snapshot.update(running=False, sessions=[])
        self.change(byte_limit=1)
        self.assertTrue(self.store.load()["reload_pending"])
        self.assertEqual(self.calls, [])
        self.snapshot.update(running=True)
        self.run_cycle()
        self.assertFalse(self.store.load()["reload_pending"])
        self.assertEqual(self.calls, [["/usr/sbin/ipsec", "rereadsecrets"]])

    def test_explicit_rollback_restores_only_quota_suspensions_and_requires_stopped_worker(self):
        self.run_cycle()
        self.advance()
        self.change(byte_limit=1)
        saved = self.store.path.read_bytes()
        with self.assertRaises(UserError):
            restore_access(self.users, self.controller.runner)
        def stopped_runner(command, **kwargs):
            code = 3 if command[0].endswith("systemctl") and command[-1].startswith("ikev2-web-limits") else 0
            return subprocess.CompletedProcess(command, code)
        restore_access(self.users, stopped_runner)
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual(self.store.path.read_bytes(), saved)

    def test_public_catalog_and_broker_limit_queue_are_audited_and_contain_no_credentials(self):
        self.run_cycle()
        queue = self.root / "queue"
        def runner(command, **kwargs):
            request = json.loads((queue / "request.json").read_text())
            response = self.controller.cycle(request)
            response["request_id"] = request["request_id"]
            atomic_private(queue / "result.json", json.dumps(response).encode())
            return subprocess.CompletedProcess(command, 0)
        manager = UserManager(self.users, queue, runner)
        server = BrokerServer(self.root / "runtime" / "broker.sock", os.getuid(), os.getgid(), ServiceController(),
                              AuditLogger(self.root / "audit.log"), users=manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = BrokerClient(server.socket_path)
            listing = client.list_users()
            self.assertNotIn(SECRET, json.dumps(listing))
            request = {"operation": "limits_set", "username": "alice", "revision": listing["revision"],
                       "limits_revision": listing["limits_revision"], "byte_limit": 10000, "validity_seconds": None}
            self.assertTrue(client.manage_limits(request)["ok"])
            self.assertEqual(self.usage()["byte_limit"], 10000)
            self.assertEqual(client.manage_limits(request)["status"], 409)
            invalid = client.manage_limits({**request, "byte_limit": False})
            self.assertEqual(invalid["status"], 400)
            self.assertEqual(list(queue.iterdir()), [])
            audit = (self.root / "audit.log").read_text()
            self.assertNotIn(SECRET, audit)
            self.assertIn('"operation":"limits_set"', audit)
            server.allowed_uid += 1
            self.assertEqual(client.manage_limits(request)["error"], "peer_not_allowed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class LimitsCliTests(unittest.TestCase):
    def test_cli_discovers_changes_and_deletes_suspended_users_without_enabling_them(self):
        source = (Path(__file__).resolve().parents[1] / "ikev2-strongswan-ubuntu.sh").read_text()
        catalog = source[source.index("get_vpn_users() {"):source.index("parse_connected_vpn_sessions() {")]
        mutations = source[source.index("change_vpn_user_password() ("):source.index("active_ikev2_sa_count() {")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets"
            atomic_private(path, ORIGINAL.replace(b"alice :", SUSPENDED_PREFIX.encode() + b"alice :"))
            script = "set -Eeuo pipefail\n" + catalog + mutations + r"""
require_managed_installation() { :; }
select_vpn_user() { SELECTED_VPN_USER=alice; }
refresh_online_vpn_users() { VPN_SESSION_STATUS_AVAILABLE=no; }
collect_new_vpn_password() { NEW_VPN_PASSWORD=disposable-replacement-password; VPN_PASSWORD_WAS_GENERATED=no; }
escape_secret() { printf '%s' "$1"; }
lock_web_accounts() { :; }
reload_vpn_secrets() { :; }
ask_yes_no() { return 0; }
log() { :; }
info() { :; }
warn() { :; }
get_vpn_users
change_vpn_user_password
"""
            environment = {**os.environ, "IPSEC_SECRETS": str(path)}
            result = subprocess.run(["bash", "-c", script], env=environment, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.splitlines(), ["alice", "bob"])
            self.assertIn(SUSPENDED_PREFIX + 'alice : EAP "disposable-replacement-password"', path.read_text())
            subprocess.run(["bash", "-c", script + "\nremove_vpn_user\n"], env=environment, check=True, stdout=subprocess.DEVNULL)
            self.assertNotIn("alice", parse_users(path.read_bytes())[1])
            self.assertIn(b': RSA "server-key.pem"', path.read_bytes())


if __name__ == "__main__":
    unittest.main()
