import base64
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from engine import Engine, Error, local_path, relative, remote_config
from server import Server

BINARY = os.getenv('RCLONE_BINARY', '/tmp/webdav-rclone-test/rclone-v1.75.0-osx-arm64/rclone')


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.root = self.path / 'files'
        self.root.mkdir()
        (self.root / 'MiShare').mkdir()
        self.engine = Engine(self.path / 'state', self.root, BINARY, port(), '127.0.0.1')

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()


class ConfigurationTests(Base):
    def test_confined_paths(self):
        for p in ('../etc', 'MiShare/../../etc', 'a\\b', 'remote:x', 'a/./b', 'a\nb', '.webdav-history'):
            with self.assertRaises(Error):
                relative(p)
        (self.root / 'escape').symlink_to('/tmp')
        with self.assertRaises(Error):
            local_path(self.root, 'escape')
        self.assertEqual(local_path(self.root, 'MiShare'), self.root / 'MiShare')

    def test_remote_validation_and_secrets(self):
        body = dict(name='NAS', url='https://example.com/dav', username='u', password='secret-password')
        self.engine.mutate('remote/save', body)
        snapshot = self.engine.snapshot()
        self.assertNotIn('secret-password', json.dumps(snapshot))
        self.assertTrue(snapshot['remotes'][0]['hasPassword'])
        self.assertEqual((self.engine.data / 'config.json').stat().st_mode & 0o777, 0o600)
        for url in ('file:///etc', 'http://example.com', 'https://u:p@example.com', 'https://example.com/?token=secret', 'https://example.com/../x'):
            with self.assertRaises(Error):
                remote_config(dict(body, url=url))
        old = self.engine.config['remotes'][0]
        self.assertEqual(remote_config(dict(body, password=''), old)['password'], old['password'])

    def test_share_disabled_and_strong_password(self):
        self.assertFalse(self.engine.config['share']['enabled'])
        with self.assertRaises(Error):
            self.engine.mutate('share/start', {})
        with self.assertRaises(Error):
            self.engine.mutate('share/save', dict(path='MiShare', username='nas', password='weak'))

    def test_share_password_policy(self):
        cases = json.loads(Path(__file__).with_name('password_cases.json').read_text())
        cases += [{'value': 'a1' + 'a' * 198, 'valid': True},
                  {'value': 'a1' + 'a' * 199, 'valid': False},
                  {'value': 'a1' + '\U0001f600' * 198, 'valid': True},
                  {'value': None, 'valid': False}, {'value': 12345678, 'valid': False}]
        for case in cases:
            with self.subTest(case=case):
                body = dict(path='MiShare', username='nas', password=case['value'])
                before = json.dumps(self.engine.config)
                if case['valid']:
                    self.engine.mutate('share/save', body)
                    self.assertEqual(self.engine.config['share']['password'], case['value'])
                else:
                    with self.assertRaises(Error):
                        self.engine.mutate('share/save', body)
                    self.assertEqual(json.dumps(self.engine.config), before)

    def test_share_password_blank_preserves_existing(self):
        body = dict(path='MiShare', username='nas', password='')
        with self.assertRaises(Error):
            self.engine.mutate('share/save', body)
        self.engine.config['share']['password'] = 'oldlowercasepassword'
        for change in ({'password': ''}, {}):
            self.engine.mutate('share/save', dict(path='MiShare', username='nas', **change))
            self.assertEqual(self.engine.config['share']['password'], 'oldlowercasepassword')

    def test_copy_plan_and_link_destination(self):
        job = dict(local='MiShare', remote='backup', direction='download', bandwidth=5)
        cmd = self.engine.command(job, 'run1')
        self.assertEqual(cmd[1], 'copy')
        self.assertIn('--backup-dir', cmd)
        self.assertNotIn('--delete', ' '.join(cmd))
        self.assertNotIn('--copy-links', cmd)
        self.assertIn('5M', cmd)
        (self.root / 'MiShare' / 'escape').symlink_to('/tmp')
        with self.assertRaises(Error):
            self.engine.command(job, 'run1')

    def test_restart_marks_interrupted(self):
        self.engine.config['jobs'] = [{'id':'one','status':'running'}]
        self.engine.save()
        second = Engine(self.engine.data, self.root, BINARY)
        self.assertEqual(second.config['jobs'][0]['status'], 'interrupted')
        second.close()

    def test_scheduler_due_only_and_single_job(self):
        self.engine.config['jobs'] = [dict(id='one', interval=15, nextRun=time.time()+100)]
        with patch.object(self.engine, 'start_job') as start:
            self.engine.tick()
            start.assert_not_called()
            self.engine.config['jobs'][0]['nextRun'] = 0
            self.engine.tick()
            start.assert_called_once_with('one')
            start.reset_mock()
            self.engine.active_id = 'busy'
            self.engine.tick()
            start.assert_not_called()
        self.engine.active_id = None

    def test_job_limits_and_connection_removal(self):
        self.engine.mutate('remote/save',dict(name='Remote',url='https://example.com/',username='u',password='long-password'))
        rid=self.engine.config['remotes'][0]['id']
        body=dict(name='Test',local='MiShare',remote='',remoteId=rid,direction='upload',interval=0,bandwidth=0)
        for change in ({'local':''}, {'interval':1}, {'bandwidth':-1}, {'direction':'sync'}):
            with self.assertRaises(Error):
                self.engine.mutate('job/save',dict(body,**change))
        self.engine.mutate('job/save',body)
        with self.assertRaises(Error):
            self.engine.mutate('remote/remove',{'id':rid})


class AuthTests(Base):
    def setUp(self):
        super().setUp()
        self.server = Server(('127.0.0.1', 0), self.engine, 'u123', False, 'b' * 64)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, path, data=None, headers=None):
        request = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers=headers or {})
        try:
            return urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            return error

    def test_session_csrf_and_static_allowlist(self):
        self.assertEqual(self.request('/api/status').status, 401)
        html = self.request('/', headers={'X-Xiaomi-Client-Verify':'SUCCESS', 'X-Xiaomi-Client-DN':'CN=nas.999.a'}).read().decode()
        self.assertIn('name="webdav-session" content=""', html)
        response = self.request('/', headers={'X-Xiaomi-Client-Verify':'SUCCESS', 'X-Xiaomi-Client-DN':'CN=nas.123.a', 'X-Plugin-Proxy-Key':'b' * 64})
        html = response.read().decode()
        import re
        token = re.search('name="webdav-session" content="([^"]+)"', html)[1]
        csrf = re.search('name="csrf-token" content="([^"]+)"', html)[1]
        headers = {'X-WebDAV-Session':token, 'Content-Type':'application/json'}
        self.assertEqual(self.request('/api/status', headers=headers).status, 200)
        self.assertEqual(self.request('/api/share/stop', {}, headers).status, 403)
        folder = {'path':'MiShare', 'name':'AuthTest'}
        self.assertEqual(self.request('/api/folder/create', folder).status, 401)
        self.assertEqual(self.request('/api/folder/create', folder, headers).status, 403)
        self.assertFalse((self.root / 'MiShare/AuthTest').exists())
        headers['X-CSRF-Token'] = csrf
        self.assertEqual(self.request('/api/share/stop', {}, headers).status, 200)
        self.assertEqual(self.request('/api/folder/create', folder, headers).status, 200)
        rename = {'path':'MiShare/AuthTest', 'name':'Renamed'}
        self.assertEqual(self.request('/api/folder/rename', rename).status, 401)
        self.assertEqual(self.request('/api/folder/rename', rename,
                                     {'X-WebDAV-Session':token, 'Content-Type':'application/json'}).status, 403)
        self.assertTrue((self.root / 'MiShare/AuthTest').exists())
        self.assertEqual(self.request('/api/folder/rename', rename, headers).status, 200)
        self.assertTrue((self.root / 'MiShare/Renamed').is_dir())
        self.assertEqual(self.request('/api/status', headers={'X-WebDAV-Session':token+'x'}).status, 401)
        for path in ('/../engine.py', '/server.py', '/config.json', '/ca.key'):
            self.assertEqual(self.request(path).status, 404)


@unittest.skipUnless(Path(BINARY).exists(), 'Set RCLONE_BINARY to run real transfer tests')
class RcloneTests(Base):
    def test_https_auth_readonly_and_hidden_symlink(self):
        folder = self.root / 'MiShare'
        (folder / 'sample.txt').write_text('hello')
        (folder / 'outside').symlink_to('/tmp')
        self.engine.mutate('share/save', dict(path='MiShare', username='nas', password='DavTest8', readOnly=True))
        self.engine.mutate('share/start', {})
        context = ssl.create_default_context(cafile=str(self.engine.data / 'ca.crt'))
        url = f'https://127.0.0.1:{self.engine.port}/'
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(url, context=context)
        self.assertEqual(denied.exception.code, 401)
        headers = {'Authorization':'Basic '+base64.b64encode(b'nas:DavTest8').decode()}
        request = urllib.request.Request(url+'sample.txt', headers=headers)
        self.assertEqual(urllib.request.urlopen(request, context=context).read(), b'hello')
        for method in ('PUT', 'DELETE'):
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(urllib.request.Request(url+'sample.txt', data=b'changed' if method=='PUT' else None, method=method, headers=headers), context=context)
            self.assertIn(denied.exception.code, (403, 404, 405, 500))
        self.assertEqual((folder / 'sample.txt').read_text(), 'hello')
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(url+'outside/',headers=headers),context=context)

    def test_upload_download_retains_versions_and_no_deletes(self):
        remote_root = self.path / 'remote'
        remote_root.mkdir()
        remote_port = port()
        env = self.engine.base_env()
        env.update(RCLONE_USER='test', RCLONE_PASS='fixture-password')
        fixture = subprocess.Popen([BINARY,'serve','webdav',str(remote_root),'--addr',f'127.0.0.1:{remote_port}', '--dir-cache-time', '0s'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(.5)
            self.engine.mutate('remote/save',dict(name='Fixture',url=f'http://127.0.0.1:{remote_port}',username='test',password='fixture-password',allowHttp=True))
            rid = self.engine.config['remotes'][0]['id']
            self.assertEqual(self.engine.browse('remote', '', rid), [])
            (self.root / 'MiShare' / 'sample.txt').write_text('new long content')
            (remote_root / 'sample.txt').write_text('old')
            (remote_root / 'remote-only.txt').write_text('preserve')
            self.engine.mutate('job/save',dict(name='Upload',local='MiShare',remote='',remoteId=rid,direction='upload',interval=0,bandwidth=0))
            jid = self.engine.config['jobs'][0]['id']
            self.engine.mutate('job/run', {'id':jid})
            self.engine.worker.join(25)
            self.assertIsNone(self.engine.active_id)
            self.assertEqual(self.engine.config['jobs'][0]['status'], 'success', self.engine.config['jobs'][0])
            self.assertEqual((remote_root/'sample.txt').read_text(),'new long content')
            self.assertEqual((remote_root/'remote-only.txt').read_text(),'preserve')
            versions = list((remote_root/'.webdav-history').rglob('sample.txt'))
            self.assertEqual(len(versions),1)
            self.assertEqual(versions[0].read_text(),'old')
            (self.root/'download').mkdir()
            (self.root/'download'/'sample.txt').write_text('local older')
            self.engine.mutate('job/save',dict(name='Download',local='download',remote='',remoteId=rid,direction='download',interval=0,bandwidth=0))
            jid = self.engine.config['jobs'][-1]['id']
            self.engine.mutate('job/run',{'id':jid})
            self.engine.worker.join(25)
            self.assertEqual(self.engine.config['jobs'][-1]['status'],'success')
            self.assertEqual((self.root/'download'/'sample.txt').read_text(),'new long content')
            versions=list((self.root/'download'/'.webdav-history').rglob('sample.txt'))
            self.assertEqual(len(versions),1)
            self.assertEqual(versions[0].read_text(),'local older')
            # Cancel a genuinely throttled transfer; no child process may remain.
            with (self.root/'MiShare'/'large.bin').open('wb') as output:
                output.truncate(16*1024*1024)
            upload=self.engine.config['jobs'][0]
            upload['bandwidth']=1
            self.engine.mutate('job/run',{'id':upload['id']})
            deadline=time.monotonic()+5
            while self.engine.process is None and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertIsNotNone(self.engine.process)
            self.engine.mutate('job/cancel',{'id':upload['id']})
            self.engine.worker.join(8)
            self.assertIsNone(self.engine.active_id)
            self.assertEqual(upload['status'],'cancelled')
        finally:
            fixture.terminate()
            fixture.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
