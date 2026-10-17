from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from webui.broker import AuditLogger, BrokerClient, BrokerServer, ServiceController
from webui.diagnostics import DiagnosticsReader


NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)
DATES = "notBefore=Oct  1 00:00:00 2026 GMT\nnotAfter=Jan  1 00:00:00 2027 GMT\n"
UDP = "  sl  local_address rem_address st\n  1: 00000000:01F4 00000000:0000 07\n  2: 00000000:1194 00000000:0000 07\n"


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "state.env"
        self.state.write_text("DO_NOT_EXECUTE=$(touch unexpected)\nVPN_PASSWORD=never-return-this\n")
        self.forwarding = self.root / "ip_forward"
        self.forwarding.write_text("1\n")
        self.udp = self.root / "udp"
        self.udp.write_text(UDP)
        self.states = {}
        self.calls = []
        self.dates = DATES
        self.status = "Connections:\n ikev2-eap: %any...%any IKEv2\nSecurity Associations (0 up, 0 connecting): none"
        self.fail_commands = False
        self.reader = DiagnosticsReader(self.runner, state=self.state, certificate=self.root / "public-cert.pem",
                                        forwarding=self.forwarding, udp_files=(self.udp, self.root / "missing-udp6"),
                                        clock=lambda: NOW)

    def runner(self, command, **kwargs):
        self.calls.append(command)
        self.assertGreater(kwargs["timeout"], 0)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        if self.fail_commands:
            raise subprocess.TimeoutExpired(command, 3, output="never-return-this")
        if command[0] == "/usr/bin/systemctl":
            content = self.states.get(command[-1], "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n")
        elif command[0] == "/usr/bin/openssl":
            content = self.dates
        elif command == ["/usr/sbin/ipsec", "statusall", "ikev2-eap"]:
            content = self.status
        else:
            raise AssertionError(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, content, "never-return-this")

    def checks(self):
        return {item["id"]: item for item in self.reader.snapshot()["checks"]}

    def test_healthy_snapshot_reads_only_fixed_sources_and_hides_raw_output(self):
        before = self.state.read_bytes(), self.forwarding.read_bytes(), self.udp.read_bytes()
        result = self.reader.snapshot()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["checks"]), 8)
        self.assertTrue(all(item["status"] == "pass" for item in result["checks"]))
        self.assertNotIn("never-return-this", json.dumps(result))
        self.assertNotIn("VPN_PASSWORD", json.dumps(result))
        self.assertEqual(before, (self.state.read_bytes(), self.forwarding.read_bytes(), self.udp.read_bytes()))
        self.assertFalse((self.root / "unexpected").exists())
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.calls[-1], ["/usr/bin/openssl", "x509", "-in", str(self.root / "public-cert.pem"), "-noout", "-dates"])

    def test_optional_uninstalled_or_disabled_proxy_does_not_raise_false_warning(self):
        for content in ("LoadState=not-found\nActiveState=inactive\n", "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"):
            self.states["ikev2-vpn-proxy.service"] = content
            self.assertEqual(self.checks()["proxy_service"]["status"], "pass")
        self.states["ikev2-vpn-proxy.service"] = "LoadState=loaded\nActiveState=failed\nUnitFileState=enabled\n"
        self.assertEqual(self.checks()["proxy_service"]["status"], "fail")

    def test_stopped_vpn_reports_unavailable_connection_without_probing_or_starting(self):
        self.states["strongswan-starter.service"] = "LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled\n"
        self.udp.write_text("  sl local_address rem_address st\n")
        checks = self.checks()
        self.assertEqual(checks["vpn_service"]["status"], "warn")
        self.assertEqual(checks["connection"]["status"], "unknown")
        self.assertEqual(checks["udp_ports"]["status"], "warn")
        self.assertFalse(any(command[0] == "/usr/sbin/ipsec" for command in self.calls))

    def test_missing_connection_or_udp_binding_and_disabled_forwarding_are_detected(self):
        self.status = "Security Associations (0 up, 0 connecting): none"
        self.udp.write_text(UDP.split("  2:")[0])
        self.forwarding.write_text("0\n")
        checks = self.checks()
        for identifier in ("connection", "udp_ports", "ipv4_forwarding"):
            self.assertEqual(checks[identifier]["status"], "fail")
            self.assertTrue(checks[identifier]["guidance"])
        self.assertIn("4500", checks["udp_ports"]["detail"])
        self.assertNotIn("500,", checks["udp_ports"]["detail"])

    def test_certificate_expiry_not_before_and_renewal_window(self):
        for dates, expected, text in (
            ("notBefore=Oct  1 00:00:00 2026 GMT\nnotAfter=Oct 16 00:00:00 2026 GMT\n", "fail", "expired"),
            ("notBefore=Oct 18 00:00:00 2026 GMT\nnotAfter=Jan  1 00:00:00 2027 GMT\n", "fail", "not valid yet"),
            ("notBefore=Oct  1 00:00:00 2026 GMT\nnotAfter=Oct 30 00:00:00 2026 GMT\n", "warn", "13 days"),
            ("invalid certificate or secret output", "unknown", "could not be inspected"),
        ):
            self.dates = dates
            item = self.checks()["server_certificate"]
            self.assertEqual(item["status"], expected)
            self.assertIn(text, item["detail"])

    def test_timeouts_and_unreadable_proc_data_are_unknown_without_raw_errors(self):
        self.fail_commands = True
        self.udp.write_text("invalid proc data")
        self.forwarding.unlink()
        result = self.reader.snapshot()
        self.assertNotIn("never-return-this", json.dumps(result))
        for item in result["checks"][1:]:
            self.assertEqual(item["status"], "unknown")

    def test_ipv6_socket_table_can_supply_both_ports_without_ipv4_table(self):
        self.udp.write_text(UDP.replace("00000000:", "0" * 32 + ":"))
        self.assertEqual(self.checks()["udp_ports"]["status"], "pass")
        self.udp.unlink()
        self.assertEqual(self.checks()["udp_ports"]["status"], "unknown")

    def test_real_openssl_public_certificate_dates_are_supported(self):
        certificate, key = self.root / "cert.pem", self.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=diagnostics-fixture",
                        "-keyout", str(key), "-out", str(certificate)], check=True, capture_output=True)
        reader = DiagnosticsReader(certificate=certificate)
        result = reader._certificate()
        self.assertEqual(result["status"], "warn")
        self.assertIn("days remaining", result["detail"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_broker_rejects_arguments_and_untrusted_peers_without_running_checks(self):
        server = BrokerServer(self.root / "broker.sock", os.getuid(), os.getgid(), ServiceController(),
                              AuditLogger(self.root / "audit.log"), diagnostics=self.reader)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = BrokerClient(self.root / "broker.sock")
            self.assertEqual(client.request({"action": "diagnostics", "command": "restart"})["status"], 400)
            self.assertEqual(self.calls, [])
            self.assertTrue(client.diagnostics()["ok"])
            self.assertFalse((self.root / "audit.log").exists())
            self.calls.clear()
            server.allowed_uid = os.getuid() + 10000
            self.assertEqual(client.diagnostics()["error"], "peer_not_allowed")
            self.assertEqual(self.calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
