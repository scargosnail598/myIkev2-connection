"""Interactive CDN setup with temporary configs and a stub installation helper."""

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class CdnSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ikev2-cdn-setup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "proxy"
        self.web_config = self.root / "web-auth.json"
        self.web_config.write_text('{}')
        self.networks = self.root / "provider ranges.txt"
        self.networks.write_text('8.8.8.0/24\n')
        self.trace = self.root / "trace"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in {
            "curl": 'exit "${HEALTH_EXIT:-0}"',
            "systemctl": 'exit 1',
            "ss": '[[ "${PORT_BUSY:-no}" != yes ]] || printf occupied',
        }.items():
            path = self.bin / name
            path.write_text('#!/usr/bin/env bash\n' + body + '\n')
            path.chmod(0o755)
        # All file targets are temporary. Bypass UID only for this fixture and
        # use the test account for fixture file ownership in unprivileged CI.
        source = (REPO / 'packaging/setup-cdn-proxy.sh').read_text()
        source = source.replace('PROXY_DIR=/etc/ikev2-web-proxy', f'PROXY_DIR="{self.config}"')
        source = source.replace('WEB_CONFIG=/etc/ikev2-web/config.json', f'WEB_CONFIG="{self.web_config}"')
        source = source.replace('[[ ${EUID} -eq 0 ]]', '[[ 0 -eq 0 ]]')
        source = source.replace("'0:755'", f"'{os.getuid()}:755'")
        source = source.replace("'0:644'", f"'{os.getuid()}:644'")
        source = source.replace('-o root -g root', f'-o {os.getuid()} -g {os.getgid()}')
        self.script = self.root / 'setup.sh'
        self.script.write_text(source)
        shutil.copyfile(REPO / 'packaging/render-cdn-proxy.py', self.root / 'render-cdn-proxy.py')
        (self.root / 'install-cdn-proxy.sh').write_text('''#!/usr/bin/env bash
printf 'proxy install\n' >> "$TRACE"
exit "${INSTALL_EXIT:-0}"
''')
        self.env = {**os.environ, 'PATH': f"{self.bin}:{os.environ['PATH']}", 'TRACE': str(self.trace)}

    def run_setup(self, data='', *args):
        return subprocess.run(['bash', str(self.script), *args], input=data, text=True,
                              capture_output=True, env=self.env, timeout=10)

    def file_input(self, confirm='y', hostname='panel.example.com', port='8080'):
        return f'{hostname}\n{port}\n1\n{self.networks}\n{confirm}\n'

    def existing(self):
        self.config.mkdir(mode=0o755)
        self.config.chmod(0o755)
        values = {'origin.json': '{"hostname":"old.example.com","port":9090}\n',
                  'cdn-ips.txt': '8.8.8.0/24\n', 'Caddyfile': 'prior proxy configuration\n'}
        for name, value in values.items():
            (self.config / name).write_text(value)
            (self.config / name).chmod(0o644)
        return values

    def test_file_input_creates_config_after_confirmation(self):
        result = self.run_setup(self.file_input())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.config / 'origin.json').read_text()),
                         {'hostname': 'panel.example.com', 'port': 8080})
        self.assertEqual((self.config / 'cdn-ips.txt').read_text(), self.networks.read_text())
        self.assertEqual((self.config / 'origin.json').stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.trace.read_text(), 'proxy install\n')
        self.assertIn('unencrypted', result.stdout)
        self.assertIn('Keep 8787 private', result.stdout)

    def test_pasted_networks_and_default_port(self):
        result = self.run_setup('panel.example.com\n\n2\n8.8.8.0/24\n\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.config / 'origin.json').read_text())['port'], 8080)

    def test_cancel_and_eof_never_write_system_settings(self):
        for confirmation in ('n', ''):
            with self.subTest(confirmation=confirmation):
                result = self.run_setup(self.file_input(confirm=confirmation))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.config.exists())
                self.assertFalse(self.trace.exists())

    def test_invalid_settings_or_networks_never_change_existing_config(self):
        original = self.existing()
        for hostname, port, networks in (
            ('not a hostname', '8080', '8.8.8.0/24'),
            ('panel.example.com', '8787', '8.8.8.0/24'),
            ('panel.example.com', 'invalid', '8.8.8.0/24'),
            ('panel.example.com', '8080', '0.0.0.0/0'),
            ('panel.example.com', '8080', ''),
        ):
            with self.subTest(hostname=hostname, port=port, networks=networks):
                self.networks.write_text(networks)
                result = self.run_setup(self.file_input(hostname=hostname, port=port))
                self.assertNotEqual(result.returncode, 0)
                for name, value in original.items():
                    self.assertEqual((self.config / name).read_text(), value)
                self.assertFalse(self.trace.exists())
                self.assertFalse((self.config / 'backups').exists())

    def test_existing_settings_can_be_kept_and_are_backed_up(self):
        original = self.existing()
        result = self.run_setup('\n\n\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.config / 'origin.json').read_text()),
                         {'hostname': 'old.example.com', 'port': 9090})
        backups = list((self.config / 'backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)
        for name, value in original.items():
            self.assertEqual((backups[0] / name).read_text(), value)

    def test_missing_webui_and_failed_health_stop_before_prompts(self):
        self.web_config.unlink()
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Install the Web UI first', result.stderr)
        self.web_config.touch()
        self.env['HEALTH_EXIT'] = '7'
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local Web UI is unavailable', result.stderr)
        self.assertFalse(self.config.exists())

    def test_occupied_port_is_rejected_before_changes(self):
        self.env['PORT_BUSY'] = 'yes'
        result = self.run_setup(self.file_input())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('occupied', result.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.trace.exists())

    def test_helper_failure_returns_failure_and_retains_backup_for_recovery(self):
        original = self.existing()
        self.env['INSTALL_EXIT'] = '23'
        result = self.run_setup(self.file_input())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('new settings remain saved for retry', result.stderr)
        backup = next((self.config / 'backups').iterdir())
        for name, value in original.items():
            self.assertEqual((backup / name).read_text(), value)

    def test_existing_config_symlink_is_rejected(self):
        self.existing()
        (self.config / 'cdn-ips.txt').unlink()
        (self.config / 'cdn-ips.txt').symlink_to(self.networks)
        result = self.run_setup(self.file_input())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('regular files', result.stderr)
        self.assertFalse(self.trace.exists())

    def test_webui_install_retains_complete_cdn_tools(self):
        source = (REPO / 'packaging/install-webui.sh').read_text()
        start = source.index('\ninstall_source() {') + 1
        end = source.index('\nensure_service_account() {', start)
        body = source[start:end].replace('-o root -g root', f'-o {os.getuid()} -g {os.getgid()}')
        installed = self.root / 'installed'
        body += f'\nSOURCE_DIR={shlex.quote(str(REPO))}\nINSTALL_DIR={shlex.quote(str(installed))}\ninstall_source\n'
        result = subprocess.run(['bash', '-euc', body], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('setup-cdn-proxy.sh', 'install-cdn-proxy.sh', 'render-cdn-proxy.py',
                     'systemd/ikev2-web-proxy.service'):
            self.assertTrue((installed / 'packaging' / name).is_file(), name)
        result = subprocess.run(['bash', str(installed / 'packaging/setup-cdn-proxy.sh'), '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('HTTP CDN origin', result.stdout)

    def test_unknown_arguments_are_rejected(self):
        result = self.run_setup('', '--unknown')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.config.exists())


if __name__ == '__main__':
    unittest.main()
