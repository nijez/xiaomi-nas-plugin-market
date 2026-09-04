"""Bounded qBittorrent container lifecycle and Web API adapter."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

VERSION = '0.1.0-rc1'
IMAGE = 'ghcr.io/linuxserver/qbittorrent@sha256:a00b6a597a3832a1814cde0ef60abc55c94644f3f80902c3432f6af6de8d4a96'
NAME = 'xiaomi-plugin-qbittorrent'
LABEL = 'io.xiaomi-plugin.qb.owner'
PORT = 18123


class Error(RuntimeError):
    pass


def password_hash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 200 or any(ord(c) < 32 for c in password):
        raise Error('密码须为 8 至 200 个字符')
    if sum(bool(re.search(p, password)) for p in (r'[A-Z]', r'[a-z]', r'[0-9]', r'[^A-Za-z0-9\s]')) < 2:
        raise Error('密码须至少包含大写、小写、数字、符号中的两种')
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac('sha512', password.encode(), salt, 100000, 64)
    return base64.b64encode(salt).decode() + ':' + base64.b64encode(key).decode()


def confined(root, relative):
    if not isinstance(relative, str) or relative.startswith('/') or '\\' in relative:
        raise Error('目录路径无效')
    current = Path(root)
    if current.is_symlink() or not current.is_dir():
        raise Error('用户存储不可用')
    for part in relative.split('/') if relative else []:
        if part in ('', '.', '..') or part.startswith('.') or any(ord(c) < 32 for c in part):
            raise Error('目录路径无效')
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise Error('目录不存在或为符号链接')
    current.resolve().relative_to(Path(root).resolve())
    return current


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream)
    tmp.replace(path)


def torrent_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', value):
        raise Error('任务标识无效')
    return value


def mutation(action, data):
    """No arbitrary qB API, save path, shell command, or deleteFiles input."""
    if action in ('start', 'stop', 'remove'):
        params = {'hashes': torrent_hash(data.get('hash'))}
        if action == 'remove':
            params['deleteFiles'] = 'false'
        return 'torrents/' + {'start': 'start', 'stop': 'stop', 'remove': 'delete'}[action], params
    if action == 'magnet':
        value = data.get('url', '')
        if not isinstance(value, str) or len(value) > 16384 or '\n' in value or '\r' in value:
            raise Error('磁力链接无效')
        parsed = urlsplit(value)
        xt = parse_qs(parsed.query).get('xt', [])
        if parsed.scheme != 'magnet' or not any(re.fullmatch(r'urn:btih:(?:[a-fA-F0-9]{40}|[A-Z2-7a-z]{32})|urn:btmh:1220[a-fA-F0-9]{64}', v) for v in xt):
            raise Error('请填写有效的磁力链接')
        return 'torrents/add', {'urls': value, 'savepath': '/downloads', 'autoTMM': 'false', 'stopped': 'false'}
    if action == 'limits':
        prefs = {}
        for source, target, maximum in [('download', 'dl_limit', 1048576), ('upload', 'up_limit', 1048576), ('active', 'max_active_downloads', 10)]:
            value = data.get(source)
            if type(value) is not int or not (1 if source == 'active' else 0) <= value <= maximum:
                raise Error('限速或并发数无效')
            prefs[target] = value * 1024 if source != 'active' else value
        prefs['queueing_enabled'] = True
        return 'app/setPreferences', {'json': json.dumps(prefs)}
    raise Error('不支持此操作')


def container_args(config, data):
    return ['run', '-d', '--name', NAME, '--label', LABEL + '=' + config['owner'],
            '--restart', 'no', '--memory', '512m', '--memory-swap', '512m', '--cpus', '1.5',
            '--pids-limit', '128', '--security-opt', 'no-new-privileges:true',
            '--log-opt', 'max-size=5m', '--log-opt', 'max-file=2',
            '-e', 'PUID=' + str(config['uid']), '-e', 'PGID=' + str(config['gid']),
            '-e', 'UMASK=077', '-e', 'TZ=Asia/Shanghai', '-e', 'WEBUI_PORT=' + str(PORT),
            '-p', '127.0.0.1:' + str(PORT) + ':' + str(PORT),
            '--mount', 'type=bind,src=' + str(data / 'config') + ',dst=/config',
            '--mount', 'type=bind,src=' + config['download'] + ',dst=/downloads', IMAGE]


class Engine:
    def __init__(self, data, root, dev=False):
        self.data, self.root, self.dev = Path(data), Path(root), dev
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.lock = threading.Lock()
        self.busy, self.error = False, ''
        self.worker = None
        self.cfgfile = self.data / 'settings.json'
        self.config = json.loads(self.cfgfile.read_text()) if self.cfgfile.exists() else None

    def docker(self, *args, timeout=30):
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        try:
            result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error('Docker 不可用或操作超时') from exc
        if result.returncode:
            raise Error('Docker 操作失败，请检查镜像网络、端口和可用资源；未修改其他容器')
        return result.stdout

    def owned(self):
        names = self.docker('ps', '-a', '--filter', 'name=^/' + NAME + '$', '--format', '{{.Names}}').splitlines()
        if NAME not in names:
            return None
        item = json.loads(self.docker('inspect', NAME))[0]
        if not self.config or item.get('Config', {}).get('Labels', {}).get(LABEL) != self.config['owner']:
            raise Error('同名容器不属于本插件，拒绝接管')
        return item

    def browse(self, relative):
        folder = confined(self.root, relative)
        return sorted([{'name': p.name, 'path': (relative + '/' if relative else '') + p.name}
                       for p in folder.iterdir() if not p.name.startswith('.') and p.is_dir() and not p.is_symlink()], key=lambda p: p['name'])[:1000]

    def snapshot(self):
        running = False
        error = self.error
        if self.config and not self.busy:
            try:
                item = self.owned()
                running = bool(item and item.get('State', {}).get('Running'))
            except Error as exc:
                error = str(exc)
        return {'version': VERSION, 'configured': bool(self.config), 'running': running,
                'busy': self.busy, 'error': error, 'preview': self.dev,
                'directory': self.config['relative'] if self.config else '', 'imageVersion': '5.2.3 / LSIO ls474'}

    def setup(self, relative, password):
        if self.config:
            raise Error('已完成初始化；现有目录和密码不会被覆盖')
        hashed = password_hash(password)
        if not relative:
            raise Error('请选择存储根目录下的文件夹')
        parent = confined(self.root, relative)
        if ',' in str(parent):
            raise Error('Docker 挂载目录不能包含逗号')
        # Only a new dedicated child is claimed; existing user data is never chowned.
        target = parent / 'qBDownloads'
        if target.exists() or target.is_symlink():
            raise Error('qBDownloads 已存在，请选择其他父目录，避免接管已有文件')
        uid, gid = parent.stat().st_uid, parent.stat().st_gid
        if not uid or not gid:
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        self.docker('info', '--format', '{{.Architecture}}')
        if NAME in self.docker('ps', '-a', '--format', '{{.Names}}').splitlines():
            raise Error('同名容器已存在，拒绝覆盖')
        target.mkdir(mode=0o700)
        os.chown(target, uid, gid)
        cfgdir = self.data / 'config' / 'qBittorrent'
        cfgdir.mkdir(parents=True, mode=0o700)
        for path in (cfgdir.parent, cfgdir):
            os.chown(path, uid, gid)
            os.chmod(path, 0o700)
        conf = ('[LegalNotice]\nAccepted=true\n[Network]\nPortForwardingEnabled=false\n'
                '[BitTorrent]\nSession\\DefaultSavePath=/downloads\nSession\\QueueingSystemEnabled=true\n'
                'Session\\MaxActiveDownloads=2\nSession\\MaxActiveTorrents=4\nSession\\MaxConnections=150\n'
                '[Preferences]\nWebUI\\Address=*\nWebUI\\Port=18123\nWebUI\\Username=admin\n'
                'WebUI\\Password_PBKDF2="@ByteArray(' + hashed + ')"\n'
                'WebUI\\LocalHostAuth=true\nWebUI\\AuthSubnetWhitelistEnabled=false\n'
                'WebUI\\CSRFProtection=true\nWebUI\\HostHeaderValidation=true\nWebUI\\UseUPnP=false\n')
        confpath = cfgdir / 'qBittorrent.conf'
        with confpath.open('x') as stream:
            os.chmod(confpath, 0o600)
            stream.write(conf)
        os.chown(confpath, uid, gid)
        stat = target.stat()
        self.config = {'owner': secrets.token_hex(24), 'relative': relative + '/qBDownloads',
                       'download': str(target), 'uid': uid, 'gid': gid, 'device': stat.st_dev,
                       'inode': stat.st_ino, 'enabled': True}
        atomic_json(self.cfgfile, self.config)

    def check_directory(self):
        folder = confined(self.root, self.config['relative'])
        stat = folder.stat()
        if str(folder) != self.config['download'] or (stat.st_dev, stat.st_ino) != (self.config['device'], self.config['inode']):
            raise Error('下载目录身份已变化，拒绝启动；请先检查存储挂载')

    def start(self):
        if not self.config:
            raise Error('请先初始化')
        self.check_directory()
        item = self.owned()
        if item:
            if not item.get('State', {}).get('Running'):
                self.docker('start', NAME)
        else:
            self.docker('pull', IMAGE, timeout=900)
            self.check_directory()
            self.docker(*container_args(self.config, self.data), timeout=90)
        self.config['enabled'] = True
        atomic_json(self.cfgfile, self.config)
        for _ in range(60):
            try:
                response, _, _ = qb_request('app/version')
                if response in (200, 403):
                    return
            except Error:
                pass
            time.sleep(1)
        raise Error('容器已启动，但 Web API 尚未就绪；可稍后刷新')

    def stop(self, remember=True):
        if not self.config:
            return
        item = self.owned()
        if item and item.get('State', {}).get('Running'):
            self.docker('stop', '--time', '15', NAME)
        if remember:
            self.config['enabled'] = False
            atomic_json(self.cfgfile, self.config)

    def launch(self, action, data):
        if self.dev:
            raise Error('预览模式不会启动下载或修改 NAS')
        if action not in ('setup', 'start', 'stop'):
            raise Error('未知服务操作')
        if not self.lock.acquire(False):
            raise Error('服务操作正在进行，请稍候')
        self.busy, self.error = True, ''
        def work():
            try:
                if action == 'setup':
                    self.setup(data.get('path', ''), data.get('password', ''))
                if action in ('setup', 'start'):
                    self.start()
                else:
                    self.stop()
            except Error as exc:
                self.error = str(exc)
            except Exception:
                self.error = '初始化失败；保留已有配置和文件，请检查目录权限'
            finally:
                self.busy = False
                self.lock.release()
        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()


def qb_request(route, params=None, cookie='', raw=None, content_type=None):
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=15)
    body = raw if raw is not None else urlencode(params).encode() if params is not None else None
    headers = {'Referer': 'http://127.0.0.1:' + str(PORT) + '/', 'Cookie': cookie,
               'Content-Type': content_type or 'application/x-www-form-urlencoded'}
    try:
        connection.request('POST' if body is not None else 'GET', '/api/v2/' + route, body, headers)
        response = connection.getresponse()
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise Error('任务数据过多，请减少单次查询')
        return response.status, data, response.getheader('Set-Cookie', '')
    except (OSError, http.client.HTTPException) as exc:
        raise Error('qBittorrent 未运行或尚未就绪') from exc
    finally:
        connection.close()
