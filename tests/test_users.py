import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from webui.users import UserError, UserManager, UserStore, atomic_private, parse_users
from webui.broker import AuditLogger, BrokerClient, BrokerServer, ServiceController

FIXTURE_SECRET = "test secret with 'quote' and \\slash"
ORIGINAL = b'# Keep server identity\n: RSA "server-key.pem"\nalice : EAP "fixture-password-only"\n'


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "secrets"
        self.state = self.directory / "state.env"
        self.state.touch()
        atomic_private(self.path, ORIGINAL)
        self.commands = []
        self.active = True
        self.reload_ok = True

        def runner(command, **kwargs):
            self.commands.append(command)
            self.assertNotIn(FIXTURE_SECRET, repr(command) + repr(kwargs))
            code = 0 if (self.active if "is-active" in command else self.reload_ok) else 3
            return subprocess.CompletedProcess(command, code)

        self.store = UserStore(self.path, self.state, runner)

    def change(self, operation="create", username="bob", **extra):
        payload = {"operation": operation, "username": username, "password": FIXTURE_SECRET,
                   "expected_digest": hashlib.sha256(self.path.read_bytes()).hexdigest(), **extra}
        return self.store.apply(payload)

    def test_create_reset_delete_preserve_other_lines_and_never_return_password(self):
        for operation, username in [("create", "bob"), ("reset", "alice"), ("delete", "bob")]:
            result = self.change(operation, username)
            self.assertTrue(result["ok"])
            self.assertNotIn(FIXTURE_SECRET, json.dumps(result))
            self.assertTrue(self.path.read_bytes().startswith(ORIGINAL.split(b'alice')[0]))
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIn(f'alice : EAP "{FIXTURE_SECRET}"', self.path.read_text())
        self.assertEqual(set(parse_users(self.path.read_bytes())[1]), {"alice"})
        self.assertEqual(len([c for c in self.commands if "rereadsecrets" in c]), 3)
        self.assertFalse(any("restart" in c for c in self.commands))

    def test_invalid_usernames_passwords_and_operations_do_not_change_storage(self):
        for extra in [{"username": "../root"}, {"username": "a\nb"}, {"username": "a" * 65},
                      {"password": "short"}, {"password": "newline\npassword"},
                      {"password": 'quoted "password" is invalid'},
                      {"password": "رمزعبوربسیارطولانی"}, {"operation": "shell"}]:
            with self.subTest(extra=extra), self.assertRaises(UserError):
                self.change(**extra)
            self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual(self.commands, [])

    def test_duplicate_missing_and_stale_accounts_are_rejected(self):
        for operation, username, extra, code in [
            ("create", "alice", {}, "already_exists"), ("reset", "missing", {}, "not_found"),
            ("delete", "alice", {}, "last_user"),
            ("create", "bob", {"expected_digest": "outdated"}, "conflict"),
        ]:
            with self.subTest(code=code), self.assertRaises(UserError) as error:
                self.change(operation, username, **extra)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(self.path.read_bytes(), ORIGINAL)

    def test_last_user_requires_explicit_boolean_confirmation(self):
        with self.assertRaises(UserError):
            self.change("delete", "alice", allow_last="true")
        self.change("delete", "alice", allow_last=True)
        self.assertEqual(parse_users(self.path.read_bytes())[1], {})
        self.assertIn(b': RSA "server-key.pem"', self.path.read_bytes())

    def test_failed_reload_restores_original_and_never_restarts_vpn(self):
        self.reload_ok = False
        with self.assertRaises(UserError) as error:
            self.change()
        self.assertEqual(error.exception.code, "reload_failed")
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual(len(list(self.directory.glob(".secrets.webui-*"))), 0)

    def test_stopped_vpn_saves_without_starting_or_reloading(self):
        self.active = False
        self.assertTrue(self.change()["applies_on_start"])
        self.assertEqual(len(self.commands), 1)
        self.assertIn("bob", parse_users(self.path.read_bytes())[1])

    def test_unsafe_files_and_unmanaged_installation_are_refused(self):
        self.path.chmod(0o644)
        with self.assertRaises(UserError):
            self.store.snapshot()
        self.path.chmod(0o600)
        original = self.path.with_suffix(".original")
        self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaises(OSError):
            self.store.snapshot()
        self.path.unlink()
        original.rename(self.path)
        self.state.unlink()
        with self.assertRaises(UserError):
            self.change()

    def test_external_edit_before_replace_is_not_overwritten(self):
        with patch.object(self.store, "snapshot", side_effect=[ORIGINAL, ORIGINAL + b"# CLI edit\n"]):
            with self.assertRaises(UserError) as error:
                self.change()
        self.assertEqual(error.exception.code, "conflict")
        self.assertEqual(self.path.read_bytes(), ORIGINAL)

    def test_ambiguous_entries_require_manual_management(self):
        for raw in [ORIGINAL + b'include /etc/other.secrets\n', ORIGINAL + b'alice : EAP "duplicate-secret"\n',
                    ORIGINAL + b'"quoted-user" : EAP "fixture-only"\n']:
            with self.subTest(raw=raw), self.assertRaises(UserError):
                parse_users(raw)

    def test_paginated_catalog_and_revision_never_expose_secrets_or_raw_digests(self):
        self.path.write_bytes(ORIGINAL + b"".join(f'user{i:03} : EAP "fixture-only-password"\n'.encode() for i in range(90)))
        manager = UserManager(self.store)
        first = manager.list_users()
        self.assertEqual(len(first["users"]), 20)
        self.assertEqual(first["total"], 91)
        self.assertLess(len(json.dumps(first).encode()), 4096)
        self.assertNotEqual(first["revision"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertNotIn("fixture-only-password", json.dumps(first))
        self.assertEqual(manager.list_users("USER089")["users"], [{"username": "user089"}])
        self.assertEqual(manager.list_users("absent")["matched"], 0)
        self.assertEqual(manager.list_users(page=999)["page"], first["pages"] - 1)

    def test_queue_runs_fixed_helper_and_cleans_credentials(self):
        queue = self.directory / "queue"
        def runner(command, **kwargs):
            self.assertEqual(command, ["/usr/bin/systemctl", "start", "ikev2-web-users-apply.service"])
            self.assertEqual(queue.stat().st_mode & 0o777, 0o700)
            request = json.loads((queue / "request.json").read_text())
            self.assertEqual((queue / "request.json").stat().st_mode & 0o777, 0o600)
            result = self.store.apply(request)
            result["request_id"] = request["request_id"]
            atomic_private(queue / "result.json", json.dumps(result).encode())
            return subprocess.CompletedProcess(command, 0)
        manager = UserManager(self.store, queue, runner)
        request = {"operation": "create", "username": "bob", "password": FIXTURE_SECRET,
                   "revision": manager.list_users()["revision"]}
        result = manager.execute(request)
        self.assertNotIn(FIXTURE_SECRET, json.dumps(result))
        self.assertEqual(list(queue.iterdir()), [])
        with self.assertRaises(UserError) as error:
            manager.execute(request)
        self.assertEqual(error.exception.code, "conflict")

    def test_forged_revision_and_internal_check_cannot_be_used_as_mutations(self):
        manager = UserManager(self.store)
        for revision in ["é", True, {}, None, "0" * 64]:
            with self.subTest(revision=revision), self.assertRaises(UserError):
                manager.execute({"operation": "delete", "username": "alice", "revision": revision})
        with self.assertRaises(UserError):
            manager.execute({"operation": "check"})

    def test_missing_helper_result_is_reported_without_exposing_password(self):
        manager = UserManager(self.store, self.directory / "queue", lambda command, **kwargs: subprocess.CompletedProcess(command, 1))
        with self.assertRaises(UserError) as error:
            manager.execute({"operation": "create", "username": "bob", "password": FIXTURE_SECRET,
                             "revision": manager.list_users()["revision"]})
        self.assertNotIn(FIXTURE_SECRET, str(error.exception))
        self.assertEqual(list(manager.directory.iterdir()), [])

    def test_concurrent_operations_fail_fast_instead_of_queuing_mutations(self):
        manager = UserManager(self.store)
        manager._lock.acquire()
        try:
            with self.assertRaises(UserError) as error:
                manager.check()
            self.assertEqual(error.exception.code, "busy")
        finally:
            manager._lock.release()

    def test_helper_check_is_read_only(self):
        self.assertTrue(self.store.check()["checked"])
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertFalse(any("rereadsecrets" in command for command in self.commands))
        self.assertIn(["/usr/sbin/ipsec", "status"], self.commands)

    def test_read_only_helper_check_uses_private_queue_and_cleans_it(self):
        queue = self.directory / "queue"
        def runner(command, **kwargs):
            request = json.loads((queue / "request.json").read_text())
            self.assertEqual(request["operation"], "check")
            self.assertNotIn("password", request)
            result = self.store.check()
            result["request_id"] = request["request_id"]
            atomic_private(queue / "result.json", json.dumps(result).encode())
            return subprocess.CompletedProcess(command, 0)
        self.assertTrue(UserManager(self.store, queue, runner).check()["checked"])
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual(list(queue.iterdir()), [])

    def test_unix_broker_lists_users_and_denies_untrusted_peer(self):
        manager = UserManager(self.store)
        server = BrokerServer(self.directory / "broker.sock", os.getuid(), os.getgid(),
                              ServiceController(), AuditLogger(self.directory / "audit.log"), users=manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = BrokerClient(server.socket_path)
            result = client.list_users()
            self.assertEqual(result["users"], [{"username": "alice"}])
            self.assertNotIn("fixture-password-only", json.dumps(result))
            invalid = client.manage_user({"operation": "create", "username": "../../etc/shadow", "password": FIXTURE_SECRET})
            self.assertFalse(invalid["ok"])
            self.assertFalse((self.directory / "audit.log").exists())
            server.allowed_uid += 1
            self.assertEqual(client.list_users()["error"], "peer_not_allowed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
