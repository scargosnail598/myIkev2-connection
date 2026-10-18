"""Run the reset CLI against disposable auth files and a stub systemctl only."""

import errno
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest

from webui.security import AuthConfig, hash_password, verify_password

REPO = Path(__file__).resolve().parents[1]
OLD_PASSWORD = "disposable-old-panel-password"
NEW_PASSWORD = "disposable-new-panel-password"


class ResetCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.installed = self.root / "installed"
        self.installed.mkdir()
        (self.installed / "webui").symlink_to(REPO / "webui", target_is_directory=True)
        self.config = self.root / "auth.json"
        self.payload = {"username": "operator", "password_hash": hash_password(OLD_PASSWORD),
                        "secure_cookie": True, "session_ttl_seconds": 7200, "extra": {"preserved": True}}
        self.config.write_text(json.dumps(self.payload))
        self.config.chmod(0o600)
        self.original = self.config.read_bytes()
        self.calls = self.root / "systemctl.jsonl"
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        systemctl = binary_dir / "systemctl"
        systemctl.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["RESET_TEST_CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "show":
    print(os.environ.get("RESET_TEST_LOAD", "loaded"))
elif sys.argv[1] == "restart":
    raise SystemExit(int(os.environ.get("RESET_TEST_RESTART", "0")))
elif sys.argv[1] == "is-active":
    raise SystemExit(int(os.environ.get("RESET_TEST_ACTIVE", "0")))
else:
    raise SystemExit(99)
''')
        systemctl.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", ""),
                            "RESET_TEST_CALLS": str(self.calls)}
        source = (REPO / "packaging/reset-webui-password.sh").read_text()
        for old, new in (
            ('INSTALL_DIR="/opt/ikev2"', f'INSTALL_DIR="{self.installed}"'),
            ('CONFIG_FILE="/etc/ikev2-web/config.json"', f'CONFIG_FILE="{self.config}"'),
            # CI is unprivileged. Bypass only the fixture's UID check, never sudo
            # or alter a real service/config; every filesystem target is temporary.
            ('[[ ${EUID} -eq 0 ]]', '[[ 0 -eq 0 ]]'),
        ):
            self.assertIn(old, source)
            source = source.replace(old, new)
        self.script = self.root / "reset-webui-password.sh"
        self.script.write_text(source)
        self.script.chmod(0o755)
        self.installer = self.root / "install-webui.sh"
        self.installer.write_text((REPO / "packaging/install-webui.sh").read_text())

    def run_cli(self, password=NEW_PASSWORD, options=("--password-stdin",), installer=False):
        command = ["bash", str(self.installer if installer else self.script)]
        if installer:
            command.append("reset-password")
        return subprocess.run([*command, *options], input=password, text=True, capture_output=True,
                              env=self.environment, timeout=15)

    def commands(self):
        return list(map(json.loads, self.calls.read_text().splitlines())) if self.calls.exists() else []

    def assert_password_changed_safely(self, output):
        payload = json.loads(self.config.read_text())
        expected = {**self.payload, "password_hash": payload["password_hash"]}
        self.assertEqual(payload, expected)
        self.assertTrue(verify_password(NEW_PASSWORD, payload["password_hash"]))
        self.assertFalse(verify_password(OLD_PASSWORD, payload["password_hash"]))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(NEW_PASSWORD, output + self.config.read_text())
        self.assertEqual(self.commands(), [["show", "--property=LoadState", "--value", "ikev2-web.service"],
                                          ["restart", "ikev2-web.service"], ["is-active", "--quiet", "ikev2-web.service"]])

    def test_installer_reset_preserves_owner_and_settings_and_restarts_only_webui(self):
        owner = self.config.stat()
        result = self.run_cli(installer=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_password_changed_safely(result.stdout + result.stderr)
        self.assertEqual((self.config.stat().st_uid, self.config.stat().st_gid), (owner.st_uid, owner.st_gid))
        self.assertIn("sessions were signed out", result.stdout)

    def test_invalid_password_never_changes_config_or_restarts_services(self):
        for password in ("short", NEW_PASSWORD + "\n", "x" * 4097):
            with self.subTest(password_length=len(password)):
                result = self.run_cli(password)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.config.read_bytes(), self.original)
                self.assertFalse(any(command[0] == "restart" for command in self.commands()))
                self.assertNotIn(password, result.stdout + result.stderr)

    def test_unknown_options_and_nonterminal_interactive_reset_are_rejected(self):
        for options in (("--password", NEW_PASSWORD), ()):
            result = self.run_cli(options=options)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.config.read_bytes(), self.original)
            self.assertNotIn(NEW_PASSWORD, result.stdout + result.stderr)
        self.assertIn("requires a terminal", result.stderr)
        self.assertFalse(any(command[0] == "restart" for command in self.commands()))

    def test_missing_config_service_or_tools_fail_before_any_password_change(self):
        self.environment["RESET_TEST_LOAD"] = "not-found"
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.environment.pop("RESET_TEST_LOAD")
        self.config.unlink()
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.config.exists())
        self.config.write_bytes(self.original)
        self.config.chmod(0o600)
        (self.installed / "webui").unlink()
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(any(command[0] == "restart" for command in self.commands()))

    def test_unsafe_or_symlink_config_is_not_overwritten(self):
        self.config.chmod(0o644)
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.config.read_bytes(), self.original)
        target = self.root / "target.json"
        self.config.rename(target)
        target.chmod(0o600)
        self.config.symlink_to(target)
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(target.read_bytes(), self.original)
        self.assertFalse(any(command[0] == "restart" for command in self.commands()))

    def test_restart_failure_reports_saved_password_without_claiming_success(self):
        self.environment["RESET_TEST_RESTART"] = "1"
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("new password was saved", result.stderr)
        self.assertNotIn("reset successfully", result.stdout)
        self.assertTrue(verify_password(NEW_PASSWORD, AuthConfig.from_file(self.config).password_hash))
        self.assertNotIn(NEW_PASSWORD, result.stdout + result.stderr)

    def test_inactive_service_after_restart_reports_failure(self):
        self.environment["RESET_TEST_ACTIVE"] = "3"
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not active", result.stderr)
        self.assertNotIn("reset successfully", result.stdout)

    def interactive(self, confirmation):
        master, slave = pty.openpty()
        process = subprocess.Popen(["bash", str(self.script)], stdin=slave, stdout=slave, stderr=slave,
                                   env=self.environment, start_new_session=True)
        os.close(slave)
        transcript = bytearray()
        def read_until(marker):
            deadline = time.monotonic() + 10
            while marker not in transcript:
                self.assertLess(time.monotonic(), deadline, "Password prompt did not arrive")
                if select.select([master], [], [], 0.1)[0]:
                    transcript.extend(os.read(master, 4096))
        try:
            read_until(b"minimum 12 characters")
            os.write(master, (NEW_PASSWORD + "\n").encode())
            read_until(b"Confirm Web UI password:")
            os.write(master, (confirmation + "\n").encode())
            process.wait(timeout=10)
            while select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 4096)
                    if not chunk:
                        break
                    transcript.extend(chunk)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
            return process.returncode, transcript.decode()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def test_interactive_password_is_confirmed_without_terminal_echo(self):
        code, output = self.interactive(NEW_PASSWORD)
        self.assertEqual(code, 0, output)
        self.assert_password_changed_safely(output)

    def test_interactive_mismatch_preserves_config_and_does_not_restart(self):
        code, output = self.interactive("different-disposable-password")
        self.assertNotEqual(code, 0)
        self.assertIn("Passwords do not match", output)
        self.assertNotIn(NEW_PASSWORD, output)
        self.assertNotIn("different-disposable-password", output)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(any(command[0] == "restart" for command in self.commands()))

    @unittest.skipIf(os.geteuid() == 0, "Requires an unprivileged test process")
    def test_real_script_requires_root_before_reading_configuration(self):
        result = subprocess.run(["bash", str(REPO / "packaging/reset-webui-password.sh"), "--password-stdin"],
                                input=NEW_PASSWORD, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sudo or as root", result.stderr)


if __name__ == "__main__":
    unittest.main()
