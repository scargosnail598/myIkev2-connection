import json
from http.cookiejar import CookieJar
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from webui.app import Ikev2WebServer, SystemStatusProvider  # noqa: E402
from webui.broker import (  # noqa: E402
    AuditLogger,
    BrokerClient,
    BrokerError,
    BrokerServer,
    ServiceController,
    installation_state,
)
from webui.security import AuthConfig, AuthManager, hash_password, verify_password  # noqa: E402


PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD, salt=b"0123456789abcdef")


class FakeRunner:
    def __init__(self, states):
        self.states = states

    def __call__(self, command, **_kwargs):
        state = self.states.get(command[-1], "unknown")
        return subprocess.CompletedProcess(command, 0 if state == "active" else 3, state + "\n", "")


class FakeBroker:
    def __init__(self, available=True):
        self.is_available = available
        self.calls = []

    def available(self):
        return self.is_available

    def status(self):
        if not self.is_available:
            raise BrokerError("Privileged broker is unavailable.")
        return {"ok": True, "installed": True}

    def service_control(self, target, operation):
        self.calls.append((target, operation))
        return {"ok": True, "target": target, "operation": operation, "results": []}


class SystemStatusProviderTests(unittest.TestCase):
    def test_root_only_state_is_reported_as_unknown_without_broker(self):
        with patch("webui.broker.MANAGED_STATE_FILE") as state:
            state.is_file.side_effect = PermissionError("root-only state")
            self.assertIsNone(installation_state())
            dashboard = SystemStatusProvider(FakeRunner({})).dashboard()
        self.assertIsNone(dashboard["installed"])
        self.assertFalse(dashboard["capabilities"]["service_actions"])

    def test_broker_reports_installation_without_exposing_root_state(self):
        with patch("webui.broker.MANAGED_STATE_FILE") as state:
            state.is_file.side_effect = PermissionError("root-only state")
            dashboard = SystemStatusProvider(FakeRunner({}), FakeBroker()).dashboard()
        self.assertTrue(dashboard["installed"])
        self.assertTrue(dashboard["capabilities"]["service_actions"])

    def test_offline_broker_does_not_break_dashboard(self):
        dashboard = SystemStatusProvider(FakeRunner({}), FakeBroker(available=False)).dashboard()
        self.assertEqual(dashboard["mode"], "read-only")

    def test_normalizes_known_service_states(self):
        provider = SystemStatusProvider(FakeRunner({"vpn.service": "active"}))
        self.assertEqual(provider.service_state("vpn.service"), "active")

    def test_rejects_unexpected_systemctl_output(self):
        provider = SystemStatusProvider(FakeRunner({"vpn.service": "surprise"}))
        self.assertEqual(provider.service_state("vpn.service"), "unknown")


class SecurityTests(unittest.TestCase):
    def test_unicode_username_can_authenticate(self):
        auth = AuthManager(AuthConfig("مدیر", PASSWORD_HASH))
        self.assertIsNotNone(auth.authenticate("مدیر", PASSWORD, "client"))
        self.assertIsNone(auth.authenticate("دیگری", PASSWORD, "client"))

    def test_unicode_csrf_is_rejected_without_exception(self):
        auth = AuthManager(AuthConfig("admin", PASSWORD_HASH))
        _token, session = auth.authenticate("admin", PASSWORD, "client")
        self.assertFalse(auth.csrf_valid(session, "نامعتبر"))

    def test_password_hash_round_trip(self):
        self.assertTrue(verify_password(PASSWORD, PASSWORD_HASH))
        self.assertFalse(verify_password("incorrect-password", PASSWORD_HASH))

    def test_short_password_is_rejected(self):
        with self.assertRaises(ValueError):
            hash_password("too-short")

    def test_session_expires(self):
        now = [100.0]
        auth = AuthManager(
            AuthConfig("admin", PASSWORD_HASH, secure_cookie=False, session_ttl_seconds=300),
            clock=lambda: now[0],
        )
        token, _session = auth.authenticate("admin", PASSWORD, "client")
        self.assertIsNotNone(auth.session(token))
        now[0] = 401.0
        self.assertIsNone(auth.session(token))

    def test_login_is_locked_after_five_failures(self):
        now = [100.0]
        auth = AuthManager(
            AuthConfig("admin", PASSWORD_HASH, secure_cookie=False, session_ttl_seconds=300),
            clock=lambda: now[0],
        )
        for _attempt in range(5):
            self.assertIsNone(auth.authenticate("admin", "wrong-password", "client"))
        allowed, retry_after = auth.login_allowed("client")
        self.assertFalse(allowed)
        self.assertEqual(retry_after, 300)
        self.assertIsNone(auth.authenticate("admin", PASSWORD, "client"))
        now[0] = 401.0
        self.assertTrue(auth.login_allowed("client")[0])

    def test_config_must_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"username": "admin", "password_hash": PASSWORD_HASH}))
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "group or others"):
                AuthConfig.from_file(path)


class BrokerTests(unittest.TestCase):
    def test_ikev2_restart_starts_firewall_first(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        result = ServiceController(runner=runner, systemctl="/systemctl").execute("ikev2", "restart")
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [
            ["/systemctl", "start", "ikev2-vpn-firewall.service"],
            ["/systemctl", "restart", "strongswan-starter.service"],
        ])

    def test_command_chain_stops_after_failure(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            returncode = 1 if len(calls) == 2 else 0
            return subprocess.CompletedProcess(command, returncode, "", "failed")

        result = ServiceController(runner=runner).execute("all", "start")
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 2)

    def test_rejects_arbitrary_service_target(self):
        with self.assertRaises(BrokerError):
            ServiceController().execute("ikev2;reboot", "start")

    def test_unix_socket_round_trip_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "broker.sock"
            audit_path = root / "audit.log"
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            server = BrokerServer(
                socket_path,
                os.getuid(),
                os.getgid(),
                ServiceController(runner=runner),
                AuditLogger(audit_path),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = BrokerClient(socket_path)
                self.assertTrue(client.available())
                self.assertIn("installed", client.status())
                response = client.service_control("proxy", "start")
                self.assertTrue(response["ok"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

            events = [json.loads(line) for line in audit_path.read_text().splitlines()]
            self.assertEqual([event["stage"] for event in events], ["requested", "completed"])
            self.assertTrue(events[-1]["ok"])
            self.assertEqual(audit_path.stat().st_mode & 0o777, 0o600)

    def test_audit_log_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("unchanged\n")
            link = root / "audit.log"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                AuditLogger(link).write(
                    peer_uid=os.getuid(),
                    target="ikev2",
                    operation="start",
                    stage="requested",
                    ok=None,
                )
            self.assertEqual(target.read_text(), "unchanged\n")


class WebServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.broker = FakeBroker()
        provider = SystemStatusProvider(FakeRunner({
            "strongswan-starter.service": "active",
            "ikev2-vpn-firewall.service": "active",
            "ikev2-vpn-proxy.service": "inactive",
        }), broker=cls.broker)
        config = AuthConfig("admin", PASSWORD_HASH, secure_cookie=False, session_ttl_seconds=300)
        cls.server = Ikev2WebServer(
            ("127.0.0.1", 0), provider, AuthManager(config), cls.broker
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.csrf_token = None

    def get_json(self, path, authenticated=False):
        if authenticated:
            self.login()
        with self.opener.open(self.base_url + path, timeout=3) as response:
            return response, json.load(response)

    def post_json(self, path, payload=None, headers=None):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        data = json.dumps(payload or {}).encode()
        request = Request(self.base_url + path, method="POST", data=data, headers=request_headers)
        return self.opener.open(request, timeout=3)

    def login(self, password=PASSWORD):
        with self.post_json("/api/auth/login", {"username": "admin", "password": password}) as response:
            payload = json.load(response)
        self.csrf_token = payload["csrf_token"]
        return payload

    def test_health_endpoint(self):
        response, payload = self.get_json("/api/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_dashboard_returns_service_state(self):
        _response, payload = self.get_json("/api/dashboard", authenticated=True)
        states = {item["id"]: item["state"] for item in payload["services"]}
        self.assertEqual(states, {"ikev2": "active", "firewall": "active", "proxy": "inactive"})
        self.assertTrue(payload["capabilities"]["service_actions"])

    def test_service_control_reaches_allowlisted_broker(self):
        self.login()
        with self.post_json(
            "/api/services/ikev2/restart",
            headers={"X-CSRF-Token": self.csrf_token},
        ) as response:
            payload = json.load(response)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.broker.calls[-1], ("ikev2", "restart"))

    def test_service_control_rejects_unknown_target(self):
        self.login()
        with self.assertRaises(HTTPError) as raised:
            self.post_json(
                "/api/services/arbitrary/restart",
                headers={"X-CSRF-Token": self.csrf_token},
            )
        self.assertEqual(raised.exception.code, 400)

    def test_dashboard_requires_authentication(self):
        with self.assertRaises(HTTPError) as raised:
            self.get_json("/api/dashboard")
        self.assertEqual(raised.exception.code, 401)

    def test_login_uses_http_only_same_site_cookie(self):
        with self.post_json(
            "/api/auth/login", {"username": "admin", "password": PASSWORD}
        ) as response:
            payload = json.load(response)
            cookie = response.headers["Set-Cookie"]
        self.assertTrue(payload["authenticated"])
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Secure", cookie)

    def test_invalid_login_is_generic(self):
        with self.assertRaises(HTTPError) as raised:
            self.login(password="wrong-password")
        self.assertEqual(raised.exception.code, 401)
        payload = json.load(raised.exception)
        self.assertEqual(payload["message"], "Invalid username or password.")

    def test_unicode_login_returns_unauthorized(self):
        with self.assertRaises(HTTPError) as raised:
            self.post_json("/api/auth/login", {"username": "مدیر", "password": PASSWORD})
        self.assertEqual(raised.exception.code, 401)

    def test_logout_requires_csrf(self):
        self.login()
        with self.assertRaises(HTTPError) as raised:
            self.post_json("/api/auth/logout")
        self.assertEqual(raised.exception.code, 403)

    def test_logout_invalidates_session(self):
        self.login()
        with self.post_json(
            "/api/auth/logout", headers={"X-CSRF-Token": self.csrf_token}
        ) as response:
            self.assertFalse(json.load(response)["authenticated"])
        with self.assertRaises(HTTPError) as raised:
            self.get_json("/api/dashboard")
        self.assertEqual(raised.exception.code, 401)

    def test_path_traversal_is_not_served(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base_url + "/assets/../app.py", timeout=3)
        self.assertEqual(raised.exception.code, 404)


class ConfigCommandTests(unittest.TestCase):
    def test_configure_cookie_preserves_credentials_owner_and_extra_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertEqual(self.create_config(path, PASSWORD).returncode, 0)
            payload = json.loads(path.read_text())
            payload["extra"] = "preserved"
            path.write_text(json.dumps(payload))
            original = path.stat()
            for options, secure in [((), True), (("--allow-http",), False)]:
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "webui.manage", "configure-cookie",
                     "--config", str(path), *options], text=True, capture_output=True, cwd=REPO,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = dict(payload, secure_cookie=secure)
                self.assertEqual(json.loads(path.read_text()), expected)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), (original.st_uid, original.st_gid))
                self.assertEqual(len(list(path.parent.iterdir())), 1)

    def reset_password(self, path, password, *options):
        return subprocess.run(
            [sys.executable, "-B", "-m", "webui.manage", "reset-password",
             "--config", str(path), "--password-stdin", *options],
            input=password, text=True, capture_output=True, cwd=REPO, check=False,
        )

    def test_reset_replaces_password_and_preserves_config_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            payload = {"username": "operator", "password_hash": PASSWORD_HASH,
                       "secure_cookie": True, "session_ttl_seconds": 7200, "extra": "preserved"}
            path.write_text(json.dumps(payload))
            path.chmod(0o600)
            original = path.stat()
            new_password = "new secret for admin"
            result = self.reset_password(path, new_password)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = AuthConfig.from_file(path)
            self.assertTrue(verify_password(new_password, config.password_hash))
            self.assertFalse(verify_password(PASSWORD, config.password_hash))
            self.assertEqual(config.username, "operator")
            self.assertTrue(config.secure_cookie)
            self.assertEqual(config.session_ttl_seconds, 7200)
            self.assertEqual(json.loads(path.read_text())["extra"], "preserved")
            self.assertEqual(path.stat().st_uid, original.st_uid)
            self.assertEqual(path.stat().st_gid, original.st_gid)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(new_password, path.read_text() + result.stdout + result.stderr)
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_reset_can_restore_admin_username(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertEqual(self.create_config(path, PASSWORD).returncode, 0)
            payload = json.loads(path.read_text())
            payload["username"] = "operator"
            path.write_text(json.dumps(payload))
            result = self.reset_password(path, PASSWORD, "--username", "admin")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(AuthConfig.from_file(path).username, "admin")

    def test_invalid_reset_keeps_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertEqual(self.create_config(path, PASSWORD).returncode, 0)
            original = path.read_bytes()
            for password, options in [("short", ()), (PASSWORD + "\n", ()), (PASSWORD, ("--username", ""))]:
                result = self.reset_password(path, password, *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_bytes(), original)

    def test_reset_requires_existing_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertNotEqual(self.reset_password(path, PASSWORD).returncode, 0)
            self.assertFalse(path.exists())
            path.write_text(json.dumps({"username": "admin", "password_hash": PASSWORD_HASH}))
            path.chmod(0o644)
            original = path.read_bytes()
            self.assertNotEqual(self.reset_password(path, PASSWORD).returncode, 0)
            self.assertEqual(path.read_bytes(), original)

    def create_config(self, path, password):
        return subprocess.run(
            [sys.executable, "-B", "-m", "webui.manage", "create-config",
             "--output", str(path), "--allow-http", "--password-stdin"],
            input=password, text=True, capture_output=True, cwd=REPO, check=False,
        )

    def test_password_from_stdin_creates_private_hashed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            result = self.create_config(path, PASSWORD)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = AuthConfig.from_file(path)
            self.assertTrue(verify_password(PASSWORD, config.password_hash))
            self.assertFalse(config.secure_cookie)
            self.assertNotIn(PASSWORD, path.read_text() + result.stdout + result.stderr)

    def test_empty_stdin_fails_without_creating_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            result = self.create_config(path, "")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("at least 12", result.stderr)
            self.assertFalse(path.exists())

    def test_stdin_does_not_overwrite_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("existing config")
            result = self.create_config(path, PASSWORD)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(), "existing config")


if __name__ == "__main__":
    unittest.main()
