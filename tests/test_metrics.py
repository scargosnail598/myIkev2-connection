import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from webui.broker import AuditLogger, BrokerClient, BrokerServer, ServiceController
from webui.metrics import MetricsReader


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "net").mkdir()
        self.state = self.root / "state.env"
        self.state.write_text("OUT_IF=ens3\nPASSWORD=never-return-this\n")
        self.now = 100.0
        self.reader = MetricsReader(proc=self.root, state=self.state, monotonic=lambda: self.now, clock=lambda: 1000 + self.now)
        self.cpu()
        self.memory()
        self.network()
        (self.root / "net/route").write_text("Iface Destination Gateway Flags RefCnt Use Metric Mask\neth0 00000000 0100000A 0003 0 0 100 00000000\nens3 00000000 0100000A 0003 0 0 50 00000000\n")

    def cpu(self, user=100, system=100, idle=800, guest=50, iowait=0):
        (self.root / "stat").write_text(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 {guest} 0\ncpu0 0 0 0 0 0 0 0 0 0 0\ncpu1 0 0 0 0 0 0 0 0 0 0\n")

    def memory(self, total=4096, available=3072):
        (self.root / "meminfo").write_text(f"MemTotal: {total} kB\nMemAvailable: {available} kB\nMemFree: 10 kB\nCached: 2000 kB\n")

    def network(self, interface="ens3", rx=1000, tx=2000):
        values = [rx, 0, 0, 0, 0, 0, 0, 0, tx, 0, 0, 0, 0, 0, 0, 0]
        (self.root / "net/dev").write_text("Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n" + interface + ": " + " ".join(map(str, values)) + "\n")

    def test_first_sample_reports_memory_and_totals_but_does_not_invent_rates(self):
        result = self.reader.snapshot()
        self.assertIsNone(result["cpu"]["percent"])
        self.assertEqual(result["cpu"]["cores"], 2)
        self.assertEqual(result["memory"], {"total_bytes": 4194304, "used_bytes": 1048576, "percent": 25.0})
        self.assertEqual(result["network"]["interface"], "ens3")
        self.assertEqual(result["network"]["source"], "configured")
        self.assertEqual(result["network"]["rx_bytes"], 1000)
        self.assertIsNone(result["network"]["rx_bytes_per_second"])
        self.assertNotIn("PASSWORD", json.dumps(result))
        self.assertNotIn("never-return-this", json.dumps(result))

    def test_cpu_and_network_rates_use_deltas_and_do_not_double_count_guests(self):
        self.reader.snapshot()
        self.now += 5
        self.cpu(user=130, system=120, idle=850, guest=5000)
        self.network(rx=6120, tx=12240)
        result = self.reader.snapshot()
        self.assertEqual(result["cpu"]["percent"], 50.0)
        self.assertEqual(result["network"]["rx_bytes_per_second"], 1024.0)
        self.assertEqual(result["network"]["tx_bytes_per_second"], 2048.0)
        self.assertEqual([item["age_seconds"] for item in result["history"]], [5, 0])

    def test_waiting_for_io_is_excluded_from_cpu_busy_time(self):
        self.reader.snapshot()
        self.now += 5
        self.cpu(user=110, system=110, idle=850, iowait=30)
        self.assertEqual(self.reader.snapshot()["cpu"]["percent"], 20.0)

    def test_multiple_readers_share_one_sample_and_history_is_bounded(self):
        first = self.reader.snapshot()
        self.now += 1
        self.cpu(user=200)
        self.assertEqual(self.reader.snapshot()["sampled_at"], first["sampled_at"])
        for _ in range(150):
            self.now += 5
            result = self.reader.snapshot()
        self.assertLessEqual(len(result["history"]), 61)
        self.assertTrue(all(0 <= point["age_seconds"] <= 300 for point in result["history"]))
        self.assertLess(len(json.dumps(result).encode()), 65536)

    def test_counter_reset_and_long_pause_are_gaps_not_negative_or_invented_rates(self):
        self.reader.snapshot()
        self.now += 5
        self.cpu(user=1, system=1, idle=1)
        self.network(rx=1, tx=1)
        result = self.reader.snapshot()
        self.assertIsNone(result["cpu"]["percent"])
        self.assertIsNone(result["network"]["rx_bytes_per_second"])
        self.now += 60
        self.cpu(user=100, system=100, idle=800)
        self.network(rx=10000, tx=10000)
        result = self.reader.snapshot()
        self.assertIsNone(result["cpu"]["percent"])
        self.assertIsNone(result["network"]["tx_bytes_per_second"])
        self.assertIsNotNone(result["memory"]["percent"])

    def test_interface_change_restarts_rate_baseline(self):
        self.reader.snapshot()
        self.now += 5
        self.state.write_text("OUT_IF='enp1s0'\n")
        self.network(interface="enp1s0", rx=100000)
        result = self.reader.snapshot()
        self.assertEqual(result["network"]["interface"], "enp1s0")
        self.assertIsNone(result["network"]["rx_bytes_per_second"])
        self.assertEqual(result["history"][0]["interface"], "ens3")

    def test_default_route_fallback_selects_lowest_metric_and_never_sources_state(self):
        self.state.unlink()
        self.assertEqual(self.reader.snapshot()["network"]["source"], "default_route")
        self.assertEqual(self.reader.snapshot()["network"]["interface"], "ens3")
        self.now += 5
        self.state.write_text("OUT_IF=$(touch unexpected)\n")
        result = self.reader.snapshot()
        self.assertIsNone(result["network"]["interface"])
        self.assertFalse((self.root / "unexpected").exists())
        for name in ("../secrets", "lo", "ens3\nOUT_IF=eth0"):
            self.now += 5
            self.state.write_text("OUT_IF=" + name + "\n")
            self.assertIsNone(self.reader.snapshot()["network"]["interface"])

    def test_unicode_state_fields_do_not_hide_the_configured_interface(self):
        self.state.write_text("CA_NAME='گواهی سرور'\nOUT_IF=ens3\n", encoding="utf-8")
        result = self.reader.snapshot()
        self.assertEqual(result["network"]["interface"], "ens3")
        self.assertNotIn("CA_NAME", json.dumps(result))

    def test_missing_and_malformed_sources_do_not_become_zero_usage(self):
        (self.root / "stat").write_text("cpu bad data")
        (self.root / "meminfo").write_text("MemTotal: 4096 kB\nMemFree: 10 kB\n")
        (self.root / "net/dev").unlink()
        result = self.reader.snapshot()
        self.assertEqual(result["cpu"], {"percent": None, "cores": None})
        self.assertIsNone(result["memory"]["used_bytes"])
        self.assertIsNone(result["network"]["rx_bytes"])
        self.now += 5
        self.memory(total=100, available=200)
        self.assertIsNone(self.reader.snapshot()["memory"]["percent"])

    def test_broker_allows_read_only_metrics_and_rejects_arguments_and_wrong_peer(self):
        server = BrokerServer(self.root / "broker.sock", os.getuid(), os.getgid(), ServiceController(),
                              AuditLogger(self.root / "audit.log"), metrics=self.reader)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = BrokerClient(self.root / "broker.sock")
            self.assertEqual(client.request({"action": "metrics", "interface": "eth0"})["status"], 400)
            self.assertTrue(client.metrics()["ok"])
            self.assertFalse((self.root / "audit.log").exists())
            server.allowed_uid = os.getuid() + 10000
            self.assertEqual(client.metrics()["error"], "peer_not_allowed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
