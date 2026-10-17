import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from webui.broker import AuditLogger, BrokerClient, BrokerServer, ServiceController
from webui.sessions import SessionReader, parse_sessions


STATUS = """Security Associations (2 up, 1 connecting):
 ikev2-eap[7]: ESTABLISHED 2 minutes ago, 203.0.113.1[vpn.example]...198.51.100.2[alice]
 ikev2-eap[7]: IKEv2 SPIs: 123_i 456_r*, reauthentication in 2 hours
 ikev2-eap[7]: IKE proposal: AES_CBC_256/HMAC_SHA2_256_128
 ikev2-eap{9}: INSTALLED, TUNNEL, reqid 1, ESP in UDP SPIs: 123_i 456_o
 ikev2-eap{9}: AES_CBC_256/HMAC_SHA2_256_128, 1024 bytes_i (3 pkts, 1s ago), 2048 bytes_o (4 pkts, 0s ago), rekeying in 40 minutes
 ikev2-eap{9}: 0.0.0.0/0 ::/0 === 10.10.10.1/32 fd00::1/128
 other[8]: ESTABLISHED 1 hour ago, 203.0.113.1[local]...198.51.100.3[hidden-user]
 other{10}: INSTALLED, TUNNEL, reqid 2
 other{10}: 0.0.0.0/0 === 192.168.0.1/32
 ikev2-eap[11]: CONNECTING, 203.0.113.1[local]...198.51.100.4[unverified]
"""


def runner_for(status=STATUS, state="active", code=0):
    def runner(command, **kwargs):
        if command[0] == "/usr/bin/systemctl":
            return subprocess.CompletedProcess(command, 0 if state == "active" else 3, state, "")
        return subprocess.CompletedProcess(command, code, status, "private daemon failure detail")
    return runner


class SessionTests(unittest.TestCase):
    def test_only_established_managed_sessions_with_dual_stack_and_counters(self):
        sessions = parse_sessions(STATUS)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0], {"id": "ikev2-eap[7]", "username": "alice", "public_ip": "198.51.100.2",
                                      "duration": "2 minutes ago", "vpn_addresses": ["10.10.10.1", "fd00::1"],
                                      "rx_bytes": 1024, "tx_bytes": 2048})

    def test_rekey_children_are_summed_and_uninstalled_children_ignored(self):
        output = STATUS.split(" other[", 1)[0] + """
 ikev2-eap{12}: INSTALLED, TUNNEL, reqid 1
 ikev2-eap{12}: AES_CBC_256, 10 bytes_i, 20 bytes_o, rekeying in 40 minutes
 ikev2-eap{12}: 0.0.0.0/0 === 10.10.10.1/32
 ikev2-eap{13}: REKEYED, TUNNEL, reqid 1
 ikev2-eap{13}: AES_CBC_256, 100000 bytes_i, 200000 bytes_o
"""
        session = parse_sessions(output)[0]
        self.assertEqual((session["rx_bytes"], session["tx_bytes"]), (1034, 2068))
        self.assertEqual(session["vpn_addresses"], ["10.10.10.1", "fd00::1"])

    def test_accounting_snapshot_exposes_individual_children_including_final_rekey_counters(self):
        output = STATUS.split(" other[", 1)[0] + """
 ikev2-eap{12}: REKEYED, TUNNEL, reqid 1
 ikev2-eap{12}: AES_CBC_256, 10 bytes_i, 20 bytes_o
"""
        snapshot = parse_sessions(output, accounting=True)[0]
        self.assertEqual(snapshot["children"], [{"id": "9", "rx_bytes": 1024, "tx_bytes": 2048},
                                                {"id": "12", "rx_bytes": 10, "tx_bytes": 20}])
        self.assertEqual((snapshot["rx_bytes"], snapshot["tx_bytes"]), (1024, 2048))
        self.assertNotIn("children", parse_sessions(output)[0])

    def test_missing_counters_are_unknown_and_invalid_addresses_are_hidden(self):
        output = STATUS.replace("198.51.100.2[alice]", "bad-address[alice]")
        output = output.replace("1024 bytes_i", "unavailable bytes_i")
        session = parse_sessions(output)[0]
        self.assertIsNone(session["rx_bytes"])
        self.assertIsNone(session["tx_bytes"])
        self.assertIsNone(session["public_ip"])
        self.assertEqual(parse_sessions(STATUS.replace("[alice]", "[unsafe identity]")), [])

    def test_authenticated_eap_identity_overrides_remote_ike_identity(self):
        output = STATUS.replace("[alice]", "[generic remote identity]").replace(
            " ikev2-eap[7]: IKEv2", " ikev2-eap[7]: Remote EAP identity: actual-user\n ikev2-eap[7]: IKEv2")
        self.assertEqual(parse_sessions(output)[0]["username"], "actual-user")
        self.assertEqual(parse_sessions(output.replace("identity: actual-user", "identity: bad identity")), [])

    def test_multiple_sessions_for_one_user_remain_distinct(self):
        output = STATUS + STATUS.replace("[7]", "[14]")
        self.assertEqual([item["id"] for item in parse_sessions(output)], ["ikev2-eap[7]", "ikev2-eap[14]"])

    def test_search_and_pagination_are_bounded(self):
        output = "\n".join(STATUS.replace("[7]", f"[{number}]") for number in range(45))
        reader = SessionReader(runner_for(output))
        result = reader.list_sessions("ALICE", 1)
        self.assertEqual((result["total"], result["matched"], result["pages"], len(result["sessions"])), (45, 45, 3, 20))
        self.assertEqual(reader.list_sessions("alice", 99)["page"], 2)
        self.assertEqual(reader.list_sessions("no-match")["sessions"], [])
        for search, page in [("x" * 65, 0), ([], 0), ("", True), ("", -1), ("", 100001)]:
            self.assertEqual(reader.list_sessions(search, page)["status"], 400)

    def test_stopped_is_empty_but_unknown_and_failures_are_unavailable(self):
        for state in ("inactive", "failed"):
            result = SessionReader(runner_for(state=state)).list_sessions()
            self.assertTrue(result["ok"])
            self.assertFalse(result["running"])
            self.assertEqual(result["sessions"], [])
        for runner in (runner_for(state="activating"), runner_for(code=1), runner_for(status="malformed")):
            result = SessionReader(runner).list_sessions()
            self.assertEqual(result["status"], 503)
            self.assertNotIn("private", json.dumps(result))

    def test_timeout_is_safe_and_read_command_uses_fixed_arguments(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 0, "active", "")
            self.assertEqual(kwargs["env"]["LC_ALL"], "C")
            raise subprocess.TimeoutExpired(command, 5)
        self.assertEqual(SessionReader(runner).list_sessions()["status"], 503)
        self.assertEqual(calls[-1], ["/usr/sbin/ipsec", "statusall", "ikev2-eap"])

    def test_broker_returns_full_page_above_old_response_limit_and_rejects_fields(self):
        output = "\n".join(STATUS.replace("[alice]", "[" + "a" * 64 + "]").replace("[7]", f"[{number}]") for number in range(20))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = BrokerServer(root / "broker.sock", os.getuid(), os.getgid(), ServiceController(),
                                  AuditLogger(root / "audit.log"), sessions=SessionReader(runner_for(output)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = BrokerClient(root / "broker.sock")
                result = client.list_sessions()
                self.assertGreater(len(json.dumps(result)), 4096)
                self.assertEqual(len(result["sessions"]), 20)
                result = client.request({"action": "sessions_list", "command": "restart"})
                self.assertEqual(result["status"], 400)
                self.assertFalse((root / "audit.log").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
