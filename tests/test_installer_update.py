"""Server release updates using disposable scripts/state and offline downloads."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / 'ikev2-strongswan-ubuntu.sh').read_text().split('\nmain "$@"\n', 1)[0]
DOWNLOAD = SOURCE[SOURCE.index("\ndownload_release_asset() (") + 1:SOURCE.index("\ninstall_optional_webui() {")]
FUNCTIONS = SOURCE[SOURCE.index('\ninstaller_semver() {') + 1:SOURCE.index('\ngenerate_password() {')]
MAIN = SOURCE[SOURCE.index('\nmain() {') + 1:]


class InstallerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ikev2-update-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'server.sh'
        self.state_dir = self.root / 'state'
        self.state_dir.mkdir()
        self.state = self.state_dir / 'state.env'
        self.state.write_text('INSTALLER_VERSION=6.4.9-en\nCERT_MODE=private\n')
        self.state.chmod(0o600)
        self.original_state = self.state.read_bytes()
        self.release = self.root / 'release.json'
        self.release.write_text(json.dumps({'tag_name': 'server-v6.4.10'}, indent=2))
        self.artifact = self.root / 'artifact.sh'
        self.artifact.write_text('#!/usr/bin/env bash\nCURRENT_INSTALLER_VERSION="6.4.10-en"\n'
                                 'printf "NEW-MENU %s\\n" "$CURRENT_INSTALLER_VERSION"\n')
        self.sums = self.root / 'sums'
        self.checksum()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.trace = self.root / 'trace'
        curl = self.bin / 'curl'
        curl.write_text('''#!/usr/bin/env bash
set -Eeuo pipefail
url="${!#}"
printf '%s\n' "$url" >> "$TRACE"
if [[ "$url" == */releases/latest ]]; then cat "$FIXTURE_RELEASE"; exit 0; fi
[[ "${DOWNLOAD_FAIL:-no}" != yes ]] || exit 22
if [[ "${FAIL_DIRECT:-no}" == yes && "$url" == https://github.com/* ]]; then exit 22; fi
while [[ $# -gt 0 ]]; do
  if [[ "$1" == -o ]]; then output="$2"; shift; fi
  shift
done
case "$url" in
  */releases/tags/server-v6.4.10) cp "$FIXTURE_RELEASE" "$output" ;;
  */releases/assets/201) cp "$FIXTURE_ARTIFACT" "$output" ;;
  */releases/assets/202) cp "$FIXTURE_SUMS" "$output" ;;
  */SHA256SUMS) cp "$FIXTURE_SUMS" "$output" ;;
  *.sh) cp "$FIXTURE_ARTIFACT" "$output" ;;
  *) exit 99 ;;
esac
''')
        curl.chmod(0o755)
        move = self.bin / 'mv'
        move.write_text('''#!/usr/bin/env bash
if [[ "${FAIL_STATE:-no}" == yes && "${!#}" == "$FIXTURE_STATE" ]]; then exit 1; fi
exec /usr/bin/mv "$@"
''')
        move.chmod(0o755)
        self.env = {**os.environ, 'PATH': f"{self.bin}:{os.environ['PATH']}",
                    'TRACE': str(self.trace), 'FIXTURE_RELEASE': str(self.release),
                    'FIXTURE_ARTIFACT': str(self.artifact), 'FIXTURE_SUMS': str(self.sums),
                    'FIXTURE_STATE': str(self.state), 'FIXTURE_ROOT': str(self.root)}

    def checksum(self):
        digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.sums = self.root / 'sums'
        self.sums.write_text(f'{digest}  ./ikev2-strongswan-ubuntu-v6.4.10.sh\n')
        release = json.loads(self.release.read_text())
        release['assets'] = [
            {'id': 201, 'name': 'ikev2-strongswan-ubuntu-v6.4.10.sh', 'digest': 'sha256:' + digest},
            {'id': 202, 'name': 'SHA256SUMS',
             'digest': 'sha256:' + hashlib.sha256(self.sums.read_bytes()).hexdigest()},
        ]
        self.release.write_text(json.dumps(release, indent=2))

    def run_update(self, relaunch=False, dispatch=False):
        prelude = '''set -Eeuo pipefail
CURRENT_INSTALLER_VERSION="6.4.9-en"
GITHUB_REPO=example/project
GITHUB_RELEASE_TAG_PREFIX=server-v
GITHUB_RELEASE_API=https://api.github.com/repos/example/project/releases/latest
STATE_DIR="$FIXTURE_ROOT/state"
BACKUP_DIR="$STATE_DIR/backups"
STATE_FILE="$FIXTURE_STATE"
BOLD='' RESET=''
require_root() { :; }
initialize_logging() { :; }
check_ubuntu() { :; }
configure_connection_logging() { printf 'Unexpected VPN mutation' >&2; exit 99; }
command_exists() { [[ "$1" == curl ]]; }
log() { printf '%s\\n' "$*"; }
info() { printf '%s\\n' "$*"; }
warn() { printf '%s\\n' "$*" >&2; }
die() { printf '%s\\n' "$*" >&2; exit 1; }
ask_yes_no() { [[ "${CONFIRM:-yes}" == yes ]]; }
'''
        invocation = 'main update' if dispatch else ('update_installer --relaunch' if relaunch else 'update_installer')
        self.original_script = prelude + DOWNLOAD + FUNCTIONS + (MAIN if dispatch else '') + '\n' + invocation + '\nprintf "OLD-TAIL\\n"\n'
        self.script.write_text(self.original_script)
        return subprocess.run(['bash', str(self.script)], env=self.env,
                              text=True, capture_output=True, timeout=15)

    def assert_unchanged(self):
        self.assertEqual(self.script.read_text(), self.original_script)
        self.assertEqual(self.state.read_bytes(), self.original_state)
        self.assert_cleanup()

    def assert_cleanup(self):
        self.assertEqual(list(self.state_dir.glob('update.*')), [])
        self.assertEqual(list(self.root.glob('server.sh.update.*')), [])

    def test_script_ahead_of_release_is_reported_without_downgrade(self):
        self.release.write_text(json.dumps({'tag_name': 'server-v6.4.8'}, indent=2))
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('newer than the latest published release', result.stderr)
        self.assertIn('No downgrade was performed', result.stderr)
        self.assertNotIn('already up to date', result.stdout)
        self.assertEqual(len(self.trace.read_text().splitlines()), 1)
        self.assert_unchanged()

    def test_equal_release_explains_main_branch_changes_are_separate(self):
        self.release.write_text(json.dumps({'tag_name': 'server-v6.4.9'}, indent=2))
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('matches the latest published release', result.stdout)
        self.assertIn('Changes pushed only to main', result.stdout)
        self.assert_unchanged()

    def test_verified_update_records_actual_version_and_preserves_state(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.script.read_bytes(), self.artifact.read_bytes())
        self.assertEqual(self.state.read_text(), 'INSTALLER_VERSION=6.4.10-en\nCERT_MODE=private\n')
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        backups = list((self.state_dir / 'backups').glob('installer-*.sh'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original_script)
        self.assertIn('Run the installer again', result.stdout)
        self.assert_cleanup()

    def test_updater_uses_api_fallback_and_keeps_version_and_checksum_checks(self):
        self.env['FAIL_DIRECT'] = 'yes'
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.script.read_bytes(), self.artifact.read_bytes())
        self.assertIn('releases/assets/201', self.trace.read_text())
        self.assertIn('releases/assets/202', self.trace.read_text())
        self.assert_cleanup()

    def test_update_before_vpn_install_does_not_require_managed_state(self):
        self.state.unlink()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.script.read_bytes(), self.artifact.read_bytes())
        self.assert_cleanup()

    def test_checksum_mismatch_never_replaces_script(self):
        self.sums.write_text('0' * 64 + '  ikev2-strongswan-ubuntu-v6.4.10.sh\n')
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SHA256 verification failed', result.stderr)
        self.assert_unchanged()

    def test_embedded_version_must_match_release(self):
        self.artifact.write_text(self.artifact.read_text().replace('6.4.10-en', '6.4.8-en'))
        self.checksum()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('version does not match', result.stderr)
        self.assert_unchanged()

    def test_bad_syntax_is_rejected(self):
        self.artifact.write_text('if then\n')
        self.checksum()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('syntax validation', result.stderr)
        self.assert_unchanged()

    def test_failed_download_removes_temporary_files(self):
        self.env['DOWNLOAD_FAIL'] = 'yes'
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assert_unchanged()

    def test_missing_state_version_prevents_replacement(self):
        self.state.write_text('CERT_MODE=private\n')
        self.original_state = self.state.read_bytes()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no installer version', result.stderr)
        self.assert_unchanged()

    def test_state_write_failure_restores_previous_script(self):
        self.env['FAIL_STATE'] = 'yes'
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('previous installer was restored', result.stderr)
        self.assert_unchanged()

    def test_menu_update_executes_new_script_instead_of_old_menu(self):
        result = self.run_update(relaunch=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('NEW-MENU 6.4.10-en', result.stdout)
        self.assertNotIn('OLD-TAIL', result.stdout)
        self.assert_cleanup()

    def test_direct_update_skips_vpn_logging_reconfiguration(self):
        result = self.run_update(dispatch=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Unexpected VPN mutation', result.stderr)
        self.assert_cleanup()

    def test_cancel_does_not_download_or_create_backups(self):
        self.env['CONFIRM'] = 'no'
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state_dir / 'backups').exists())
        self.assert_unchanged()


if __name__ == '__main__':
    unittest.main()
