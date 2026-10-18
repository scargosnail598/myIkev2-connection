"""TOTP vectors, durable replay prevention, enrollment and HTTP access controls."""

import base64
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from webui.app import Ikev2WebServer
from webui.security import AuthConfig, AuthError, AuthManager, hash_password
from webui.totp import code_at, matching_counter, provisioning_uri, valid_secret

PASSWORD = "disposable-authenticator-panel-password"
PASSWORD_HASH = hash_password(PASSWORD)
SECRET = base64.b32encode(b"12345678901234567890").decode()
REPO = Path(__file__).resolve().parents[1]


class TotpTests(unittest.TestCase):
    def test_rfc_6238_sha1_vectors_with_six_digit_truncation(self):
        for timestamp, expected in ((59, "94287082"), (1111111109, "07081804"),
                                    (1111111111, "14050471"), (1234567890, "89005924"),
                                    (2000000000, "69279037"), (20000000000, "65353130")):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(code_at(SECRET, timestamp // 30), expected[-6:])

    def test_clock_window_format_and_replay(self):
        for counter in (99, 100, 101):
            code = code_at(SECRET, counter)
            self.assertEqual(matching_counter(SECRET, code, 3000), counter)
            self.assertIsNone(matching_counter(SECRET, code, 3000, counter))
        self.assertIsNone(matching_counter(SECRET, code_at(SECRET, 98), 3000))
        for malformed in (None, 123456, "12345", "1234567", "۱۲۳۴۵۶", "123456\n", " 123456"):
            self.assertIsNone(matching_counter(SECRET, malformed, 3000))

    def test_provisioning_uri_encodes_username_and_issuer(self):
        from urllib.parse import parse_qs, urlparse, unquote
        parsed = urlparse(provisioning_uri(SECRET, "مدیر:a?b/c"))
        self.assertEqual(parsed.scheme, "otpauth")
        self.assertEqual(unquote(parsed.path), "/IKEv2 Web UI:مدیر:a?b/c")
        self.assertEqual(parse_qs(parsed.query)["secret"], [SECRET])
        self.assertEqual(parse_qs(parsed.query)["period"], ["30"])
        self.assertTrue(valid_secret(SECRET))


class PersistentAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.payload = {"username": "operator", "password_hash": PASSWORD_HASH, "secure_cookie": False,
                        "session_ttl_seconds": 600, "custom": {"keep": True}}
        self.write(self.payload)
        self.now = [3000.0]
        self.clock = [100.0]
        self.auth = self.manager()

    def write(self, payload):
        self.path.write_text(json.dumps(payload))
        self.path.chmod(0o600)

    def manager(self):
        return AuthManager(AuthConfig.from_file(self.path), config_path=self.path,
                           wall_clock=lambda: self.now[0], clock=lambda: self.clock[0])

    def login(self, auth=None, code=None, client="client"):
        return (auth or self.auth).authenticate("operator", PASSWORD, client, code)

    def enroll(self):
        token, session = self.login()
        data = self.auth.begin_two_factor(token, PASSWORD, "client")
        code = code_at(data["secret"], 100)
        self.auth.confirm_two_factor(token, code, "client")
        return token, session, data["secret"]

    def test_pending_setup_is_not_persisted_and_is_session_bound(self):
        token, _ = self.login()
        other, _ = self.login()
        data = self.auth.begin_two_factor(token, PASSWORD, "client")
        self.assertNotIn(data["secret"], self.path.read_text())
        self.assertFalse(self.auth.two_factor_status()["enabled"])
        with self.assertRaises(AuthError):
            self.auth.confirm_two_factor(other, code_at(data["secret"], 100), "client")
        self.auth.cancel_two_factor(token)
        with self.assertRaises(AuthError):
            self.auth.confirm_two_factor(token, code_at(data["secret"], 100), "client")

    def test_confirmation_is_required_expires_and_rejects_wrong_code(self):
        token, _ = self.login()
        data = self.auth.begin_two_factor(token, PASSWORD, "client")
        with self.assertRaises(AuthError):
            self.auth.confirm_two_factor(token, "invalid", "client")
        self.assertFalse(self.auth.two_factor_status()["enabled"])
        self.clock[0] += 601
        with self.assertRaises(AuthError):
            self.auth.confirm_two_factor(token, code_at(data["secret"], 100), "client")
        self.assertNotIn("totp_secret", json.loads(self.path.read_text()))

    def test_enable_preserves_settings_invalidates_other_sessions_and_requires_code(self):
        other, _ = self.login()
        token, _, secret = self.enroll()
        self.assertIsNone(self.auth.session(other))
        self.assertIsNotNone(self.auth.session(token))
        self.assertEqual(self.auth.two_factor_status(), {"enabled": True})
        self.assertEqual(json.loads(self.path.read_text())["custom"], self.payload["custom"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.login())
        self.assertIsNone(self.login(code=code_at(secret, 100)))  # Enrollment consumed this step.
        self.now[0] += 30
        self.assertIsNotNone(self.login(code=code_at(secret, 101)))

    def test_replay_is_rejected_after_restart_and_across_concurrent_managers(self):
        _, _, secret = self.enroll()
        self.now[0] += 30
        first, second = self.manager(), self.manager()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda auth: self.login(auth, code_at(secret, 101)), (first, second)))
        self.assertEqual(sum(result is not None for result in outcomes), 1)
        self.assertIsNone(self.login(self.manager(), code_at(secret, 101)))

    def test_disable_requires_password_and_fresh_code(self):
        token, _, secret = self.enroll()
        with self.assertRaises(AuthError):
            self.auth.disable_two_factor(token, "wrong", code_at(secret, 100), "client")
        with self.assertRaises(AuthError):
            self.auth.disable_two_factor(token, PASSWORD, code_at(secret, 100), "client")
        self.now[0] += 30
        self.auth.disable_two_factor(token, PASSWORD, code_at(secret, 101), "client")
        self.assertFalse(self.manager().two_factor_status()["enabled"])
        self.assertIsNotNone(self.login())

    def test_otp_failures_are_throttled_and_password_does_not_bypass_them(self):
        self.enroll()
        for _ in range(5):
            self.assertIsNone(self.login(code="wrong"))
        self.assertFalse(self.auth.login_allowed("client")[0])
        self.clock[0] += 301
        self.assertTrue(self.auth.login_allowed("client")[0])

    def test_setup_and_confirmation_failures_share_rate_limit(self):
        token, _ = self.login()
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.auth.begin_two_factor(token, "wrong", "client")
        with self.assertRaises(AuthError) as raised:
            self.auth.begin_two_factor(token, PASSWORD, "client")
        self.assertEqual(raised.exception.status, 429)

    def test_failed_persistence_cannot_issue_a_session_or_enable_totp(self):
        token, _ = self.login()
        data = self.auth.begin_two_factor(token, PASSWORD, "client")
        with patch("webui.security.write_existing_config", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(AuthError) as raised:
                self.auth.confirm_two_factor(token, code_at(data["secret"], 100), "client")
        self.assertEqual(raised.exception.status, 503)
        self.assertFalse(self.manager().two_factor_status()["enabled"])

    def test_cli_reset_clears_totp_and_invalidates_live_sessions(self):
        token, _, _ = self.enroll()
        result = subprocess.run([sys.executable, "-B", "-m", "webui.manage", "reset-password",
                                 "--config", str(self.path), "--password-stdin"], input=PASSWORD,
                                text=True, capture_output=True, cwd=REPO, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("two-factor authentication disabled", result.stdout)
        payload = json.loads(self.path.read_text())
        self.assertNotIn("totp_secret", payload)
        self.assertNotIn("totp_last_counter", payload)
        self.assertIsNone(self.auth.session(token))
        self.assertIsNotNone(self.login())

    def test_pending_setup_cannot_enable_totp_after_cli_recovery(self):
        token, _ = self.login()
        data = self.auth.begin_two_factor(token, PASSWORD, "client")
        result = subprocess.run([sys.executable, "-B", "-m", "webui.manage", "reset-password",
                                 "--config", str(self.path), "--password-stdin"], input=PASSWORD,
                                text=True, capture_output=True, cwd=REPO, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(AuthError) as raised:
            self.auth.confirm_two_factor(token, code_at(data["secret"], 100), "client")
        self.assertEqual(raised.exception.status, 401)
        self.assertFalse(self.auth.two_factor_status()["enabled"])

    def test_cookie_configuration_preserves_two_factor(self):
        self.enroll()
        previous = json.loads(self.path.read_text())
        result = subprocess.run([sys.executable, "-B", "-m", "webui.manage", "configure-cookie",
                                 "--config", str(self.path)], capture_output=True, cwd=REPO, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.path.read_text()), {**previous, "secure_cookie": True})

    def test_malformed_or_unreadable_config_fails_closed(self):
        token, _ = self.login()
        for updates in ({"totp_secret": "bad"}, {"totp_last_counter": True}):
            self.write({**self.payload, **updates})
            with self.assertRaises(ValueError):
                AuthConfig.from_file(self.path)
            with self.assertRaises(AuthError):
                self.login()
            with self.assertRaises(AuthError):
                self.auth.session(token)


class TwoFactorHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Ikev2WebServer(("127.0.0.1", 0), None, AuthManager(AuthConfig("operator", PASSWORD_HASH)), None)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.path.write_text(json.dumps({"username": "operator", "password_hash": PASSWORD_HASH, "secure_cookie": False}))
        self.path.chmod(0o600)
        self.now = 3000
        self.server.auth_manager = AuthManager(AuthConfig.from_file(self.path), config_path=self.path, wall_clock=lambda: self.now)
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf = None

    def request(self, path, payload=None, csrf=True):
        headers = {"Content-Type": "application/json"}
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with self.opener.open(request, timeout=5) as response:
            return response, json.load(response)

    def login(self, code=None):
        response, data = self.request("/api/auth/login", {"username": "operator", "password": PASSWORD, "code": code})
        self.csrf = data["csrf_token"]
        return response, data

    def test_two_factor_endpoints_require_session_and_csrf(self):
        for path, payload in (("/api/auth/2fa", None), ("/api/auth/2fa/setup", {"password": PASSWORD}),
                              ("/api/auth/2fa/enable", {"code": "123456"}),
                              ("/api/auth/2fa/disable", {"password": PASSWORD, "code": "123456"}),
                              ("/api/auth/2fa/cancel", {})):
            with self.assertRaises(HTTPError) as raised:
                self.request(path, payload)
            self.assertEqual(raised.exception.code, 401)
        self.login()
        with self.assertRaises(HTTPError) as raised:
            self.request("/api/auth/2fa/setup", {"password": PASSWORD}, csrf=False)
        self.assertEqual(raised.exception.code, 403)

    def test_enrollment_login_disable_and_secret_redaction(self):
        self.login()
        response, data = self.request("/api/auth/2fa/setup", {"password": PASSWORD})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        secret = data["secret"]
        self.assertTrue(valid_secret(secret))
        response, data = self.request("/api/auth/2fa/enable", {"code": code_at(secret, 100)})
        self.assertEqual(data, {"enabled": True})
        self.assertNotIn(secret, json.dumps(data))
        _, data = self.request("/api/auth/2fa")
        self.assertEqual(data, {"enabled": True})
        self.request("/api/auth/logout", {})
        for code in (None, "000", code_at(secret, 100)):
            with self.assertRaises(HTTPError) as raised:
                self.login(code)
            self.assertEqual(raised.exception.code, 401)
            self.assertIsNone(raised.exception.headers.get("Set-Cookie"))
        self.now += 30
        self.login(code_at(secret, 101))
        self.now += 30
        _, data = self.request("/api/auth/2fa/disable", {"password": PASSWORD, "code": code_at(secret, 102)})
        self.assertEqual(data, {"enabled": False})
        self.request("/api/auth/logout", {})
        self.login()

    def test_extra_fields_rejected_and_config_failure_is_service_unavailable(self):
        self.login()
        with self.assertRaises(HTTPError) as raised:
            self.request("/api/auth/2fa/setup", {"password": PASSWORD, "secret": SECRET})
        self.assertEqual(raised.exception.code, 400)
        self.path.chmod(0o644)
        with self.assertRaises(HTTPError) as raised:
            self.request("/api/auth/2fa")
        self.assertEqual(raised.exception.code, 503)
        self.assertNotIn(SECRET, raised.exception.read().decode())
