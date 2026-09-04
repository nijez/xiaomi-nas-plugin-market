#!/usr/bin/env python3
"""Same-origin Xiaomi plugin UI with per-device sessions and CSRF protection."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import pwd
import re
import secrets
import signal
import threading
import time
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from engine import Engine, Error

PROJECT = Path(__file__).resolve().parent
WEB = PROJECT / 'web'
TTL = 30 * 86400


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, engine, user, dev=False):
        self.engine, self.user, self.dev = engine, user, dev
        self.probe_lock = threading.Lock()
        keyfile = engine.data / 'session.key'
        if not keyfile.exists():
            with keyfile.open('xb') as stream:
                os.chmod(keyfile, 0o600)
                stream.write(secrets.token_bytes(32))
        self.key = keyfile.read_bytes()
        super().__init__(addr, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'NASWebDAV/0.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def log_message(self, *args):
        pass

    def send(self, code, data, mime='application/json; charset=utf-8', headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def sign(self, value):
        return hmac.new(self.server.key, value.encode(), hashlib.sha256).hexdigest()

    def session(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = self.headers.get('X-WebDAV-Session', '') or (cookie['nas_webdav'].value if 'nas_webdav' in cookie else '')
            payload, signature = token.rsplit('.', 1)
            if len(token) > 200 or int(payload.split('.')[0]) < time.time():
                return None
            return token if secrets.compare_digest(signature, self.sign(payload)) else None
        except (ValueError, CookieError, UnicodeError):
            return None

    def trusted(self):
        if self.server.dev:
            return True
        verify = self.headers.get('X-Xiaomi-Client-Verify', '') == 'SUCCESS'
        dn = self.headers.get('X-Xiaomi-Client-DN', '')
        owner = re.search(r'CN=nas\.' + re.escape(self.server.user.lstrip('u')) + r'\.', dn)
        if verify and owner:
            return True
        try:
            return ipaddress.ip_address(self.headers.get('X-Real-IP', '')).is_loopback
        except ValueError:
            return False

    def require(self, write=False):
        token = self.session()
        if not token:
            self.send(401, {'ok': False, 'error': '请从小米智能存储客户端重新打开 WebDAV 文件桥'})
            return False
        if write and not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.sign('csrf:' + token)):
            self.send(403, {'ok': False, 'error': '操作令牌失效，请重新打开插件'})
            return False
        return True

    def do_GET(self):
        route = urlsplit(self.path)
        path = route.path
        if path == '/healthz':
            self.send(200, {'ok': True, 'version': '0.2.0-rc5'})
            return
        if path in ('/', '/index.html'):
            token = self.session()
            if not token and self.trusted():
                payload = f'{int(time.time()) + TTL}.{secrets.token_hex(16)}'
                token = payload + '.' + self.sign(payload)
            page = (WEB / 'index.html').read_text().replace('__SESSION_TOKEN__', token or '').replace('__CSRF_TOKEN__', self.sign('csrf:' + token) if token else '')
            base = '/' if self.server.dev else '/plugin/' + self.server.user.lstrip('u') + '/webdav/'
            headers = {'Set-Cookie': f'nas_webdav={token}; Path={base}; HttpOnly; SameSite=Strict; Max-Age={TTL}' + ('' if self.server.dev else '; Secure')} if token else {}
            self.send(200, page.encode(), 'text/html; charset=utf-8', headers)
            return
        if path.startswith('/api/'):
            if not self.require():
                return
            try:
                if path == '/api/status':
                    self.send(200, {'ok': True, **self.server.engine.snapshot()})
                elif path == '/api/browse':
                    query = parse_qs(route.query)
                    location = query.get('location', ['local'])[0]
                    if location not in ('local', 'remote'):
                        raise Error('未知目录类型')
                    if not self.server.probe_lock.acquire(False):
                        raise Error('另一个目录查询正在进行')
                    try:
                        items = self.server.engine.browse(location, query.get('path', [''])[0], query.get('id', [''])[0])
                    finally:
                        self.server.probe_lock.release()
                    self.send(200, {'ok': True, 'items': items})
                elif path == '/api/certificate':
                    cert = self.server.engine.data / 'ca.crt'
                    if not cert.exists():
                        raise Error('首次开启共享后才会生成证书')
                    self.send(200, cert.read_bytes(), 'application/x-x509-ca-cert', {'Content-Disposition': 'attachment; filename="nas-webdav-ca.crt"'})
                else:
                    self.send(404, {'ok': False, 'error': 'not found'})
            except (Error, ValueError, OSError) as e:
                self.send(400, {'ok': False, 'error': str(e) if isinstance(e, Error) else '目录查询失败'})
            return
        # Static allowlist: no source, credentials, cert keys or dot files served.
        file = WEB / path.lstrip('/')
        public_asset = re.fullmatch(r'/assets/(webdav|reload|folder|close|plus|up|copy|link|edit|trash|upload|download|play|stop|transfer)\.png', path)
        if (path in ('/app.bundle.js', '/app.js', '/access-ui.js', '/icon-assets.js', '/styles.css') or public_asset) and file.is_file():
            self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        else:
            self.send(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        if not self.require(True):
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384 or self.headers.get_content_type() != 'application/json':
                raise Error('请求格式不正确')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise Error('请求必须为 JSON 对象')
            route = urlsplit(self.path).path
            if not route.startswith('/api/'):
                raise Error('未知操作')
            self.server.engine.mutate(route[5:], body)
            self.send(200, {'ok': True})
        except (Error, ValueError, TypeError, KeyError, OSError) as e:
            self.send(400, {'ok': False, 'error': str(e) if isinstance(e, Error) else '设置未保存，请检查字段或服务状态'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dev', action='store_true')
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '18120')))
    args = parser.parse_args()
    os.umask(0o077)
    user = os.getenv('NAS_USER_ID', '')
    if not args.dev and not re.fullmatch(r'u[0-9]+', user):
        raise SystemExit('NAS_USER_ID is required')
    data = Path(os.getenv('DATA_DIR', '/data/plugin/webdav/data'))
    root = Path(os.getenv('LOCAL_ROOT', f'/nas/pool0/{user}/data'))
    binary = Path(os.getenv('RCLONE_BINARY', str(PROJECT / 'bin/rclone')))
    if not binary.is_file():
        raise SystemExit('Bundled rclone is missing')
    if not args.dev:
        os.chmod(binary, 0o755)
    if os.getuid() == 0 and user:
        account = pwd.getpwnam(user)
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(data, account.pw_uid, account.pw_gid)
        os.initgroups(user, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    engine = Engine(data, root, binary)
    app = Server(('127.0.0.1', args.port), engine, user, args.dev)
    supervisor = threading.Thread(target=engine.supervise, daemon=True)
    supervisor.start()
    def shutdown(*_):
        threading.Thread(target=app.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        app.serve_forever()
    finally:
        engine.close()
        app.server_close()


if __name__ == '__main__':
    main()
