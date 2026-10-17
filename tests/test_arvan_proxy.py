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
spec = importlib.util.spec_from_file_location("arvan_proxy", REPO / "packaging/render-arvan-proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
CADDY = os.environ.get("CADDY_BINARY") or shutil.which("caddy")


class OriginSettingsTests(unittest.TestCase):
    def test_rejects_invalid_settings_and_network_lists(self):
        settings = {"hostname": "p.mahanfreenet.ir", "port": 8080}
        for networks in ("", "0.0.0.0/0", "127.0.0.1/32", "10.0.0.0/8", "185.143.232.1/22", "<html>error</html>"):
            with self.subTest(networks=networks), self.assertRaises(ValueError):
                proxy.render(settings, networks)
        for port in (80, 8787, 65536, True, "8080"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                proxy.render(dict(settings, port=port), "185.143.232.0/22")
        for hostname in ("p.example.com\n}", "example.com/path", "-bad.example.com"):
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                proxy.render(dict(settings, hostname=hostname), "185.143.232.0/22")


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
        config = proxy.render({"hostname": "p.mahanfreenet.ir", "port": cls.port}, "185.143.232.0/22")
        config = config.replace("reverse_proxy 127.0.0.1:8787", f"reverse_proxy 127.0.0.1:{cls.backend.server_port}")
        path = Path(cls.directory.name) / "Caddyfile"
        path.write_text(config)
        cls.log = tempfile.TemporaryFile()
        cls.process = subprocess.Popen([CADDY, "run", "--config", str(path), "--adapter", "caddyfile"], stdout=cls.log, stderr=cls.log)
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

    def request(self, host="p.mahanfreenet.ir", scheme="https", source="127.0.0.1", extra=None):
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
        status, _, body = self.request(source="127.0.0.2", extra={"X-Forwarded-For": "185.143.232.1", "X-Real-IP": "185.143.232.1"})
        self.assertEqual(status, 403)
        self.assertNotIn(b"probe", body)

    def test_wrong_hostname_is_rejected(self):
        self.assertEqual(self.request(host="other.example.com")[0], 404)

    def test_http_visitor_redirects_to_canonical_https_without_origin_port(self):
        status, headers, _ = self.request(scheme="http")
        self.assertEqual(status, 308)
        self.assertEqual(headers["Location"], "https://p.mahanfreenet.ir/api/health?probe=1")


if __name__ == "__main__":
    unittest.main()
