import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cdn_proxy", REPO / "packaging/render-cdn-proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
CADDY = os.environ.get("CADDY_BINARY") or shutil.which("caddy")


# Public ranges below are validation fixtures, not a deployment allowlist.
class OriginSettingsTests(unittest.TestCase):
    def test_rejects_invalid_settings_and_network_lists(self):
        settings = {"hostname": "panel.example.com", "port": 8080}
        for networks in ("", "0.0.0.0/0", "127.0.0.1/32", "10.0.0.0/8", "8.8.8.1/24", "<html>error</html>"):
            with self.subTest(networks=networks), self.assertRaises(ValueError):
                proxy.render(settings, networks)
        for port in (80, 8787, 65536, True, "8080"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                proxy.render(dict(settings, port=port), "8.8.8.0/24")
        for hostname in ("p.example.com\n}", "example.com/path", "-bad.example.com"):
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                proxy.render(dict(settings, hostname=hostname), "8.8.8.0/24")


class CdnInstallerInputTests(unittest.TestCase):
    """Only temporary configs; stop before APT, users, services or system writes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ikev2-cdn-input-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "config"
        self.config.mkdir(mode=0o755)
        self.config.chmod(0o755)
        origin = self.config / "origin.json"
        origin.write_text('{"hostname": "panel.example.com", "port": 8080}')
        origin.chmod(0o644)
        source = (REPO / "packaging/install-cdn-proxy.sh").read_text()
        source = source.replace('PROXY_DIR=/etc/ikev2-web-proxy', f'PROXY_DIR="{self.config}"')
        source = source.replace('[[ ${EUID} -eq 0 ]]', '[[ 0 -eq 0 ]]')
        # CI may be unprivileged. Check fixture ownership against the test user.
        source = source.replace("'0:755'", f"'{os.getuid()}:755'")
        source = source.replace("'0:644'", f"'{os.getuid()}:644'")
        source = source.replace("'0:600'", f"'{os.getuid()}:600'")
        self.script = self.root / "install.sh"
        self.script.write_text(source)
        shutil.copyfile(REPO / "packaging/render-cdn-proxy.py", self.root / "render-cdn-proxy.py")
        shutil.copyfile(REPO / "packaging/cdn-networks.py", self.root / "cdn-networks.py")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "trace"
        for name, body in {
            "apt-get": 'printf "apt reached\\n" >> "$TRACE"; exit 79',
            "systemctl": 'exit 1',
            "ss": 'exit 0',
            "curl": 'printf "unexpected network request\\n" >> "$TRACE"; exit 99',
        }.items():
            binary = self.bin / name
            binary.write_text("#!/usr/bin/env bash\n" + body + "\n")
            binary.chmod(0o755)

    def install(self):
        return subprocess.run(["bash", str(self.script)], text=True, capture_output=True,
                              env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                                   "TRACE": str(self.trace)}, timeout=10)

    def network_file(self, text):
        path = self.config / "cdn-ips.txt"
        path.write_text(text)
        path.chmod(0o644)
        return path

    def test_missing_networks_stop_before_any_system_changes(self):
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cdn-ips.txt first", result.stderr)
        self.assertFalse(self.trace.exists())

    def test_invalid_networks_stop_before_any_system_changes(self):
        for networks in ("", "0.0.0.0/0", "10.0.0.0/8", "not-a-network"):
            with self.subTest(networks=networks):
                self.network_file(networks)
                self.assertNotEqual(self.install().returncode, 0)
                self.assertFalse(self.trace.exists())

    def test_network_symlink_is_rejected(self):
        path = self.network_file("8.8.8.0/24")
        target = self.config / "saved-ranges"
        path.rename(target)
        path.symlink_to(target)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.trace.exists())

    def test_valid_custom_networks_reach_stubbed_apt_without_vendor_download(self):
        self.network_file("8.8.8.0/24")
        result = self.install()
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertEqual(self.trace.read_text(), "apt reached\n")


    def test_complete_install_enables_auto_timer_or_disables_it_for_manual_mode(self):
        # Run the installer end-to-end against temporary application/unit paths;
        # package, identity and service commands are all local test executables.
        source = self.script.read_text()
        library = self.root / "lib"
        units = self.root / "units"
        application = self.root / "application"
        units.mkdir()
        (application / "webui").mkdir(parents=True)
        (application / "webui/__init__.py").touch()
        (application / "webui/manage.py").write_text("# fixture cookie helper; no host configuration access\n")
        source = source.replace('/usr/local/lib/ikev2-web', str(library))
        source = source.replace('/etc/systemd/system', str(units))
        source = source.replace('/etc/ikev2-web-proxy', str(self.config))
        source = source.replace('/opt/ikev2', str(application))
        source = source.replace('-o root -g root', f'-o {os.getuid()} -g {os.getgid()}')
        self.script.write_text(source)
        shutil.copytree(REPO / 'packaging/systemd', self.root / 'systemd')
        stubs = {
            'apt-get': 'if [[ "$1" == download ]]; then touch caddy_fixture.deb; fi',
            'dpkg-deb': '''target="${!#}"
mkdir -p "$target/usr/bin"
printf '#!/usr/bin/env bash\\nexit 0\\n' > "$target/usr/bin/caddy"
chmod 755 "$target/usr/bin/caddy"''',
            'getent': 'exit 0',
            'runuser': 'exit 0',
            'curl': 'exit 0',
            'systemctl': 'printf "systemctl %s\\n" "$*" >> "$TRACE"',
        }
        for name, body in stubs.items():
            binary = self.bin / name
            binary.write_text('#!/usr/bin/env bash\nset -euo pipefail\n' + body + '\n')
            binary.chmod(0o755)
        self.network_file('8.8.8.0/24\n')
        import json
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                settings = {'hostname': 'panel.example.com', 'port': 8080}
                if automatic:
                    settings['network_source'] = {'provider': 'cloudflare'}
                (self.config / 'origin.json').write_text(json.dumps(settings))
                self.trace.write_text('')
                result = self.install()
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = 'enable --now' if automatic else 'disable --now'
                self.assertIn(f'systemctl {expected} ikev2-web-cdn-networks.timer', self.trace.read_text())
                self.assertTrue((library / 'cdn-networks.py').is_file())
                self.assertTrue((units / 'ikev2-web-cdn-networks.service').is_file())
                self.assertTrue((units / 'ikev2-web-cdn-networks.timer').is_file())


@unittest.skipUnless(CADDY, "Install caddy or set CADDY_BINARY for real proxy integration tests")
class CaddyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Backend(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Cache-Control", "public, max-age=300")
                self.end_headers()
                self.wfile.write((self.path + "|" + self.headers.get("X-Forwarded-Proto", "")).encode())

            def log_message(self, *args):
                pass

        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        cls.thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.thread.start()
        cls.directory = tempfile.TemporaryDirectory()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        config = proxy.render({"hostname": "panel.example.com", "port": cls.port}, "8.8.8.0/24")
        config = config.replace("reverse_proxy 127.0.0.1:8787", f"reverse_proxy 127.0.0.1:{cls.backend.server_port}")
        path = Path(cls.directory.name) / "Caddyfile"
        path.write_text(config)
        cls.log = tempfile.TemporaryFile()
        cls.process = subprocess.Popen(
            [CADDY, "run", "--config", str(path), "--adapter", "caddyfile"],
            stdout=cls.log, stderr=cls.log,
            env=dict(os.environ, XDG_CONFIG_HOME=cls.directory.name),
        )
        for _ in range(100):
            if cls.process.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError(cls.log.read().decode())
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("Caddy did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.log.close()
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def request(self, host="panel.example.com", scheme="https", source="127.0.0.1", extra=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3, source_address=(source, 0))
        headers = {"Host": host, "X-Forwarded-Proto": scheme}
        headers.update(extra or {})
        connection.request("GET", "/api/health?probe=1", headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_allowed_peer_proxies_and_overrides_backend_cache_policy(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body, b"/api/health?probe=1|https")
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertNotIn("public", headers["Cache-Control"])

    def test_untrusted_peer_cannot_forge_forwarded_headers(self):
        status, _, body = self.request(source="127.0.0.2", extra={"X-Forwarded-For": "8.8.8.1", "X-Real-IP": "8.8.8.1"})
        self.assertEqual(status, 403)
        self.assertNotIn(b"probe", body)

    def test_wrong_hostname_is_rejected(self):
        self.assertEqual(self.request(host="other.example.com")[0], 404)

    def test_http_visitor_redirects_to_canonical_https_without_origin_port(self):
        status, headers, _ = self.request(scheme="http")
        self.assertEqual(status, 308)
        self.assertEqual(headers["Location"], "https://panel.example.com/api/health?probe=1")


if __name__ == "__main__":
    unittest.main()
