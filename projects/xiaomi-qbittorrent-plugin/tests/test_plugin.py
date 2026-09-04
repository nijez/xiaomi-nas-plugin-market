import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from engine import Engine, Error, IMAGE, NAME, LABEL, confined, mutation, password_hash, container_args
from server import Server


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'MiShare').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)

    def test_password_hash_matches_qb(self):
        salt, key = (base64.b64decode(v) for v in password_hash('Example123!').split(':'))
        self.assertEqual(len(salt), 16)
        self.assertEqual(key, hashlib.pbkdf2_hmac('sha512', b'Example123!', salt, 100000, 64))

    def test_password_policy(self):
        for value in ['abc', '12345678', 'abcdefgh', 'ABCD123\n', None]:
            with self.subTest(value=value), self.assertRaises(Error):
                password_hash(value)

    def test_password_salt_random(self):
        self.assertNotEqual(password_hash('Example123'), password_hash('Example123'))

    def test_confined_normal(self):
        self.assertEqual(confined(self.root, 'MiShare'), self.root / 'MiShare')

    def test_reject_traversal(self):
        for name in ['../', '/etc', '.', 'MiShare/..', 'MiShare//x', '.hidden', 'MiShare\\x']:
            with self.subTest(name=name), self.assertRaises(Error):
                confined(self.root, name)

    def test_reject_symlink(self):
        (self.root / 'link').symlink_to(self.root / 'MiShare')
        with self.assertRaises(Error):
            confined(self.root, 'link')

    def test_browse_hides_symlinks(self):
        (self.root / 'alias').symlink_to(self.root / 'MiShare')
        self.assertEqual(self.engine.browse(''), [{'name': 'MiShare', 'path': 'MiShare'}])

    def test_task_hash_not_all(self):
        for value in ['all', 'x', 'a' * 40 + '|all', None]:
            with self.assertRaises(Error):
                mutation('stop', {'hash': value})

    def test_remove_always_keeps_files(self):
        route, params = mutation('remove', {'hash': 'a' * 40, 'deleteFiles': True})
        self.assertEqual(route, 'torrents/delete')
        self.assertEqual(params['deleteFiles'], 'false')

    def test_only_whitelisted_mutations(self):
        with self.assertRaises(Error):
            mutation('setLocation', {'location': '/config'})

    def test_limits_bounds(self):
        for value in [-1, 11, True, '2']:
            with self.assertRaises(Error):
                mutation('limits', {'download': 0, 'upload': 0, 'active': value})

    def test_limits_units(self):
        route, params = mutation('limits', {'download': 100, 'upload': 200, 'active': 2})
        prefs = json.loads(params['json'])
        self.assertEqual(prefs['dl_limit'], 102400)
        self.assertTrue(prefs['queueing_enabled'])

    def test_magnet_fixed_directory(self):
        route, params = mutation('magnet', {'url': 'magnet:?xt=urn:btih:' + 'a' * 40, 'savepath': '/config'})
        self.assertEqual(params['savepath'], '/downloads')

    def test_non_magnets_rejected(self):
        for url in ['http://example.org/file', 'magnet:?foo=bar', 'magnet:?xt=urn:btih:' + 'a'*40 + '\nhttp://example.org']:
            with self.assertRaises(Error):
                mutation('magnet', {'url': url})

    def test_container_isolation(self):
        args = container_args({'owner': 'test', 'uid': 1000, 'gid': 1000, 'download': '/test/downloads'}, Path('/test/private'))
        self.assertIn(IMAGE, args)
        self.assertIn('127.0.0.1:18123:18123', args)
        self.assertNotIn('--privileged', args)
        self.assertNotIn('--network', args)
        self.assertFalse(any('docker.sock' in a for a in args))
        self.assertEqual(args.count('--mount'), 2)
        self.assertEqual(args[args.index('--restart')+1], 'no')

    def test_dev_cannot_start(self):
        self.engine.dev = True
        with self.assertRaises(Error):
            self.engine.launch('start', {})

    def test_refuses_existing_directory(self):
        (self.root / 'MiShare/qBDownloads').mkdir()
        with patch.object(self.engine, 'docker') as docker, self.assertRaises(Error):
            self.engine.setup('MiShare', 'Example123')
        docker.assert_not_called()

    def test_directory_identity_change(self):
        folder = self.root / 'MiShare'
        s = folder.stat()
        self.engine.config = {'relative': 'MiShare', 'download': str(folder), 'device': s.st_dev, 'inode': s.st_ino + 1}
        with self.assertRaises(Error):
            self.engine.check_directory()

    def test_foreign_container_not_stopped(self):
        self.engine.config = {'owner': 'mine'}
        with patch.object(self.engine, 'docker', side_effect=[NAME, json.dumps([{'Config': {'Labels': {LABEL: 'foreign'}}}])]) as docker:
            with self.assertRaises(Error):
                self.engine.stop()
            self.assertEqual(docker.call_count, 2)

    def test_stop_remembers_preference(self):
        self.engine.config = {'owner': 'mine', 'enabled': True}
        with patch.object(self.engine, 'owned', return_value={'State': {'Running': True}}), patch.object(self.engine, 'docker') as docker:
            self.engine.stop()
            docker.assert_called_once_with('stop', '--time', '15', NAME)
        self.assertFalse(json.loads(self.engine.cfgfile.read_text())['enabled'])

    def test_service_stop_preserves_enabled(self):
        self.engine.config = {'enabled': True}
        with patch.object(self.engine, 'owned', return_value=None):
            self.engine.stop(remember=False)
        self.assertTrue(self.engine.config['enabled'])

    def test_setup_writes_only_owned_child(self):
        # This test runs on a normal non-root Mac user and mocks only chown/Docker.
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')
        before = (self.root / 'MiShare').stat()
        with patch('engine.os.chown'), patch.object(self.engine, 'docker', return_value=''):
            self.engine.setup('MiShare', 'Example123')
        after = (self.root / 'MiShare').stat()
        self.assertEqual((before.st_uid, before.st_gid), (after.st_uid, after.st_gid))
        self.assertEqual(self.engine.config['relative'], 'MiShare/qBDownloads')
        conf = (self.engine.data / 'config/qBittorrent/qBittorrent.conf').read_text()
        self.assertNotIn('Example123', conf)
        self.assertIn('PortForwardingEnabled=false', conf)
        self.assertIn('CSRFProtection=true', conf)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = Engine(Path(self.tmp.name) / 'data', self.tmp.name, dev=True)
        self.server = Server(('127.0.0.1', 0), self.engine, 'u123456', dev=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        def close():
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.addCleanup(close)
        _, html = self.request('GET', '/')
        self.token = re.search(r'name="qb-session" content="([^"]+)"', html.decode())[1]
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', html.decode())[1]

    def request(self, method, route, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            conn.request(method, route, json.dumps(data) if data is not None else None, headers or {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def auth(self):
        return {'X-QB-Session': self.token, 'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'}

    def test_unauthenticated_reads_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_csrf_required(self):
        self.assertEqual(self.request('POST', '/api/remove', {}, {'X-QB-Session': self.token})[0], 403)

    def test_status_no_secrets(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertNotIn(self.server.key.hex(), body.decode())

    def test_malformed_session(self):
        self.assertEqual(self.request('GET', '/api/status', headers={'X-QB-Session':'garbage'})[0], 401)

    def test_no_arbitrary_file_serving(self):
        self.assertEqual(self.request('GET', '/engine.py', headers=self.auth())[0], 404)

    def test_no_generic_api(self):
        self.assertEqual(self.request('POST', '/api/app/setPreferences', {}, self.auth())[0], 400)

    def test_preview_write_blocked(self):
        code, body = self.request('POST', '/api/service/start', {}, self.auth())
        self.assertEqual(code, 400)

    def test_qb_login_required(self):
        code, body = self.request('GET', '/api/torrents', headers=self.auth())
        self.assertEqual(code, 400)

    def test_login_password_not_stored(self):
        with patch('server.qb_request', return_value=(200, b'Ok.', 'SID=' + 'a'*32 + '; HttpOnly')):
            code, _ = self.request('POST', '/api/login', {'password':'secret123'}, self.auth())
        self.assertEqual(code, 200)
        self.assertNotIn('secret123', repr(self.server.logins))

    def test_expired_qb_cookie_cleared(self):
        self.server.logins[self.token] = ('SID=test', 9999999999)
        with patch('server.qb_request', return_value=(403, b'', '')):
            self.request('GET', '/api/torrents', headers=self.auth())
        self.assertNotIn(self.token, self.server.logins)

    def test_wrong_device_cert_gets_no_session(self):
        self.server.dev = False
        code, body = self.request('GET', '/', headers={'X-Xiaomi-Client-Verify':'SUCCESS','X-Xiaomi-Client-DN':'CN=nas.999999.test.2','X-Real-IP':'192.168.1.2'})
        self.assertIn(b'name="qb-session" content=""', body)

    def test_owner_cert_gets_session(self):
        self.server.dev = False
        _, body = self.request('GET', '/', headers={'X-Xiaomi-Client-Verify':'SUCCESS','X-Xiaomi-Client-DN':'CN=nas.123456.test.2'})
        self.assertNotIn(b'name="qb-session" content=""', body)


if __name__ == '__main__':
    unittest.main()
