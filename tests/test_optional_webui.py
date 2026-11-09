"""Optional panel provisioning with temporary bundles and no privileged commands."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "ikev2-strongswan-ubuntu.sh").read_text().split('\nmain "$@"\n', 1)[0]


def functions(start, following):
    return SOURCE[SOURCE.index(f"\n{start}() ") + 1:SOURCE.index(f"\n{following}() ")]


WRAPPER = functions("install_webui", "on_error")
INSTALL = functions("install_vpn", "safe_purge_new_packages")
MAIN = SOURCE[SOURCE.index("\nmain() {") + 1:]
VALIDATORS = functions("is_ipv4", "ipv4_to_int") + functions("validate_fqdn", "validate_ca_name")


class OptionalWebuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ikev2-optional-panel-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trace = self.root / "trace"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "TRACE": str(self.trace), "TMPDIR": str(self.scratch)}
        self.script = self.root / "server.sh"
        self.prelude = '''set -Eeuo pipefail
GITHUB_REPO=example/project
GITHUB_RELEASE_TAG_PREFIX=server-v
BOLD='' RESET=''
command_exists() { command -v "$1" >/dev/null 2>&1; }
installer_semver() { printf '9.8.7'; }
info() { printf '%s\\n' "$*"; }
warn() { printf '%s\\n' "$*" >&2; }
'''
        self.helper = '''#!/usr/bin/env bash
set -Eeuo pipefail
printf 'panel %s\\n' "$*" >> "$TRACE"
exit "${HELPER_EXIT:-0}"
'''

    def run_script(self, body, data="", *args):
        self.script.write_text(self.prelude + body)
        return subprocess.run(["bash", str(self.script), *args], input=data,
                              env=self.env, text=True, capture_output=True, timeout=10)

    def calls(self):
        return self.trace.read_text() if self.trace.exists() else ""

    def local_helper(self):
        packaging = self.root / "packaging"
        packaging.mkdir()
        (packaging / "install-webui.sh").write_text(self.helper)
        (packaging / "TOOL_VERSION").write_text("9.8.7\n")

    def bundle(self, extra=None, tool="install-webui.sh"):
        archive = self.root / "fixture.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            helper = self.helper.encode()
            member = tarfile.TarInfo("packaging/" + tool)
            member.size = len(helper)
            output.addfile(member, io.BytesIO(helper))
            stamp = b"9.8.7\n"
            member = tarfile.TarInfo("packaging/TOOL_VERSION")
            member.size = len(stamp)
            output.addfile(member, io.BytesIO(stamp))
            if extra:
                member = tarfile.TarInfo(extra)
                member.size = 1
                output.addfile(member, io.BytesIO(b"x"))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        sums = self.root / "fixture.sums"
        sums.write_text(f"{digest}  ./ikev2-webui-v9.8.7.tar.gz\n")
        self.metadata = self.root / "release.json"
        self.write_metadata()
        self.env.update(FIXTURE_BUNDLE=str(archive), FIXTURE_SUMS=str(sums),
                        FIXTURE_METADATA=str(self.metadata))
        curl = self.bin / "curl"
        curl.write_text('''#!/usr/bin/env bash
set -Eeuo pipefail
printf 'curl %s\\n' "${!#}" >> "$TRACE"
[[ "${FAIL_DOWNLOAD:-no}" != yes ]] || exit 22
if [[ "${FAIL_DIRECT:-no}" == yes && "${!#}" == https://github.com/* ]]; then exit 22; fi
while [[ $# -gt 0 ]]; do
  if [[ "$1" == -o ]]; then target="$2"; shift; fi
  url="$1"
  shift
done
case "$url" in
  */releases/tags/server-v9.8.7) cp "$FIXTURE_METADATA" "$target" ;;
  */releases/assets/101) cp "$FIXTURE_BUNDLE" "$target" ;;
  */releases/assets/102) cp "$FIXTURE_SUMS" "$target" ;;
  */ikev2-webui-v9.8.7.tar.gz) cp "$FIXTURE_BUNDLE" "$target" ;;
  */SHA256SUMS) cp "$FIXTURE_SUMS" "$target" ;;
  *) exit 99 ;;
esac
''')
        curl.chmod(0o755)
        return sums

    def write_metadata(self):
        self.metadata.write_text(json.dumps({"tag_name": "server-v9.8.7", "draft": False,
            "prerelease": False, "assets": [
                {"id": 101, "name": "ikev2-webui-v9.8.7.tar.gz", "state": "uploaded",
                 "digest": "sha256:" + hashlib.sha256((self.root / "fixture.tar.gz").read_bytes()).hexdigest()},
                {"id": 102, "name": "SHA256SUMS", "state": "uploaded",
                 "digest": "sha256:" + hashlib.sha256((self.root / "fixture.sums").read_bytes()).hexdigest()},
            ]}))

    def test_gateway_failure_uses_public_api_for_bundle_and_checksums(self):
        self.bundle()
        self.env["FAIL_DIRECT"] = "yes"
        result = self.run_script(WRAPPER + '\ninstall_webui\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("trying the public asset API", result.stderr)
        self.assertIn("releases/assets/101", self.calls())
        self.assertIn("releases/assets/102", self.calls())
        self.assertIn("panel install", self.calls())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_api_fallback_keeps_password_stdin_available(self):
        self.helper = self.helper.replace('exit "${HELPER_EXIT:-0}"',
                                          'cat > "$TRACE.stdin"\nexit "${HELPER_EXIT:-0}"')
        self.bundle()
        self.env["FAIL_DIRECT"] = "yes"
        result = self.run_script(WRAPPER + '\ninstall_webui --password-stdin\n', "disposable-password")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(str(self.trace) + ".stdin").read_text(), "disposable-password")
        self.assertNotIn("disposable-password", result.stdout + result.stderr + self.calls())

    def test_api_fallback_rejects_wrong_release_or_asset(self):
        self.bundle()
        self.env["FAIL_DIRECT"] = "yes"
        for updates in ({"tag_name": "server-v0.0.1"}, {"draft": True},
                        {"prerelease": True}, {"assets": []},
                        {"assets": [{"id": "101", "name": "ikev2-webui-v9.8.7.tar.gz"}]}):
            with self.subTest(updates=updates):
                self.write_metadata()
                metadata = json.loads(self.metadata.read_text())
                metadata.update(updates)
                self.metadata.write_text(json.dumps(metadata))
                result = self.run_script(WRAPPER + '\ninstall_webui\n')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("panel install", self.calls())
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_api_fallback_rejects_digest_mismatch(self):
        self.bundle()
        self.env["FAIL_DIRECT"] = "yes"
        metadata = json.loads(self.metadata.read_text())
        metadata["assets"][0]["digest"] = "sha256:" + "0" * 64
        self.metadata.write_text(json.dumps(metadata))
        result = self.run_script(WRAPPER + '\ninstall_webui\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest verification failed", result.stderr)
        self.assertNotIn("panel install", self.calls())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_api_digest_does_not_replace_mandatory_release_checksum(self):
        sums = self.bundle()
        sums.write_text("0" * 64 + "  ikev2-webui-v9.8.7.tar.gz\n")
        self.write_metadata()  # API digests match both downloaded files.
        self.env["FAIL_DIRECT"] = "yes"
        result = self.run_script(WRAPPER + '\ninstall_webui\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum verification failed", result.stderr)
        self.assertNotIn("panel install", self.calls())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_checkout_install_passes_options_without_download(self):
        self.local_helper()
        result = self.run_script(WRAPPER + '\ninstall_webui --secure-cookie --password-stdin\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "panel install --secure-cookie --password-stdin\n")

    def test_standalone_downloads_matching_release_and_cleans_up(self):
        self.bundle()
        result = self.run_script(WRAPPER + '\ninstall_webui\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("releases/download/server-v9.8.7/ikev2-webui-v9.8.7.tar.gz", self.calls())
        self.assertIn("panel install", self.calls())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_tampered_bundle_never_executes(self):
        sums = self.bundle()
        sums.write_text("0" * 64 + "  ikev2-webui-v9.8.7.tar.gz\n")
        result = self.run_script(WRAPPER + '\ninstall_webui\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum verification failed", result.stderr)
        self.assertNotIn("panel install", self.calls())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_valid_checksum_with_unsafe_archive_is_rejected(self):
        self.bundle("packaging/../../escaped")
        result = self.run_script(WRAPPER + '\ninstall_webui\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsafe Web UI bundle member", result.stderr)
        self.assertNotIn("panel install", self.calls())
        self.assertFalse((self.root / "escaped").exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_unpublished_bundle_reports_checkout_fallback(self):
        self.bundle()
        self.env["FAIL_DOWNLOAD"] = "yes"
        result = self.run_script(WRAPPER + '\ninstall_webui\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full repository checkout", result.stderr)
        self.assertNotIn("panel install", self.calls())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_download_keeps_password_stdin_available_to_installer(self):
        self.helper = self.helper.replace('exit "${HELPER_EXIT:-0}"',
                                          'cat > "$TRACE.stdin"\nexit "${HELPER_EXIT:-0}"')
        self.bundle()
        result = self.run_script(WRAPPER + '\ninstall_webui --password-stdin\n',
                                 "disposable-panel-password")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(str(self.trace) + ".stdin").read_text(),
                         "disposable-panel-password")
        self.assertNotIn("disposable-panel-password", result.stdout + result.stderr + self.calls())

    def test_existing_server_menu_installs_panel_and_returns_on_failure(self):
        state = self.root / "state"
        state.touch()
        menu = functions("interactive_menu", "usage")
        for helper_exit in ("0", "23"):
            with self.subTest(helper_exit=helper_exit):
                self.env["HELPER_EXIT"] = helper_exit
                result = self.run_script(menu + '''
STATE_FILE="$TMPDIR/../state"
install_webui() { printf 'panel install\\n' >> "$TRACE"; return "$HELPER_EXIT"; }
pause_main_menu() { read -r _; }
interactive_menu
''', "14\n\n16\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("14) Install / Update Web UI", result.stdout)
                self.assertIn("Exiting", result.stdout)
                if helper_exit != "0":
                    self.assertIn("did not complete", result.stderr)
        self.assertEqual(self.calls(), "panel install\npanel install\n")

    def test_direct_command_preserves_helper_failure(self):
        self.local_helper()
        self.env["HELPER_EXIT"] = "23"
        result = self.run_script(WRAPPER + '\ninstall_webui\n')
        self.assertEqual(result.returncode, 23)

    def test_main_webui_dispatch_does_not_reconfigure_vpn(self):
        self.local_helper()
        result = self.run_script(WRAPPER + MAIN + '''
require_root() { :; }
initialize_logging() { :; }
check_ubuntu() { :; }
configure_connection_logging() { exit 99; }
main webui --secure-cookie
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "panel install --secure-cookie\n")

    def test_stale_local_tools_are_skipped_for_the_matching_release(self):
        self.local_helper()
        (self.root / "packaging/TOOL_VERSION").write_text("9.8.6\n")
        self.bundle(tool="setup-cdn-proxy.sh")
        (self.root / "packaging/setup-cdn-proxy.sh").write_text('exit 99\n')
        wrapper = WRAPPER.replace('/opt/ikev2/packaging/', str(self.root / "absent") + '/')
        result = self.run_script(wrapper + '\nconfigure_cdn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("releases/download/", self.calls())

    def test_main_cdn_dispatch_uses_checkout_without_vpn_reconfiguration(self):
        self.local_helper()
        (self.root / "packaging/setup-cdn-proxy.sh").write_text(self.helper)
        result = self.run_script(WRAPPER + MAIN + '''
require_root() { :; }
initialize_logging() { :; }
check_ubuntu() { :; }
configure_connection_logging() { exit 99; }
main cdn
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "panel \n")

    def test_cdn_standalone_uses_verified_bundle(self):
        self.bundle(tool="setup-cdn-proxy.sh")
        wrapper = WRAPPER.replace('/opt/ikev2/packaging/', str(self.root / "absent") + '/')
        result = self.run_script(wrapper + '\nconfigure_cdn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("server-v9.8.7/ikev2-webui-v9.8.7.tar.gz", self.calls())
        self.assertIn("panel \n", self.calls())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_cdn_old_bundle_reports_missing_feature_without_running_other_tools(self):
        self.bundle()
        wrapper = WRAPPER.replace('/opt/ikev2/packaging/', str(self.root / "absent") + '/')
        result = self.run_script(wrapper + '\nconfigure_cdn\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not include the requested tool", result.stderr)
        self.assertNotIn("panel \n", self.calls())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_cdn_uses_retained_installed_helper_without_download(self):
        installed = self.root / "installed"
        (installed / "packaging").mkdir(parents=True)
        (installed / "packaging/setup-cdn-proxy.sh").write_text(self.helper)
        (installed / "packaging/TOOL_VERSION").write_text("9.8.7\n")
        wrapper = WRAPPER.replace('/opt/ikev2', str(installed))
        result = self.run_script(wrapper + '\nconfigure_cdn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "panel \n")

    def test_existing_server_menu_runs_cdn_and_recovers_from_failure(self):
        (self.root / "state").touch()
        self.env["HELPER_EXIT"] = "23"
        result = self.run_script(functions("interactive_menu", "usage") + '''
STATE_FILE="$TMPDIR/../state"
configure_cdn() { printf 'cdn setup\\n' >> "$TRACE"; return "$HELPER_EXIT"; }
pause_main_menu() { read -r _; }
interactive_menu
''', "15\n\n16\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("15) Configure Web UI for CDN", result.stdout)
        self.assertIn("CDN setup did not complete", result.stderr)
        self.assertEqual(self.calls(), "cdn setup\n")

    def fresh_install(self, panel="no", proceed="yes", helper_exit="0", cdn="no", cdn_exit="0"):
        self.env.update(PANEL_CHOICE=panel, PROCEED_CHOICE=proceed, HELPER_EXIT=helper_exit,
                        CDN_CHOICE=cdn, CDN_EXIT=cdn_exit)
        # Only the prompt/orchestration runs. Every privileged action is a stub.
        return self.run_script(WRAPPER + INSTALL + VALIDATORS + '''
STATE_FILE="$TMPDIR/nonexistent-state"
IPSEC_CONF="$TMPDIR/nonexistent-conf"
IPSEC_SECRETS="$TMPDIR/nonexistent-secrets"
FW_SERVICE=test-firewall
CLIENT_DIR="$TMPDIR/client"
default_interface() { printf eth0; }
interface_ipv4() { printf 192.0.2.1; }
ask_value() {
  case "$1" in
    SERVER_ID) printf -v "$1" '%s' vpn.example.test ;;
    panel_value) printf -v "$1" '%s' panel.example.test ;;
    *) printf -v "$1" '%s' "$4" ;;
  esac
}
validate_server_id() { :; }
validate_interface() { :; }
validate_cidr() { :; }
cidr_is_private() { :; }
route_overlap() { :; }
validate_dns_list() { :; }
normalize_dns_list() { printf '%s' "$1"; }
validate_ca_name() { :; }
initialize_proxy_state_defaults() { :; }
package_installed() { return 1; }
ask_yes_no() {
  case "$1" in
    'Install the Web UI management panel too?') [[ "$PANEL_CHOICE" == yes ]] ;;
    'Configure the Web UI with a separate CDN hostname after installation?') [[ "$CDN_CHOICE" == yes ]] ;;
    'Proceed with installation?') [[ "$PROCEED_CHOICE" == yes ]] ;;
    *) return 1 ;;
  esac
}
collect_users() { USER_NAMES=(fixture); USER_PASSWORDS=(fixture-password); }
record_preinstall_state() { printf 'vpn-provision\\n' >> "$TRACE"; }
service_is_active() { return 1; }
install_packages() { :; }
create_certificates() { :; }
write_ipsec_config() { :; }
write_sysctl_config() { :; }
write_firewall_config() { :; }
write_client_files() { :; }
start_and_verify() { printf 'vpn-verified\\n' >> "$TRACE"; }
configure_connection_logging() { :; }
show_install_result() { printf 'vpn-ready\\n' >> "$TRACE"; }
install_webui() { printf 'panel install\\n' >> "$TRACE"; return "$HELPER_EXIT"; }
configure_cdn() { printf 'cdn %s (vpn=%s)\\n' "$*" "$SERVER_ID" >> "$TRACE"; return "$CDN_EXIT"; }
install_vpn
''', "1\n")

    def test_initial_install_skip_does_not_install_panel(self):
        result = self.fresh_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("vpn-ready", self.calls())
        self.assertNotIn("panel install", self.calls())
        self.assertRegex(result.stdout, r"Install Web UI\s+: no")

    def test_initial_install_accept_runs_panel_after_vpn_verification(self):
        result = self.fresh_install(panel="yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.calls().endswith("vpn-verified\nvpn-ready\npanel install\n"))
        self.assertRegex(result.stdout, r"Install Web UI\s+: yes")
        self.assertRegex(result.stdout, r"Web UI access\s+: SSH tunnel")

    def test_initial_install_separately_collects_panel_hostname_and_runs_cdn_last(self):
        result = self.fresh_install(panel="yes", cdn="yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"VPN address / ID\s+: vpn.example.test")
        self.assertRegex(result.stdout, r"Web UI / CDN URL\s+: https://panel.example.test")
        self.assertTrue(self.calls().endswith(
            "vpn-verified\nvpn-ready\npanel install\ncdn --hostname panel.example.test (vpn=vpn.example.test)\n"))

    def test_cdn_is_not_configured_when_panel_installation_fails(self):
        result = self.fresh_install(panel="yes", cdn="yes", helper_exit="23")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VPN installation is complete", result.stderr)
        self.assertNotIn("cdn --hostname", self.calls())

    def test_optional_cdn_failure_preserves_successful_vpn_and_panel(self):
        result = self.fresh_install(panel="yes", cdn="yes", cdn_exit="23")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("panel install\ncdn --hostname", self.calls())
        self.assertIn("Optional CDN setup did not complete", result.stderr)
        self.assertIn("VPN installation is complete", result.stderr)

    def test_panel_hostname_prompt_rejects_vpn_name_ip_url_and_invalid_labels(self):
        body = (WRAPPER + VALIDATORS + functions("ask_value", "validate_email") + '''
SERVER_ID=vpn.example.test
panel_hostname=''
collect_panel_hostname panel_hostname
printf 'selected=%s vpn=%s\\n' "$panel_hostname" "$SERVER_ID"
''')
        result = self.run_script(body,
            'VPN.EXAMPLE.TEST\n192.0.2.1\nhttps://panel.example.test\nbad..example.test\nPANEL.EXAMPLE.TEST\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("must differ", result.stderr)
        self.assertIn("fully qualified panel DNS hostname", result.stderr)
        self.assertIn("selected=panel.example.test vpn=vpn.example.test", result.stdout)

    def test_cancelled_initial_install_installs_neither_component(self):
        result = self.fresh_install(panel="yes", cdn="yes", proceed="no")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "")

    def test_optional_failure_leaves_successful_vpn_and_reports_retry(self):
        result = self.fresh_install(panel="yes", helper_exit="23")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("vpn-ready\npanel install", self.calls())
        self.assertIn("VPN installation is complete", result.stderr)
        self.assertIn("webui", result.stderr)


if __name__ == "__main__":
    unittest.main()
