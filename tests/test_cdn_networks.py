"""Public feed parsing and transactional allowlist refresh, with no real services."""

import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cdn_networks', REPO / 'packaging/cdn-networks.py')
networks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(networks)
V4 = '8.8.8.0/24'
V6 = '2001:4860::/32'


class FeedTests(unittest.TestCase):
    def fetch(self, payload, provider='arvancloud', url=None):
        source = {'provider': provider}
        if url is not None:
            source['url'] = url
        with mock.patch.object(networks, 'build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(payload)
            result = networks.fetch_ranges(source)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url.split(':')[0], 'https')
            self.assertNotIn('Authorization', request.headers)
            return result

    def test_plain_feed_is_canonical_and_deduplicated(self):
        self.assertEqual(self.fetch(f'\n{V4}\n{V4}\n{V6}\n'.encode()), f'{V4}\n{V6}\n')

    def test_cloudflare_fetches_both_address_families(self):
        data = {'success': True, 'result': {'ipv4_cidrs': [V4], 'ipv6_cidrs': [V6]}}
        self.assertEqual(self.fetch(json.dumps(data).encode(), 'cloudflare'), f'{V4}\n{V6}\n')

    def test_invalid_or_incomplete_feeds_are_rejected(self):
        for data in (b'', b'<html>error</html>', b'0.0.0.0/0', b'::/0', b'10.0.0.0/8',
                     b'127.0.0.1/32', b'8.8.8.1/24', b'169.254.169.254/32'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.fetch(data)
        for data in ({'success': False}, {'success': True, 'result': {'ipv4_cidrs': [V4], 'ipv6_cidrs': []}},
                     {'success': True, 'result': {'ipv4_cidrs': [V6], 'ipv6_cidrs': [V6]}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.fetch(json.dumps(data).encode(), 'cloudflare')

    def test_https_custom_feed(self):
        self.assertEqual(self.fetch(V4.encode(), 'custom', 'https://cdn.example.com/ips.txt'), V4 + '\n')

    def test_unsafe_urls_and_redirects_are_rejected(self):
        for url in ('http://cdn.example.com/ips', 'https://u:p@cdn.example.com/ips',
                    'https://cdn.example.com/ips?token=example', 'https://cdn.example.com/#fragment',
                    'https://127.0.0.1/ips', 'https://localhost/ips', 'https://cdn.example.com:8443/ips'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                networks.validate_url(url)
        with self.assertRaises(ValueError):
            networks.HttpsRedirects().redirect_request(None, None, 302, '', {}, 'http://cdn.example.com/ips')

    def test_size_limit_and_download_errors(self):
        with mock.patch.object(networks, 'MAX_BYTES', 10), self.assertRaises(ValueError):
            self.fetch(b'x' * 11)
        with mock.patch.object(networks, 'build_opener') as opener:
            opener.return_value.open.side_effect = OSError('offline')
            with self.assertRaisesRegex(RuntimeError, 'existing ranges were preserved'):
                networks.fetch_ranges({'provider': 'arvancloud'})

    def test_unknown_provider_is_not_treated_as_manual(self):
        with self.assertRaises(ValueError):
            networks.network_source({'network_source': {'provider': 'unknown'}})


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ikev2-cdn-refresh-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.settings = {'hostname': 'panel.example.com', 'port': 8080,
                         'network_source': {'provider': 'arvancloud'}}
        self.original = {
            'origin.json': json.dumps(self.settings).encode(),
            'cdn-ips.txt': (V4 + '\n').encode(),
            'Caddyfile': networks.render(self.settings, V4).encode(),
        }
        for name, data in self.original.items():
            path = self.root / name
            path.write_bytes(data)
            path.chmod(0o644)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(networks, 'OWNER_UID', os.getuid()).start()
        mock.patch.object(networks, 'VPN_STATE', self.root / 'vpn-state.env').start()
        self.fetch = mock.patch.object(networks, 'fetch_ranges', return_value='8.8.4.0/24\n').start()
        self.command = mock.patch.object(networks, 'command', return_value=subprocess.CompletedProcess([], 0)).start()
        self.health = mock.patch.object(networks, 'proxy_health').start()

    def refresh(self):
        return networks.refresh(self.root, '/fixture/caddy')

    def unchanged(self):
        for name, data in self.original.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def restarts(self):
        return [call for call in self.command.call_args_list if call.args[0][:2] == ['systemctl', 'restart']]

    def test_refresh_refuses_panel_vpn_collision_without_fetching_or_restart(self):
        state = self.root / 'vpn-state.env'
        state.write_text('SERVER_ID=panel.example.com\n')
        state.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'Panel hostname must differ'):
            self.refresh()
        self.unchanged()
        self.fetch.assert_not_called()
        self.command.assert_not_called()

    def test_identity_is_read_as_data_and_shell_assignments_are_never_executed(self):
        state = self.root / 'vpn-state.env'
        marker = self.root / 'must-not-exist'
        state.write_text(f'SERVER_ID=$(touch {marker})\n')
        state.chmod(0o600)
        with self.assertRaises(ValueError):
            networks.vpn_identity(state)
        self.assertFalse(marker.exists())
        state.write_text("SERVER_ID='VPN.EXAMPLE.COM.'\n")
        self.assertEqual(networks.vpn_identity(state), 'VPN.EXAMPLE.COM.')
        with self.assertRaisesRegex(ValueError, 'Panel hostname must differ'):
            networks.validate_panel_identity({'hostname': 'vpn.example.com'})
        state.write_text('SERVER_ID=vpn.example.com\nSERVER_ID=other.example.com\n')
        with self.assertRaises(ValueError):
            networks.vpn_identity(state)

    def test_success_updates_ranges_and_restarts_only_proxy(self):
        self.assertTrue(self.refresh())
        self.assertEqual((self.root / 'cdn-ips.txt').read_text(), '8.8.4.0/24\n')
        self.assertIn('8.8.4.0/24', (self.root / 'Caddyfile').read_text())
        self.assertEqual((self.root / 'origin.json').read_bytes(), self.original['origin.json'])
        self.assertEqual((self.root / 'cdn-ips.txt').stat().st_mode & 0o777, 0o644)
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(self.restarts()[0].args[0][-1], 'ikev2-web-proxy.service')
        self.health.assert_called_once_with(self.settings)

    def test_unchanged_ranges_do_not_restart(self):
        self.fetch.return_value = V4 + '\n'
        self.assertFalse(self.refresh())
        self.unchanged()
        self.assertEqual(self.restarts(), [])

    def test_download_or_invalid_feed_keeps_previous_files(self):
        self.fetch.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            self.refresh()
        self.unchanged()
        self.fetch.side_effect = None
        self.fetch.return_value = '0.0.0.0/0'
        with self.assertRaises(ValueError):
            self.refresh()
        self.unchanged()
        self.assertEqual(self.restarts(), [])

    def test_caddy_validation_failure_does_not_replace_files(self):
        def run(args, check=True):
            if args[0] == '/fixture/caddy':
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        self.command.side_effect = run
        with self.assertRaises(subprocess.CalledProcessError):
            self.refresh()
        self.unchanged()
        self.assertEqual(self.restarts(), [])

    def test_restart_failure_restores_both_files_and_old_service(self):
        attempts = []
        def run(args, check=True):
            if args[:2] == ['systemctl', 'restart']:
                attempts.append(args)
                if len(attempts) == 1:
                    raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        self.command.side_effect = run
        with self.assertRaisesRegex(RuntimeError, 'previous ranges and proxy configuration restored'):
            self.refresh()
        self.unchanged()
        self.assertEqual(len(attempts), 2)

    def test_health_failure_rolls_back(self):
        self.health.side_effect = [RuntimeError('unhealthy'), None]
        with self.assertRaisesRegex(RuntimeError, 'previous ranges and proxy configuration restored'):
            self.refresh()
        self.unchanged()
        self.assertEqual(len(self.restarts()), 2)

    def test_rollback_restart_failure_is_reported(self):
        self.command.side_effect = lambda args, check=True: (
            (_ for _ in ()).throw(subprocess.CalledProcessError(1, args))
            if args[:2] == ['systemctl', 'restart'] else subprocess.CompletedProcess(args, 0))
        with self.assertRaisesRegex(RuntimeError, 'rollback failed'):
            self.refresh()
        self.unchanged()

    def test_manual_or_stopped_proxy_does_not_fetch(self):
        settings = dict(self.settings)
        settings.pop('network_source')
        (self.root / 'origin.json').write_text(json.dumps(settings))
        self.assertFalse(self.refresh())
        self.fetch.assert_not_called()
        (self.root / 'origin.json').write_bytes(self.original['origin.json'])
        self.command.return_value = subprocess.CompletedProcess([], 3)
        self.assertFalse(self.refresh())
        self.fetch.assert_not_called()
        self.assertEqual(self.restarts(), [])

    def test_symlink_or_unsafe_permissions_are_rejected(self):
        (self.root / 'cdn-ips.txt').chmod(0o666)
        with self.assertRaises(ValueError):
            self.refresh()
        (self.root / 'cdn-ips.txt').chmod(0o644)
        (self.root / 'origin.json').unlink()
        (self.root / 'origin.json').symlink_to(self.root / 'cdn-ips.txt')
        with self.assertRaises(OSError):
            self.refresh()
        self.fetch.assert_not_called()

    def test_shared_lock_blocks_concurrent_refresh(self):
        self.settings.pop('network_source')
        (self.root / 'origin.json').write_text(json.dumps(self.settings))
        lock = self.root / '.networks.lock'
        with lock.open('a') as output:
            lock.chmod(0o600)
            fcntl.flock(output.fileno(), fcntl.LOCK_EX)
            code = '''import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("refresh", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.OWNER_UID = os.getuid()
module.VPN_STATE = Path(sys.argv[2]) / "vpn-state.env"
module.refresh(Path(sys.argv[2]))
'''
            process = subprocess.Popen([sys.executable, '-B', '-c', code,
                                        str(REPO / 'packaging/cdn-networks.py'), str(self.root)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.2)
                fcntl.flock(output.fileno(), fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertIn(b'Manual CDN list', stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == '__main__':
    unittest.main()
