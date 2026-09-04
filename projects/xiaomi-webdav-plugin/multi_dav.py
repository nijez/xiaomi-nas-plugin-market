"""HTTPS-only multi-user DAV, with an independent default-deny policy boundary."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import signal
import ssl
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from compatibility import runtime_issues

if runtime_issues():
    raise RuntimeError('WebDAV runtime integrity check failed')

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))

from cheroot.ssl.builtin import BuiltinSSLAdapter
from cheroot.wsgi import Server
from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
from wsgidav.dav_provider import DAVCollection, DAVProvider
from wsgidav.error_printer import ErrorPrinter
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.lock_man.lock_storage import LockStorageDict
from wsgidav.lock_man.lock_manager import LockManager
from wsgidav.request_resolver import RequestResolver
from wsgidav.wsgidav_app import WsgiDAVApp

from access import AccessStore, verify_password
from engine import Error


class DirectoryIndex(DAVCollection):
    def get_member_names(self):
        return list(self.provider.names) if self.path == '/' else []

    def get_member(self, name):
        return DirectoryIndex('/' + name, self.environ) if name in self.provider.names else None

    def get_display_name(self):
        return self.provider.names.get(self.path.strip('/'), 'WebDAV')


class IndexProvider(DAVProvider):
    def __init__(self, names):
        super().__init__()
        self.names = names

    def is_readonly(self):
        return True

    def get_resource_inst(self, path, environ):
        return DirectoryIndex(path, environ) if path in ('', '/') else None


class ConfinedProvider(FilesystemProvider):
    def _loc_to_file_path(self, path, environ=None):
        fp = super()._loc_to_file_path(path, environ)
        candidate = Path(self.root_folder_path)
        for part in Path(fp).relative_to(candidate).parts:
            candidate = candidate / part
            if part == '.webdav-history' or candidate.is_symlink():
                raise DAVError(HTTP_FORBIDDEN)
        return fp


class MultiDAV:
    def __init__(self, data, root):
        self.store = AccessStore(data, root)
        issues = self.store.compatibility()
        if issues or not self.store.config['enabled']:
            raise Error('多用户权限配置未启用或兼容性检查失败')
        self.policy_digest = hashlib.sha256(self.store.file.read_bytes()).digest()
        self.cache_key = os.urandom(32)
        self.cache, self.failures = {}, {}
        self.auth_lock = threading.Lock()
        self.hash_slots = threading.BoundedSemaphore(2)
        self.apps, self.users = {}, {}
        self.folders = {f['id']: f for f in self.store.config['folders']}
        locks = LockManager(LockStorageDict())
        for user in self.store.config['users']:
            if not user['enabled'] or not user['grants']:
                continue
            providers = {'/': IndexProvider({k: self.folders[k]['name'] for k in user['grants']})}
            for ident, grant in user['grants'].items():
                providers['/' + ident] = ConfinedProvider(str(self.store.root / self.folders[ident]['path']),
                                                         readonly=grant == 'read', fs_opts={'follow_symlinks': False})
            # Only the outer authenticated boundary is ever passed to the HTTP server.
            app = WsgiDAVApp({'provider_mapping': providers, 'middleware_stack': [ErrorPrinter, RequestResolver],
                             'lock_storage': False, 'property_manager': False, 'verbose': 0,
                             'logging': {'enable': False}, 'dir_browser': {'enable': False},
                             'suppress_version_info': True})
            app.lock_manager = locks
            for provider in providers.values():
                provider.set_lock_manager(locks)
            self.apps[user['username']] = app
            self.users[user['username']] = user
        if not self.apps:
            raise Error('没有已启用且获得授权的账号')

    def intact(self):
        try:
            return (hmac.compare_digest(hashlib.sha256(self.store.file.read_bytes()).digest(), self.policy_digest)
                    and not self.store.compatibility())
        except OSError:
            return False

    def authenticate(self, environ):
        header = environ.get('HTTP_AUTHORIZATION', '')
        if len(header) > 4096 or not header.startswith('Basic '):
            return None
        peer = environ.get('REMOTE_ADDR', '')
        now = time.monotonic()
        key = hmac.new(self.cache_key, header.encode(), hashlib.sha256).digest()
        with self.auth_lock:
            count, until = self.failures.get(peer, (0, 0))
            if count >= 8 and now < until:
                return None
            cached = self.cache.get(key)
            if cached and cached[1] > now:
                return cached[0]
        if not self.hash_slots.acquire(False):
            return None
        try:
            raw = base64.b64decode(header[6:], validate=True).decode('utf-8')
            name, password = raw.split(':', 1)
            user = self.users.get(name)
            ok = bool(user) and len(password) <= 200 and verify_password(password, user['passwordHash'])
        except (ValueError, UnicodeError):
            name, ok = '', False
        finally:
            self.hash_slots.release()
        with self.auth_lock:
            if ok:
                if len(self.cache) >= 128:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = (name, now + 30)
                self.failures.pop(peer, None)
                return name
            if len(self.failures) >= 512 and peer not in self.failures:
                self.failures.pop(next(iter(self.failures)))
            self.failures[peer] = ((count if now < until else 0) + 1, until if now < until else now + 60)
        return None

    @staticmethod
    def deny(start_response, code):
        headers = [('Content-Type', 'text/plain; charset=utf-8'), ('Content-Length', '0'),
                   ('Cache-Control', 'no-store'), ('Connection', 'close')]
        if code == '401 Unauthorized':
            headers.append(('WWW-Authenticate', 'Basic realm="NAS WebDAV", charset="UTF-8"'))
        start_response(code, headers)
        return []

    @staticmethod
    def parts(path):
        if (not path.startswith('/') or '//' in path or '\\' in path or '%' in path
                or any(ord(c) < 32 for c in path)):
            raise ValueError('invalid path')
        parts = path.strip('/').split('/') if path.strip('/') else []
        if any(p in ('.', '..', '.webdav-history') for p in parts):
            raise ValueError('invalid path')
        return parts

    def __call__(self, environ, start_response):
        if environ.get('wsgi.url_scheme') != 'https' or not self.intact():
            return self.deny(start_response, '503 Service Unavailable')
        name = self.authenticate(environ)
        if name is None:
            return self.deny(start_response, '401 Unauthorized')
        try:
            path = environ['PATH_INFO'].encode('latin-1').decode('utf-8')
            parts = self.parts(path)
            method = environ['REQUEST_METHOD']
            grants = self.users[name]['grants']
            if not parts:
                if method not in ('OPTIONS', 'PROPFIND') or environ.get('HTTP_DEPTH', '1') not in ('0', '1'):
                    return self.deny(start_response, '403 Forbidden')
            else:
                grant = grants.get(parts[0])
                if not grant:
                    return self.deny(start_response, '404 Not Found')
                if method not in ('OPTIONS', 'PROPFIND', 'GET', 'HEAD') and grant != 'write':
                    return self.deny(start_response, '403 Forbidden')
                if len(parts) == 1 and method not in ('OPTIONS', 'PROPFIND', 'GET', 'HEAD'):
                    return self.deny(start_response, '403 Forbidden')
            if method in ('COPY', 'MOVE'):
                dest = urlsplit(environ.get('HTTP_DESTINATION', ''))
                if dest.query or dest.fragment or dest.username or dest.password:
                    raise ValueError('invalid destination')
                if dest.netloc and (dest.netloc != environ.get('HTTP_HOST') or dest.scheme != 'https'):
                    raise ValueError('foreign destination')
                target = self.parts(unquote(dest.path, errors='strict'))
                # Cross-share operations are deliberately disallowed, even between writable grants.
                if len(target) < 2 or target[0] != parts[0] or grants.get(target[0]) != 'write':
                    return self.deny(start_response, '403 Forbidden')
            environ['wsgidav.auth.user_name'] = name
            environ['wsgidav.auth.realm'] = 'NAS WebDAV'
            environ['wsgidav.auth.roles'] = []
            environ['wsgidav.auth.permissions'] = []
            return self.apps[name](environ, start_response)
        except (ValueError, UnicodeError, KeyError):
            return self.deny(start_response, '400 Bad Request')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--host', action='append', required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    app = MultiDAV(args.data, args.root)
    servers = []
    for host in args.host:
        server = Server((host, args.port), app, numthreads=4, max=4, timeout=20, request_queue_size=16)
        server.ssl_adapter = BuiltinSSLAdapter(str(Path(args.data) / 'server.crt'), str(Path(args.data) / 'server.key'))
        server.ssl_adapter.context.minimum_version = ssl.TLSVersion.TLSv1_2
        server.prepare()
        servers.append(server)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = [threading.Thread(target=s.serve, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    try:
        while not stop.wait(1):
            if not app.intact() or not all(t.is_alive() for t in threads):
                break
    finally:
        for server in servers:
            server.stop()


if __name__ == '__main__':
    main()
