import base64
import hashlib
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
import zlib

from webui.profiles import build_share, SHARE_PREFIX
from webui.users import UserError, atomic_private


class ProfileShareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.ca = Path(cls.temp.name) / "ca.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:4096", "-nodes",
                        "-keyout", "/dev/null", "-out", str(cls.ca), "-days", "1",
                        "-subj", "/CN=Disposable share test CA"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def setUp(self):
        self.temp_state = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_state.cleanup)
        self.state = Path(self.temp_state.name) / "state.env"

    def settings(self, extra=""):
        atomic_private(self.state, ("SERVER_ID=vpn.example.test\n" + extra).encode())

    def decode(self, result):
        self.assertTrue(result["uri"].startswith(SHARE_PREFIX))
        encoded = result["uri"][len(SHARE_PREFIX):]
        return json.loads(zlib.decompress(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))))

    def test_public_export_preserves_v1_profile_and_no_password_by_default(self):
        self.settings("CERTIFICATE_MODE=public-trust\nCERT_MODE=public\n")
        result = build_share(self.state, "alice", certificate=Path("/nonexistent"))
        data = self.decode(result)
        self.assertEqual((data["format"], data["version"]), ("ikev-share", 1))
        self.assertNotIn("password", data)
        profile = data["profile"]
        self.assertEqual((profile["format"], profile["version"]), ("ikev-profile", 1))
        self.assertEqual(profile["certificate_trust"], "public")
        self.assertNotIn("ca_certificate", profile)
        self.assertEqual(profile["connection"], {"mode": "full-tunnel", "ipv6": False})
        self.assertEqual(profile["remote_id"], profile["server"])
        self.assertFalse(result["includes_password"])

    def test_4096_bit_private_ca_password_ipv6_and_proxy_roundtrip_within_qr_capacity(self):
        self.settings("CERT_MODE=private\nIPV6_ENABLED=yes\nALLOW_STOCK_WINDOWS=yes\n"
                      "PROXY_ENABLED=yes\nPROXY_IP=10.254.254.1\nPROXY_PORT=1080\n")
        password = "fixture ' literal \\ secret!"
        result = build_share(self.state, "u" * 64, password, self.ca)
        data = self.decode(result)
        self.assertEqual(data["password"], password)
        self.assertTrue(result["includes_password"])
        profile = data["profile"]
        self.assertTrue(profile["connection"]["ipv6"])
        self.assertEqual(profile["server_profile"], "stock-windows-compatible")
        self.assertEqual(profile["proxy"], {"enabled": True, "type": "socks5", "host": "10.254.254.1", "port": 1080})
        ca = profile["ca_certificate"]
        der = ssl.PEM_cert_to_DER_cert(self.ca.read_text())
        self.assertEqual(base64.b64decode(ca["data"]), der)
        self.assertEqual(ca["sha256"].replace(":", "").lower(), hashlib.sha256(der).hexdigest())
        self.assertLessEqual(len(result["uri"]), 2953)

    def test_missing_invalid_ca_settings_and_shell_code_fail_closed(self):
        for extra in ("SERVER_ID=$(touch /tmp/should-never-run)\n", "CERT_MODE=unrecognized\n",
                      "SERVER_ID=second.example.test\n", "PROXY_ENABLED=yes\nPROXY_IP=invalid\nPROXY_PORT=1080\n",
                      "IPV6_ENABLED=maybe\n"):
            self.settings(extra)
            with self.subTest(extra=extra), self.assertRaises(UserError):
                build_share(self.state, "alice", certificate=self.ca)
        self.settings("CERT_MODE=private\n")
        for pem in (None, "-----BEGIN CERTIFICATE-----\nRkFLRQ==\n-----END CERTIFICATE-----\n"):
            ca = self.state.parent / "missing.pem"
            if pem is not None:
                ca.write_text(pem)
            with self.assertRaises(UserError):
                build_share(self.state, "alice", certificate=ca)

    def test_oversized_share_is_rejected(self):
        self.settings("CERT_MODE=public\n")
        import secrets
        with self.assertRaises(UserError) as error:
            build_share(self.state, "alice", secrets.token_urlsafe(4000))
        self.assertEqual(error.exception.code, "profile_too_large")
