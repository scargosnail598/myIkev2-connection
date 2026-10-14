"""ACME regressions: real temporary certificates, mocked service/Certbot commands.

Never source the installer or invoke privileged VPN commands in these tests.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "ikev2-strongswan-ubuntu.sh").read_text()
DOMAIN = "vpn.example.test"


def function(name, following):
    start = SOURCE.index(f"\n{name}() {{") + 1
    end = SOURCE.index(f"\n{following}() {{", start)
    return SOURCE[start:end]


VALIDATE = function("validate_acme_certificate", "install_acme_deploy_hook")
HOOK = function("install_acme_deploy_hook", "create_certificates")
CREATE = function("create_certificates", "write_ipsec_config")
RENEW = function("renew_acme_certificate", "diag_reset")


class AcmeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory(prefix="ikev2-cert-tests-")
        cls.fixture_root = Path(cls.fixtures.name)
        for name, serial, algorithm in (("old", "0x11", "rsa:2048"),
                                        ("new", "0x00AABB", "rsa:2048"),
                                        ("ec", "0x33", "ec")):
            args = ["openssl", "req", "-x509", "-newkey", algorithm]
            if algorithm == "ec":
                args += ["-pkeyopt", "ec_paramgen_curve:prime256v1"]
            args += ["-nodes", "-sha256", "-days", "2", "-set_serial", serial,
                     "-subj", f"/CN={DOMAIN}", "-addext", f"subjectAltName=DNS:{DOMAIN}",
                     "-addext", "extendedKeyUsage=serverAuth",
                     "-out", str(cls.fixture_root / f"{name}.pem"),
                     "-keyout", str(cls.fixture_root / f"{name}.key")]
            subprocess.run(args, check=True, capture_output=True)
        subprocess.run(["openssl", "x509", "-in", str(cls.fixture_root / "new.pem"),
                        "-signkey", str(cls.fixture_root / "new.key"), "-days", "-1",
                        "-out", str(cls.fixture_root / "expired.pem")],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ikev2-acme-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.cert = self.root / "server.pem"
        self.key = self.root / "server.key"
        self.hook = self.root / "deploy-hook"
        self.live_root = self.root / "live"
        self.live = self.live_root / DOMAIN
        self.live.mkdir(parents=True)
        self.trace = self.root / "trace"
        self.listing = self.root / "listing"
        self.listing.write_text('  subject: "CN=vpn.example.test"\n'
                                '  serial: 00:aa:bb\n'
                                '  pubkey: RSA 2048 bits, has private key\n')
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        TRACE=str(self.trace), LISTING=str(self.listing), SERVICE_ACTIVE="yes")
        self.set_source("new")
        shutil.copyfile(self.fixture_root / "old.pem", self.cert)
        shutil.copyfile(self.fixture_root / "old.key", self.key)
        self.key.chmod(0o600)
        self.old_pair = (self.cert.read_bytes(), self.key.read_bytes())
        self.mock("logger", 'printf "logger %s\\n" "$*" >> "$TRACE"\n')
        self.mock("systemctl", 'printf "systemctl %s\\n" "$*" >> "$TRACE"\n'
                  '[[ "$SERVICE_ACTIVE" == yes ]]\n')
        self.mock("sleep", ':\n')
        self.mock("ipsec", '''printf 'ipsec %s\n' "$*" >> "$TRACE"
case "$1" in
  rereadall) [[ "${FAIL_SECRETS:-no}" != yes ]] ;;
  reload) [[ "${FAIL_RELOAD:-no}" != yes ]] ;;
  listcerts) cat "$LISTING" ;;
  *) exit 99 ;;
esac
''')
        self.generate_hook()

    def mock(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\n" + body)
        path.chmod(0o755)

    def set_source(self, name):
        shutil.copyfile(self.fixture_root / f"{name}.pem", self.live / "fullchain.pem")
        shutil.copyfile(self.fixture_root / f"{name}.key", self.live / "privkey.pem")

    def bash(self, body, *args):
        return subprocess.run(["bash", "-s", "--", *map(str, args)],
                              input="set -Eeuo pipefail\n" + body, env=self.env,
                              text=True, capture_output=True, timeout=20)

    def generate_hook(self):
        result = self.bash(VALIDATE + HOOK + '''
INSTALLER_NAME=ikev2-test
STRONGSWAN_SERVICE=strongswan-starter.service
ACME_DOMAIN="$1"
ACME_CERT_PATH="$2"
ACME_KEY_PATH="$3"
ACME_DEPLOY_HOOK="$4"
install_acme_deploy_hook
''', DOMAIN, self.cert, self.key, self.hook)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Redirect only the Certbot input directory; execute the emitted hook itself.
        self.hook.write_text(self.hook.read_text().replace("/etc/letsencrypt/live/",
                                                          f"{self.live_root}/"))
        for command in (("bash", "-n"), ("shellcheck", "--severity=warning")):
            if shutil.which(command[0]):
                result = subprocess.run([*command, str(self.hook)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def deploy(self):
        return subprocess.run(["bash", str(self.hook)], env=self.env,
                              text=True, capture_output=True, timeout=20)

    def assert_old_pair(self):
        self.assertEqual((self.cert.read_bytes(), self.key.read_bytes()), self.old_pair)
        self.assertFalse(list(self.root.glob("*.new.*")))
        self.assertFalse(list(self.root.glob(".ikev2-acme.*")))

    def test_rsa_deployment_reloads_key_and_leaf_certificate(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cert.read_bytes(), (self.live / "fullchain.pem").read_bytes())
        self.assertEqual(self.key.read_bytes(), (self.live / "privkey.pem").read_bytes())
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        trace = self.trace.read_text()
        self.assertLess(trace.index("ipsec rereadall"), trace.index("ipsec reload"))
        self.assertLess(trace.index("ipsec reload"), trace.index("ipsec listcerts"))

    def test_rejects_ecdsa_without_changing_managed_files(self):
        self.set_source("ec")
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assert_old_pair()
        self.assertNotIn("ipsec", self.trace.read_text())

    def test_rejects_mismatched_key_without_changing_managed_files(self):
        shutil.copyfile(self.fixture_root / "old.key", self.live / "privkey.pem")
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assert_old_pair()

    def test_rejects_expired_certificate(self):
        shutil.copyfile(self.fixture_root / "expired.pem", self.live / "fullchain.pem")
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assert_old_pair()

    def test_rejects_wrong_hostname(self):
        result = self.bash(VALIDATE + '\nvalidate_acme_certificate "$1" "$2" wrong.example.test\n',
                           self.live / "fullchain.pem", self.live / "privkey.pem")
        self.assertNotEqual(result.returncode, 0)

    def test_rolls_back_when_credentials_cannot_be_reloaded(self):
        self.env["FAIL_SECRETS"] = "yes"
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assert_old_pair()

    def test_rolls_back_when_connections_cannot_be_reloaded(self):
        self.env["FAIL_RELOAD"] = "yes"
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assert_old_pair()

    def test_rolls_back_when_new_certificate_is_not_loaded(self):
        self.listing.write_text('subject: "CN=vpn.example.test"\nserial: 11\n'
                                'pubkey: RSA 2048 bits, has private key\n')
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assert_old_pair()

    def test_requires_the_loaded_certificate_to_have_its_private_key(self):
        self.listing.write_text('subject: "CN=vpn.example.test"\nserial: aa:bb\n'
                                'pubkey: RSA 2048 bits\n'
                                'subject: "CN=other.example.test"\nserial: 11\n'
                                'pubkey: RSA 2048 bits, has private key\n')
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assert_old_pair()

    def test_stopped_service_is_not_started(self):
        self.env["SERVICE_ACTIVE"] = "no"
        self.assertEqual(self.deploy().returncode, 0)
        self.assertEqual(self.cert.read_bytes(), (self.live / "fullchain.pem").read_bytes())
        self.assertNotIn("ipsec", self.trace.read_text())
        self.assertNotIn("systemctl start", self.trace.read_text())

    def test_failed_second_file_replacement_restores_both_files(self):
        self.mock("mv", '''if [[ "${@: -1}" == "$FAIL_TARGET" ]]; then exit 1; fi
exec /usr/bin/mv "$@"
''')
        self.env["FAIL_TARGET"] = str(self.key)
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assert_old_pair()

    def renewal(self, key_name):
        self.set_source(key_name)
        state = self.root / "state.env"
        state.write_text(f'CERT_MODE=public\nACME_DOMAIN={DOMAIN}\n')
        body = VALIDATE + RENEW.replace("/etc/letsencrypt/live/", f"{self.live_root}/") + '''
STATE_FILE="$1"
ACME_CERT_PATH="$2"
ACME_KEY_PATH="$3"
ACME_DEPLOY_HOOK="$4"
initialize_certbot_state_defaults() { :; }
command_exists() { return 0; }
log() { :; }
die() { printf '%s\\n' "$*" >&2; exit 1; }
show_certificate_status() { :; }
install_acme_deploy_hook() {
  printf 'hook refreshed\\n' >> "$TRACE"
  cat > "$ACME_DEPLOY_HOOK" <<'HOOK'
#!/usr/bin/env bash
printf 'current certificate deployed\\n' >> "$TRACE"
HOOK
  chmod 700 "$ACME_DEPLOY_HOOK"
}
certbot() { printf 'certbot %s\\n' "$*" >> "$TRACE"; }
renew_acme_certificate
'''
        return self.bash(body, state, self.cert, self.key, self.hook)

    def test_existing_ecdsa_requests_immediate_rsa_migration(self):
        result = self.renewal("ec")
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = self.trace.read_text()
        self.assertIn("--key-type rsa --rsa-key-size 3072", trace)
        self.assertIn("--force-renewal", trace)
        self.assertLess(trace.index("hook refreshed"), trace.index("certbot renew"))

    def test_existing_rsa_does_not_force_renewal(self):
        result = self.renewal("old")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--force-renewal", self.trace.read_text())
        self.assertIn("current certificate deployed", self.trace.read_text())

    def test_fresh_acme_request_explicitly_uses_rsa(self):
        # Remap every /etc path before exercising provisioning in the sandbox.
        body = VALIDATE + CREATE.replace("/etc/", f"{self.root}/etc/") + '''
CERT_MODE=public
CERTIFICATE_MODE=public-trust
SERVER_ID="$1"
CLIENT_DIR="$2/client"
SERVER_CERT="$2/etc/ipsec.d/certs/server.pem"
SERVER_KEY="$2/etc/ipsec.d/private/server.key"
CERTBOT_RELOAD_HOOK="$2/shim"
ACME_DEPLOY_HOOK="$2/deploy-hook"
log() { :; }
die() { exit 1; }
validate_fqdn() { return 0; }
check_acme_dns_reachability() { :; }
certbot_lineage_exists() { return 1; }
check_certbot_standalone_port() { :; }
write_state() { :; }
install_acme_deploy_hook() { :; }
certbot() { printf 'certbot %s\\n' "$*" >> "$TRACE"; }
create_certificates
'''
        live = self.root / "etc/letsencrypt/live" / DOMAIN
        live.mkdir(parents=True)
        shutil.copyfile(self.fixture_root / "new.pem", live / "fullchain.pem")
        shutil.copyfile(self.fixture_root / "new.key", live / "privkey.pem")
        result = self.bash(body, DOMAIN, self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--key-type rsa --rsa-key-size 3072", self.trace.read_text())


if __name__ == "__main__":
    unittest.main()
