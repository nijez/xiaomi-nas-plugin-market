import copy
import hashlib
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from access import AccessStore, host_identity, verify_password
from engine import Error
from compatibility import runtime_issues
from test_webdav import Base


class PolicyTests(Base):
    def folder(self, ident='docs', path='MiShare'):
        self.engine.mutate('access/folder/save', dict(id=ident, name=ident, path=path))

    def user(self, **changes):
        body = dict(username='alice', password='PassTest8', enabled=True, grants={'docs': 'read'})
        self.engine.mutate('access/user/save', dict(body, **changes))

    def test_no_automatic_migration_and_hashed_credentials(self):
        original = copy.deepcopy(self.engine.config)
        self.assertFalse(self.engine.access.file.exists())
        self.folder()
        self.user()
        self.assertEqual(self.engine.config, original)
        self.assertNotIn('PassTest8', self.engine.access.file.read_text())
        snap = json.dumps(self.engine.snapshot())
        self.assertNotIn('passwordHash', snap)
        self.assertNotIn('pbkdf2-sha256', snap)
        user = self.engine.access.config['users'][0]
        self.assertTrue(verify_password('PassTest8', user['passwordHash']))
        self.user(password='')
        self.assertEqual(user['passwordHash'], self.engine.access.config['users'][0]['passwordHash'])
        self.assertEqual(self.engine.access.file.stat().st_mode & 0o777, 0o600)

    def test_default_deny_overlap_and_invalid_grants(self):
        self.folder()
        (self.root/'MiShare'/'nested').mkdir()
        for ident, path in [('nested', 'MiShare/nested'), ('same', 'MiShare'), ('root', ''), ('outside', '../')]:
            with self.assertRaises(Error):
                self.folder(ident, path)
        for grants in ({'missing': 'read'}, {'docs': 'admin'}, [], None):
            with self.assertRaises(Error):
                self.user(grants=grants)
        self.user(grants={})
        with self.assertRaises(Error):
            self.engine.mutate('access/start', {})
        self.user(enabled=False)
        with self.assertRaises(Error):
            self.engine.mutate('access/start', {})

    def test_corrupt_future_schema_never_overwritten(self):
        self.folder()
        invalid_host = dict(self.engine.access.config, acceptedHost={})
        for payload in ('{invalid', '[]', 'null', json.dumps(invalid_host), json.dumps({'schema': 9000, 'enabled': True})):
            self.engine.access.file.write_text(payload)
            store = AccessStore(self.engine.data, self.root)
            self.assertTrue(store.error)
            for action in ('start', 'accept-host', 'folder/save'):
                with self.assertRaises(Error):
                    store.mutate(action, dict(confirm=True, id='x', name='x', path='MiShare'))
            self.assertEqual(store.file.read_text(), payload)

    def test_firmware_changed_and_directory_replaced(self):
        self.folder()
        self.user()
        changed = dict(host_identity(self.root), firmware='changed')
        with patch('access.host_identity', return_value=changed):
            self.assertTrue(self.engine.access.compatibility())
            with self.assertRaises(Error):
                self.engine.access.mutate('start', {})
            with self.assertRaises(Error):
                self.engine.access.mutate('accept-host', {})
        (self.root/'MiShare').rename(self.root/'previous')
        (self.root/'MiShare').mkdir()
        self.assertTrue(self.engine.access.compatibility())
        with self.assertRaises(Error):
            self.engine.access.mutate('accept-host', {'confirm': True})

    def test_explicit_import_preserves_legacy_and_is_off(self):
        self.engine.mutate('share/save', dict(path='MiShare', username='legacyuser', password='PassTest8', readOnly=True))
        before = copy.deepcopy(self.engine.config)
        with self.assertRaises(Error):
            self.engine.mutate('access/import-legacy', {})
        self.engine.mutate('access/import-legacy', {'confirm': True})
        self.assertEqual(self.engine.config, before)
        self.assertFalse(self.engine.access.config['enabled'])
        self.assertEqual(self.engine.access.config['users'][0]['grants'], {'legacy': 'read'})
        with self.assertRaises(Error):
            self.engine.mutate('access/import-legacy', {'confirm': True})

    def test_remove_folder_revokes_all_grants(self):
        self.folder()
        self.user()
        self.engine.mutate('access/folder/remove', {'id': 'docs'})
        self.assertEqual(self.engine.access.config['users'][0]['grants'], {})
        self.assertTrue((self.root/'MiShare').exists())

    def test_runtime_dependency_missing_or_changed(self):
        project = self.root / 'runtime-fixture'
        (project / 'vendor').mkdir(parents=True)
        source = project / 'vendor/module.py'
        source.write_text('value = 1\n')
        (project / 'vendor-integrity.json').write_text(json.dumps({'module.py': hashlib.sha256(source.read_bytes()).hexdigest()}))
        self.assertEqual(runtime_issues(project), [])
        source.write_text('value = 2\n')
        self.assertTrue(runtime_issues(project))
        source.unlink()
        self.assertTrue(runtime_issues(project))


class HTTPSAccessTests(Base):
    def setUp(self):
        super().setUp()
        (self.root/'Backup').mkdir()
        (self.root/'MiShare'/'file.txt').write_text('alpha')
        (self.root/'Backup'/'file.txt').write_text('beta')
        for ident, path in [('docs', 'MiShare'), ('backup', 'Backup')]:
            self.engine.mutate('access/folder/save', dict(id=ident, name=ident, path=path))
        for name, grants, enabled in [('alice', {'docs': 'read', 'backup': 'write'}, True),
                                      ('bob', {'docs': 'write'}, True), ('disabled', {'docs': 'read'}, False)]:
            self.engine.mutate('access/user/save', dict(username=name, password='PassTest8', grants=grants, enabled=enabled))
        self.engine.mutate('access/start', {})
        self.ctx = ssl.create_default_context(cafile=str(self.engine.data/'ca.crt'))
        self.url = f'https://127.0.0.1:{self.engine.port}'

    def request(self, path='/', method='GET', user='alice', password='PassTest8', data=None, **headers):
        import base64
        if user is not None:
            headers['Authorization'] = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
        request = urllib.request.Request(self.url+path, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, context=self.ctx, timeout=8) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as response:
            return response.code, response.read()

    def test_auth_and_scoped_listing(self):
        self.assertEqual(self.request('/docs/file.txt', user=None)[0], 401)
        self.assertEqual(self.request('/docs/file.txt', password='wrong')[0], 401)
        self.assertEqual(self.request('/docs/file.txt', user='disabled')[0], 401)
        self.assertEqual(self.request('/docs/file.txt'), (200, b'alpha'))
        code, body = self.request('/', 'PROPFIND', user='bob', Depth='1')
        self.assertEqual(code, 207, body)
        self.assertIn(b'/docs', body)
        self.assertNotIn(b'/backup', body)
        self.assertEqual(self.request('/backup/file.txt', user='bob')[0], 404)

    def test_read_only_all_mutating_methods(self):
        for method in ('PUT', 'DELETE', 'MKCOL', 'PROPPATCH', 'LOCK', 'UNLOCK', 'COPY', 'MOVE'):
            with self.subTest(method=method):
                self.assertEqual(self.request('/docs/file.txt', method, data=b'x', Destination=self.url+'/docs/other')[0], 403)
        self.assertEqual((self.root/'MiShare'/'file.txt').read_text(), 'alpha')

    def test_write_and_destination_boundaries(self):
        self.assertIn(self.request('/backup/new.txt', 'PUT', data=b'new')[0], (201, 204))
        self.assertEqual(self.request('/backup/new.txt'), (200, b'new'))
        self.assertIn(self.request('/backup/new.txt', 'COPY', Destination=self.url+'/backup/copied.txt')[0], (201, 204))
        self.assertEqual(self.request('/backup/copied.txt'), (200, b'new'))
        for destination in (self.url+'/docs/stolen', self.url+'/backup', 'https://foreign.invalid/backup/x', self.url+'/backup/%2e%2e/docs/x'):
            self.assertIn(self.request('/backup/new.txt', 'MOVE', Destination=destination)[0], (400, 403))
        self.assertEqual(self.request('/backup', 'DELETE')[0], 403)
        self.assertTrue((self.root/'Backup'/'new.txt').exists())

    def test_path_and_symlink_isolation(self):
        (self.root/'Backup'/'link').symlink_to(self.root/'MiShare', target_is_directory=True)
        for path in ('/backup/link/file.txt', '/backup/%2e%2e/MiShare/file.txt', '/backup/%252e%252e/MiShare/file.txt', '/backup/.webdav-history/file'):
            self.assertIn(self.request(path)[0], (400, 403, 404))
        self.assertNotEqual(self.request('/backup/link/new.txt', 'PUT', data=b'bad')[0], 201)
        self.assertFalse((self.root/'MiShare'/'new.txt').exists())

    def test_policy_change_fail_closed_and_live_edit_denied(self):
        with self.assertRaises(Error):
            self.engine.mutate('access/user/remove', {'username': 'bob'})
        self.assertEqual(self.request('/docs/file.txt')[0], 200)
        cfg = json.loads(self.engine.access.file.read_text())
        cfg['revision'] += 1
        self.engine.access.file.write_text(json.dumps(cfg))
        try:
            self.assertEqual(self.request('/docs/file.txt')[0], 503)
        except (urllib.error.URLError, ConnectionError):
            # The supervisor may already have stopped the listener.
            self.assertIsNotNone(self.engine.multi.poll())

    def test_restart_preserves_policy_and_no_legacy_fallback(self):
        self.engine.stop_multi()
        self.engine.start_multi()
        self.assertEqual(self.request('/backup/file.txt', user='bob')[0], 404)
        self.assertEqual(self.request('/docs/file.txt')[0], 200)
        with self.assertRaises(Error):
            self.engine.mutate('share/start', {})

    def test_disabled_account_cannot_reuse_cached_login(self):
        self.assertEqual(self.request('/docs/file.txt')[0], 200)
        self.engine.mutate('access/stop', {})
        self.engine.mutate('access/user/save', dict(username='alice', password='', enabled=False,
                                                 grants={'docs': 'read', 'backup': 'write'}))
        self.engine.mutate('access/start', {})
        self.assertEqual(self.request('/docs/file.txt')[0], 401)
        self.assertEqual(self.request('/docs/file.txt', user='bob')[0], 200)

    def test_exclusive_lock_shared_between_users(self):
        self.engine.mutate('access/stop', {})
        self.engine.mutate('access/user/save', dict(username='alice', password='', enabled=True,
                                                 grants={'docs': 'write'}))
        self.engine.mutate('access/start', {})
        body = b'<D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype><D:owner>alice</D:owner></D:lockinfo>'
        status, response = self.request('/docs/file.txt', 'LOCK', data=body,
                                        **{'Content-Type': 'application/xml', 'Timeout': 'Second-60'})
        self.assertEqual(status, 200, response)
        self.assertEqual(self.request('/docs/file.txt', 'PUT', user='bob', data=b'overwrite')[0], 423)
        self.assertEqual((self.root/'MiShare'/'file.txt').read_text(), 'alpha')
