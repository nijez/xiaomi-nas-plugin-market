"""Owner-authenticated, same-origin qB download UI; no generic Docker proxy."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import sys
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from engine import Engine, Error, VERSION, qb_request, mutation, torrent_hash

if (Path(__file__).resolve().parents[2] / 'shared').is_dir():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
from plugin_security import load_proxy_key, trusted_owner, session_key_v2

WEB = Path(__file__).resolve().parent / 'web'
TTL = 86400


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, engine, user, dev=False, proxy_key=''):
        self.engine, self.user, self.dev = engine, user, dev
        keyfile = engine.data / 'session.key'
        if not keyfile.exists():
            with keyfile.open('xb') as stream:
                os.chmod(keyfile, 0o600)
                stream.write(secrets.token_bytes(32))
        self.key = session_key_v2(keyfile.read_bytes(), 'qbittorrent', user)
        self.proxy_key = proxy_key
        self.logins = {}
        self.login_lock = threading.Lock()
        self.request_slots = threading.BoundedSemaphore(12)
        super().__init__(addr, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(25)

    def log_message(self, *args):
        pass

    def send(self, code, data, mime='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def sign(self, text):
        return hmac.new(self.server.key, text.encode(), hashlib.sha256).hexdigest()

    def session(self):
        token = self.headers.get('X-QB-Session', '')
        try:
            payload, signature = token.rsplit('.', 1)
            if len(token) > 200 or int(payload.split('.')[0]) < time.time():
                return None
            return token if secrets.compare_digest(signature, self.sign(payload)) else None
        except (ValueError, UnicodeError):
            return None

    def trusted(self):
        if self.server.dev:
            return True
        return trusted_owner(self.headers, self.server.user, self.server.proxy_key)

    def require(self, write=False):
        token = self.session()
        if not token:
            self.send(401, {'ok': False, 'error': '请从设备所有者的小米客户端重新打开插件'})
        elif write and not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.sign('csrf:' + token)):
            self.send(403, {'ok': False, 'error': '操作令牌失效，请重新打开插件'})
        else:
            return token
        return None

    def cookie(self, token):
        with self.server.login_lock:
            now = time.time()
            self.server.logins = {k: v for k, v in self.server.logins.items() if v[1] > now}
            return self.server.logins.get(token, ('', 0))[0]

    def call_qb(self, token, route, params=None, **kwargs):
        cookie = self.cookie(token)
        if not cookie:
            raise Error('请先登录 qBittorrent')
        code, body, _ = qb_request(route, params, cookie=cookie, **kwargs)
        if code in (401, 403):
            with self.server.login_lock:
                self.server.logins.pop(token, None)
            raise Error('qBittorrent 登录已过期，请重新登录')
        if code != 200:
            raise Error('qBittorrent 拒绝此操作（HTTP ' + str(code) + '）')
        return body

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path == '/healthz':
            return self.send(200, {'ok': True, 'version': VERSION})
        if route.path in ('/', '/index.html'):
            token = ''
            if self.trusted():
                payload = str(int(time.time()) + TTL) + '.' + secrets.token_hex(16)
                token = payload + '.' + self.sign(payload)
            html = (WEB / 'index.html').read_text().replace('__SESSION_TOKEN__', token).replace('__CSRF_TOKEN__', self.sign('csrf:' + token) if token else '')
            return self.send(200, html.encode(), 'text/html; charset=utf-8')
        if route.path in ('/app.bundle.js', '/styles.css'):
            file = WEB / route.path[1:]
            return self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0])
        if route.path == '/assets/qb.png':
            return self.send(200, (WEB / 'assets/qb.png').read_bytes(), 'image/png')
        token = self.require()
        if not token:
            return
        if not self.server.request_slots.acquire(False):
            return self.send(429, {'ok': False, 'error': '请求较多，请稍后重试'})
        try:
            query = parse_qs(route.query)
            if route.path == '/api/status':
                result = self.server.engine.snapshot()
                result['loggedIn'] = bool(self.cookie(token))
            elif route.path == '/api/browse':
                result = {'items': self.server.engine.browse(query.get('path', [''])[0])}
            elif route.path == '/api/torrents':
                result = {'items': json.loads(self.call_qb(token, 'torrents/info?limit=500&sort=added_on&reverse=true')),
                          'transfer': json.loads(self.call_qb(token, 'transfer/info'))}
            elif route.path == '/api/detail':
                key = torrent_hash(query.get('hash', [''])[0])
                result = {'files': json.loads(self.call_qb(token, 'torrents/files?hash=' + key)),
                          'properties': json.loads(self.call_qb(token, 'torrents/properties?hash=' + key))}
            elif route.path == '/api/limits':
                prefs = json.loads(self.call_qb(token, 'app/preferences'))
                result = {'download': prefs['dl_limit'] // 1024, 'upload': prefs['up_limit'] // 1024,
                          'active': prefs['max_active_downloads']}
            else:
                return self.send(404, {'ok': False, 'error': 'not found'})
            self.send(200, {'ok': True, **result})
        except (Error, ValueError, OSError, KeyError) as exc:
            self.send(400, {'ok': False, 'error': str(exc) if isinstance(exc, Error) else '读取失败，请检查存储或服务状态'})
        finally:
            self.server.request_slots.release()

    def do_POST(self):
        token = self.require(True)
        if not token:
            return
        if not self.server.request_slots.acquire(False):
            return self.send(429, {'ok': False, 'error': '请求较多，请稍后重试'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 3 * 1024 * 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise Error('请求格式无效或超过大小限制')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise Error('请求格式无效')
            action = urlsplit(self.path).path.removeprefix('/api/')
            if action.startswith('service/'):
                self.server.engine.launch(action.split('/')[1], data)
                return self.send(202, {'ok': True})
            if action == 'login':
                password = data.get('password')
                if not isinstance(password, str) or not 1 <= len(password) <= 200:
                    raise Error('请输入 qBittorrent 密码')
                code, body, header = qb_request('auth/login', {'username': 'admin', 'password': password})
                try:
                    cookie = SimpleCookie(header)
                    sid = cookie['SID'].value
                except (CookieError, KeyError):
                    sid = ''
                if code != 200 or body.strip() != b'Ok.' or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', sid):
                    raise Error('登录失败，密码错误或登录次数过多')
                self.cookie(token)
                with self.server.login_lock:
                    if len(self.server.logins) >= 64 and token not in self.server.logins:
                        raise Error('会话数量已达上限')
                    self.server.logins[token] = ('SID=' + sid, time.time() + TTL)
            elif action == 'logout':
                with self.server.login_lock:
                    self.server.logins.pop(token, None)
            elif action == 'torrent':
                value = data.get('content', '')
                if not isinstance(value, str):
                    raise Error('种子文件格式无效')
                try:
                    raw = base64.b64decode(value, validate=True)
                except ValueError as exc:
                    raise Error('种子文件格式无效') from exc
                if not 1 <= len(raw) <= 2 * 1024 * 1024 or not raw.startswith(b'd'):
                    raise Error('请选择不超过 2 MiB 的 torrent 文件')
                boundary = 'nasqb' + secrets.token_hex(24)
                body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="torrents"; filename="upload.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n').encode() + raw
                for key, value in [('savepath', '/downloads'), ('autoTMM', 'false'), ('stopped', 'false')]:
                    body += ('\r\n--' + boundary + '\r\nContent-Disposition: form-data; name="' + key + '"\r\n\r\n' + value).encode()
                body += ('\r\n--' + boundary + '--\r\n').encode()
                if self.call_qb(token, 'torrents/add', raw=body, content_type='multipart/form-data; boundary=' + boundary).strip() != b'Ok.':
                    raise Error('种子未被接受，请检查文件内容')
            else:
                target, params = mutation(action, data)
                body = self.call_qb(token, target, params)
                if action == 'magnet' and body.strip() != b'Ok.':
                    raise Error('磁力链接未被接受')
            self.send(200, {'ok': True})
        except (Error, ValueError, OSError) as exc:
            self.send(400, {'ok': False, 'error': str(exc) if isinstance(exc, Error) else '操作失败，请检查输入'})
        finally:
            self.server.request_slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dev', action='store_true')
    parser.add_argument('--stop-owned', action='store_true')
    args = parser.parse_args()
    data = os.environ.get('DATA_DIR') or (tempfile.mkdtemp(prefix='qb-preview-') if args.dev else '/data/plugin/qbittorrent/data')
    root = os.environ.get('LOCAL_ROOT', data if args.dev else '')
    user = os.environ.get('NAS_USER_ID', 'u123456' if args.dev else '')
    if not root or not re.fullmatch(r'u[0-9]+', user):
        raise SystemExit('LOCAL_ROOT and NAS_USER_ID are required')
    engine = Engine(data, root, args.dev)
    if args.stop_owned:
        engine.stop(remember=False)
        return
    server = Server(('127.0.0.1', int(os.environ.get('PORT', 18122))), engine, user, args.dev,
                    load_proxy_key(engine.data.parent / 'proxy.key'))
    if engine.config and engine.config.get('enabled') and not args.dev:
        engine.launch('start', {})
    print('qB plugin listening on http://127.0.0.1:' + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
