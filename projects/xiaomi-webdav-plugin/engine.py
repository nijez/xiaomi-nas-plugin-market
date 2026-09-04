"""WebDAV configuration, confined file paths and supervised rclone processes."""
from __future__ import annotations

import copy
import ctypes
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit


class Error(Exception):
    pass


def validate_share_password(password):
    if (not isinstance(password, str) or not 8 <= len(password) <= 200
            or any(ord(c) < 32 or ord(c) == 127 for c in password)):
        raise Error('共享密码须为 8 至 200 个字符，不能包含控制字符')
    kinds = sum(bool(re.search(pattern, password)) for pattern in
                (r'[A-Z]', r'[a-z]', r'[0-9]', r'[!-/:-@\[-`{-~]'))
    if kinds < 2:
        raise Error('共享密码须包含英文大写、英文小写、数字、半角符号中的至少两种')


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def relative(value):
    value = str(value).strip().strip('/')
    if len(value) > 1024 or any(ord(c) < 32 for c in value) or '\\' in value:
        raise Error('目录格式不正确')
    if any(part in ('.', '..') for part in value.split('/')) or ':' in value:
        raise Error('目录不能包含 ..、冒号或特殊路径')
    if '.webdav-history' in value.split('/'):
        raise Error('历史版本目录不能作为任务目录')
    return value


def local_path(root, value):
    value = relative(value)
    candidate = root
    for part in value.split('/'):
        if part:
            candidate = candidate / part
            if candidate.is_symlink():
                raise Error('不能选择符号链接目录')
    if not candidate.is_dir() or not candidate.resolve().is_relative_to(root.resolve()):
        raise Error('请选择当前账号文件区内已经存在的目录')
    # Destination symlinks could otherwise redirect writes outside the selected tree.
    return candidate


def ensure_no_links(root):
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            if (Path(base) / name).is_symlink():
                raise Error('目标目录含符号链接，已停止写入。请选择独立的备份目录')


def folder_name(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or value.startswith('.') or value.endswith('.')
            or any(ord(c) < 32 or ord(c) == 127 or c in '/\\:*?"<>|' for c in value)
            or len(value.encode('utf-8')) > 255):
        raise Error('文件夹名称不能为空、以点开头或结尾，不能包含路径分隔符、特殊字符或首尾空格；最长 255 字节')
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        raise Error('该名称是 Windows 保留名称，请使用其他名称')
    return value


@contextmanager
def folder_fd(root, path):
    # Pin every directory component; do not follow links when mutating files.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in filter(None, relative(path).split('/')):
            if part.startswith('.'):
                raise Error('不能修改隐藏目录')
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def rename_folder_exclusive(fd, source, destination):
    # An existence check followed by os.rename could overwrite a competing folder.
    libc = ctypes.CDLL(None, use_errno=True)
    name, flag = ('renameatx_np', 4) if sys.platform == 'darwin' else ('renameat2', 1)
    rename = getattr(libc, name, None)
    if rename is None:
        raise Error('当前系统不支持无覆盖重命名，已取消操作')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(fd, os.fsencode(source), fd, os.fsencode(destination), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def text(value, label, limit=160):
    value = str(value).strip()
    if not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise Error(label + '不能为空或包含控制字符')
    return value


def remote_config(body, old=None):
    old = old or {}
    url = text(body.get('url', ''), '服务器地址', 2048)
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise Error('端口格式不正确')
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise Error('请输入 http(s) WebDAV 地址，不要在 URL 中放账号、密码或查询参数')
    if any(p in ('.', '..') for p in unquote(parsed.path).split('/')):
        raise Error('服务器路径不正确')
    if parsed.scheme == 'http' and body.get('allowHttp') is not True:
        raise Error('HTTP 会明文传输密码，必须明确确认后才能使用')
    password = body.get('password') or old.get('password', '')
    if not isinstance(password, str) or not password or len(password) > 2048 or '\n' in password:
        raise Error('请输入 WebDAV 密码或应用专用密码')
    username = text(body.get('username', ''), '用户名')
    if ':' in username:
        raise Error('用户名不能包含冒号')
    return {'id': old.get('id', secrets.token_hex(8)), 'name': text(body.get('name', ''), '连接名称'),
            'url': url.rstrip('/') + '/', 'username': username, 'password': password,
            'allowHttp': parsed.scheme == 'http'}


def addresses():
    result = []
    if not sys.platform.startswith('linux'):
        return result
    try:
        import fcntl
        # The NAS ships BusyBox ip, which lacks JSON output. Query Linux directly.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for _, name in socket.if_nameindex():
                if name.startswith(('lo', 'docker', 'br-', 'veth')):
                    continue
                try:
                    raw = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack('256s', name.encode()[:15]))
                    address = ipaddress.ip_address(socket.inet_ntoa(raw[20:24]))
                    if address.is_private and not address.is_loopback and not address.is_link_local:
                        result.append(str(address))
                except OSError:
                    continue
    except (OSError, ValueError):
        pass
    return sorted(set(result))


class Engine:
    def __init__(self, data, root, binary, share_port=18121, share_host=None):
        self.data, self.root, self.binary = Path(data), Path(root), str(binary)
        self.port, self.share_host = share_port, share_host
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.process = None
        self.share = None
        self.multi = None
        self.multi_error = ''
        self.cancelled = False
        self.active_id = None
        self.share_error = ''
        self.progress = {}
        config = self.data / 'config.json'
        self.config = json.loads(config.read_text()) if config.exists() else {
            'remotes': [], 'jobs': [], 'share': {'enabled': False, 'path': 'MiShare',
            'username': 'nas', 'password': '', 'readOnly': True}}
        for job in self.config['jobs']:
            if job.get('status') == 'running':
                job.update(status='interrupted', error='服务重启，任务未完成；可以重新运行')
        self.save()
        from access import AccessStore
        self.access = AccessStore(self.data, self.root)

    def save(self):
        atomic_json(self.data / 'config.json', self.config)

    def base_env(self):
        # Ignore inherited rclone/proxy settings; credentials never appear in argv or logs.
        env = {k: v for k, v in os.environ.items() if not k.startswith('RCLONE_') and 'proxy' not in k.lower()}
        env.update(RCLONE_CONFIG=os.devnull, RCLONE_CACHE_DIR=str(self.data / 'cache'),
                   GOMEMLIMIT='128MiB', GOMAXPROCS='2')
        return env

    def env(self, remote):
        env = self.base_env()
        result = subprocess.run([self.binary, 'obscure', '-'], input=remote['password'],
                                text=True, capture_output=True, timeout=5, env=env)
        if result.returncode:
            raise Error('无法初始化传输引擎')
        env.update(RCLONE_CONFIG_REMOTE_TYPE='webdav', RCLONE_CONFIG_REMOTE_VENDOR='other',
                   RCLONE_CONFIG_REMOTE_URL=remote['url'], RCLONE_CONFIG_REMOTE_USER=remote['username'],
                   RCLONE_CONFIG_REMOTE_PASS=result.stdout.strip())
        return env

    def get_remote(self, ident):
        for item in self.config['remotes']:
            if item['id'] == ident:
                return copy.deepcopy(item)
        raise Error('连接不存在')

    def browse(self, location, path='', remote_id=''):
        path = relative(path)
        if location == 'local':
            folder = local_path(self.root, path)
            entries = []
            for item in folder.iterdir():
                if item.name.startswith('.') or item.is_symlink() or not item.is_dir():
                    continue
                item_path = item.relative_to(self.root).as_posix()
                entries.append({'name': item.name, 'path': item_path,
                                'renameBlocked': self.rename_blocked(item_path)})
                if len(entries) > 2000:
                    raise Error('子目录超过 2000 个，请输入更具体的路径')
            return sorted(entries, key=lambda e: e['name'].casefold())
        remote = self.get_remote(remote_id)
        # Bounded output and wall time, so a huge directory cannot exhaust NAS memory.
        with __import__('tempfile').TemporaryFile() as output:
            command = [self.binary, 'lsjson', 'remote:' + path, '--dirs-only', '--max-depth', '1',
                       '--contimeout', '8s', '--timeout', '15s', '--retries', '1']
            process = subprocess.Popen(command, env=self.env(remote), stdout=output, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 25
                while process.poll() is None:
                    if time.monotonic() > deadline or os.fstat(output.fileno()).st_size > 2_000_000:
                        raise Error('连接超时或目录过大，请检查地址并缩小目录范围')
                    time.sleep(0.1)
                if process.returncode:
                    raise Error('连接失败：请检查地址、账号、目录权限和 HTTPS 证书')
                output.seek(0)
                values = json.loads(output.read(2_000_001))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        return [{'name': x['Name'], 'path': relative('/'.join(filter(None, [path, x['Name']])))}
                for x in values if x.get('IsDir') and x['Name'] != '.webdav-history'][:2000]

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.config)
            for item in result['remotes']:
                item['hasPassword'] = bool(item.pop('password', ''))
            result['share']['hasPassword'] = bool(result['share'].pop('password', ''))
            result['share'].update(running=self.share is not None and self.share.poll() is None,
                                   error=self.share_error, port=self.port,
                                   urls=[f'https://{a}:{self.port}/' for a in addresses()],
                                   certificateReady=(self.data / 'ca.crt').exists())
            result['access'] = self.access.snapshot()
            result['access'].update(running=self.multi is not None and self.multi.poll() is None,
                                    error=self.multi_error, port=self.port,
                                    urls=[f'https://{a}:{self.port}/' for a in addresses()],
                                    migration=self.access.migration_preview(self.config['share']))
            result.update(version='0.2.0-rc5', root=str(self.root), progress=self.progress, activeId=self.active_id)
            return result

    def mutate(self, action, body):
        with self.lock:
            if action in ('folder/create', 'folder/rename'):
                self.change_folder(action, body)
                return
            if action.startswith('access/'):
                command = action[len('access/'):]
                if command == 'import-legacy':
                    self.access.import_legacy(self.config['share'], body.get('confirm') is True)
                elif command == 'start':
                    if self.config['share']['enabled'] or self.share and self.share.poll() is None:
                        raise Error('请先关闭原单账号共享；两种模式不能同时使用同一端口')
                    self.access.mutate('start', body)
                    try:
                        self.start_multi()
                    except (Error, OSError, subprocess.SubprocessError):
                        self.access.mutate('stop', {})
                        raise Error('多用户共享启动失败，已保持关闭；请检查兼容性、依赖和端口')
                elif command == 'stop':
                    self.stop_multi()
                    self.access.mutate('stop', body)
                else:
                    self.access.mutate(command, body)
                return
            if action == 'remote/save':
                if self.active_id:
                    raise Error('请等待当前任务结束后修改连接')
                old = self.get_remote(body['id']) if body.get('id') else None
                item = remote_config(body, old)
                if not old and len(self.config['remotes']) >= 20:
                    raise Error('最多保存 20 个连接')
                self.config['remotes'] = [x for x in self.config['remotes'] if x['id'] != item['id']] + [item]
            elif action == 'remote/remove':
                if any(j['remoteId'] == body.get('id') for j in self.config['jobs']):
                    raise Error('请先移除使用该连接的任务')
                self.config['remotes'] = [x for x in self.config['remotes'] if x['id'] != body.get('id')]
            elif action == 'job/save':
                self.save_job(body)
            elif action == 'job/remove':
                if self.active_id == body.get('id'):
                    raise Error('请先停止正在运行的任务')
                self.config['jobs'] = [x for x in self.config['jobs'] if x['id'] != body.get('id')]
            elif action == 'job/run':
                self.start_job(body.get('id'))
            elif action == 'job/cancel':
                self.cancelled = True
                if self.process and self.process.poll() is None:
                    self.process.terminate()
            elif action == 'share/save':
                if self.share and self.share.poll() is None:
                    raise Error('请先关闭共享，再修改设置')
                cfg = self.config['share']
                path = relative(body.get('path', ''))
                if not path:
                    raise Error('不能共享整个账号根目录，请选择一个子目录')
                local_path(self.root, path)
                password = body.get('password', '')
                if password == '':
                    password = cfg['password']
                    if not password:
                        raise Error('请先设置共享密码')
                else:
                    validate_share_password(password)
                username = text(body.get('username', ''), '用户名', 64)
                if not re.fullmatch(r'[A-Za-z0-9_-]+', username):
                    raise Error('共享用户名仅支持英文、数字、下划线和短横线')
                self.config['share'] = {'path': path, 'username': username, 'password': password,
                                        'readOnly': body.get('readOnly') is not False, 'enabled': False}
            elif action == 'share/start':
                self.start_share()
                self.config['share']['enabled'] = True
            elif action == 'share/stop':
                self.stop_share()
                self.config['share']['enabled'] = False
            else:
                raise Error('未知操作')
            self.save()

    def rename_blocked(self, path):
        if not path or '/' not in path:
            return '账号根目录及一级目录不能在此重命名'
        if self.active_id:
            return '备份任务运行中，请结束后再重命名'
        if self.config['share']['enabled'] or self.access.config['enabled']:
            return '请先关闭共享，再重命名文件夹'
        refs = [self.config['share'].get('path', '')]
        refs += [item['path'] for item in self.access.config['folders']]
        refs += [job.get('local', '') for job in self.config['jobs']]
        if any(ref == path or ref.startswith(path + '/') for ref in refs if ref):
            return '此目录或其子目录被共享或备份任务引用，请先调整相关配置'
        return ''

    def change_folder(self, action, body):
        path = relative(body.get('path', ''))
        name = folder_name(body.get('name'))
        local_path(self.root, path)
        if action == 'folder/rename':
            reason = self.rename_blocked(path)
            if reason:
                raise Error(reason)
            parent, old = path.rsplit('/', 1)
            folder_name(old)
            if name == old:
                raise Error('新名称与原名称相同')
        else:
            parent = path
            if self.active_id:
                raise Error('备份任务运行中，请结束后再创建文件夹')
        try:
            with folder_fd(self.root, parent) as fd:
                if action == 'folder/create':
                    os.mkdir(name, mode=0o755, dir_fd=fd)
                else:
                    import stat
                    if not stat.S_ISDIR(os.stat(old, dir_fd=fd, follow_symlinks=False).st_mode):
                        raise Error('只能重命名普通文件夹')
                    rename_folder_exclusive(fd, old, name)
        except FileExistsError:
            raise Error('同名文件或文件夹已存在，不会覆盖，请更换名称')
        except PermissionError:
            raise Error('当前账号没有该目录的写入权限')

    def save_job(self, body):
        ident = body.get('id') or secrets.token_hex(8)
        if self.active_id == ident:
            raise Error('运行中的任务不能修改')
        self.get_remote(body.get('remoteId'))
        local = relative(body.get('local', ''))
        if not local:
            raise Error('请选择子目录，不允许以整个文件区作为任务目录')
        local_path(self.root, local)
        remote = relative(body.get('remote', ''))
        direction = body.get('direction')
        if direction not in ('upload', 'download'):
            raise Error('请选择上传或下载')
        interval = int(body.get('interval', 0))
        bandwidth = int(body.get('bandwidth', 0))
        if interval not in (0, 15, 60, 360, 1440) or not 0 <= bandwidth <= 1000:
            raise Error('频率或限速数值不正确')
        old = next((x for x in self.config['jobs'] if x['id'] == ident), {})
        if not old and len(self.config['jobs']) >= 50:
            raise Error('最多保存 50 个任务')
        item = dict(old, id=ident, name=text(body.get('name', ''), '任务名称'), local=local, remote=remote,
                    remoteId=body['remoteId'], direction=direction, interval=interval, bandwidth=bandwidth,
                    nextRun=time.time() + interval * 60 if interval else 0, status=old.get('status', 'idle'))
        self.config['jobs'] = [x for x in self.config['jobs'] if x['id'] != ident] + [item]

    def command(self, job, run_id):
        local = str(local_path(self.root, job['local']))
        remote = 'remote:' + relative(job['remote'])
        src, dest = (local, remote) if job['direction'] == 'upload' else (remote, local)
        if job['direction'] == 'download':
            ensure_no_links(Path(local))
        # Old destination versions are retained; copy never propagates source deletions.
        backup = dest.rstrip('/') + '/.webdav-history/' + run_id
        command = [self.binary, 'copy', src, dest, '--backup-dir', backup,
                   '--exclude', '/.webdav-history/**', '--exclude', '**/.webdav-history/**', '--exclude', '**/.DS_Store',
                   '--transfers', '2', '--checkers', '2', '--buffer-size', '4M', '--multi-thread-streams', '0',
                   '--contimeout', '10s', '--timeout', '60s', '--retries', '2', '--low-level-retries', '2',
                   '--stats', '2s', '--stats-log-level', 'NOTICE', '--use-json-log', '--log-level', 'NOTICE']
        if job['bandwidth']:
            command += ['--bwlimit', str(job['bandwidth']) + 'M']
        return command

    def start_job(self, ident):
        if self.active_id:
            raise Error('已有任务运行中，请等待完成或停止后重试')
        job = next((x for x in self.config['jobs'] if x['id'] == ident), None)
        if not job:
            raise Error('任务不存在')
        self.command(job, 'validation')
        self.active_id, self.cancelled, self.progress = ident, False, {}
        job.update(status='running', error='', startedAt=time.time())
        self.save()
        self.worker = threading.Thread(target=self.run_job, args=(copy.deepcopy(job),), daemon=True)
        self.worker.start()

    def run_job(self, job):
        outcome, error, proc = 'failed', '', None
        run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4)
        try:
            command = self.command(job, run_id)
            env = self.env(self.get_remote(job['remoteId']))
            with self.lock:
                if self.cancelled:
                    raise Error('已停止')
                proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                self.process = proc
            for line in proc.stderr:
                try:
                    record = json.loads(line)
                    stats = record.get('stats')
                    if stats:
                        with self.lock:
                            self.progress = {k: stats.get(k, 0) for k in ('bytes', 'totalBytes', 'speed', 'transfers', 'errors', 'checks')}
                except (ValueError, TypeError):
                    pass
            code = proc.wait()
            proc.stderr.close()
            outcome = 'success' if code == 0 else 'failed'
            if code:
                error = f'传输未完成（退出码 {code}），请检查网络、证书、目录权限和容量；可重新运行'
        except (Error, OSError, subprocess.SubprocessError):
            error = '传输未完成，请检查目录、连接或引擎状态'
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait()
            with self.lock:
                current = next(x for x in self.config['jobs'] if x['id'] == job['id'])
                current.update(status='cancelled' if self.cancelled else outcome, error='' if self.cancelled else error,
                               finishedAt=time.time(), history=run_id,
                               nextRun=time.time() + current['interval'] * 60 if current['interval'] else 0)
                self.process, self.active_id = None, None
                self.save()

    def certificates(self, ips):
        ca, key = self.data / 'ca.crt', self.data / 'ca.key'
        def run(args):
            subprocess.run(['openssl'] + args, cwd=self.data, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        if not ca.exists():
            cfg = self.data / 'ca.cnf'
            cfg.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n[dn]\nCN=NAS WebDAV ' +
                           secrets.token_hex(6) + '\n[ca]\nbasicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n')
            run(['req', '-new', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650',
                 '-keyout', str(key), '-out', str(ca), '-config', str(cfg)])
        names = ['IP:127.0.0.1'] + ['IP:' + str(ipaddress.ip_address(a)) for a in ips]
        hostname = socket.gethostname()
        if re.fullmatch(r'[A-Za-z0-9.-]+', hostname):
            names.append('DNS:' + hostname)
        ext = self.data / 'server.ext'
        ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=' + ','.join(names) + '\n')
        run(['req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=NAS WebDAV',
             '-keyout', 'server.key', '-out', 'server.csr'])
        run(['x509', '-req', '-in', 'server.csr', '-CA', str(ca), '-CAkey', str(key),
             '-set_serial', str(secrets.randbits(120)), '-days', '365', '-extfile', str(ext), '-out', 'server.crt'])
        for path in self.data.glob('*.key'):
            os.chmod(path, 0o600)

    def start_share(self):
        if self.access.config['enabled']:
            raise Error('请先关闭多用户共享')
        cfg = self.config['share']
        if not cfg['password']:
            raise Error('请先保存共享目录、用户名和强密码')
        if self.share and self.share.poll() is None:
            return
        folder = local_path(self.root, cfg['path'])
        if not cfg['readOnly']:
            ensure_no_links(folder)
        ips = addresses()
        if not ips and self.share_host is None:
            raise Error('未发现可用的局域网 IPv4 地址')
        self.certificates(ips)
        env = self.base_env()
        env.update(RCLONE_USER=cfg['username'], RCLONE_PASS=cfg['password'])
        cmd = [self.binary, 'serve', 'webdav', str(folder), '--cert', str(self.data / 'server.crt'),
               '--key', str(self.data / 'server.key'), '--min-tls-version', 'tls1.2', '--buffer-size', '2M',
               '--vfs-cache-mode', 'off', '--dir-cache-time', '5s', '--disable-zip', '--exclude', '**/.webdav-history/**']
        for host in ([self.share_host] if self.share_host else ips):
            cmd += ['--addr', f'{host}:{self.port}']
        if cfg['readOnly']:
            cmd += ['--read-only']
        self.share = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        if self.share.poll() is not None:
            self.share_error = '启动失败，请检查端口冲突、证书和目录权限'
            raise Error(self.share_error)
        self.share_error = ''
        self.shared_addresses = ips

    def stop_share(self):
        if self.share and self.share.poll() is None:
            self.share.terminate()
            try:
                self.share.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.share.kill()
                self.share.wait()
        self.share = None

    def tick(self):
        with self.lock:
            if self.access.config['enabled']:
                if self.config['share']['enabled']:
                    self.stop_share()
                    self.stop_multi()
                    self.multi_error = '两种共享模式同时启用，已停止共享；请核对配置'
                    return
                issues = self.access.compatibility()
                if issues:
                    self.stop_multi()
                    self.multi_error = '；'.join(issues)
                else:
                    current = addresses()
                    if self.multi and current != getattr(self, 'multi_addresses', []):
                        self.stop_multi()
                    if not self.multi or self.multi.poll() is not None:
                        try:
                            self.start_multi()
                        except (Error, OSError, subprocess.SubprocessError):
                            self.multi_error = '共享启动失败，请检查兼容性、依赖和端口'
            if not self.active_id:
                for job in self.config['jobs']:
                    if job['interval'] and job.get('nextRun', 0) <= time.time():
                        try:
                            self.start_job(job['id'])
                        except Error as e:
                            job.update(status='failed', error=str(e), nextRun=time.time() + job['interval'] * 60)
                            self.save()
                        break
            if self.config['share']['enabled']:
                current = addresses()
                if self.share and self.share.poll() is None and current != getattr(self, 'shared_addresses', []):
                    self.stop_share()
                if not self.share or self.share.poll() is not None:
                    try:
                        self.start_share()
                    except (Error, OSError, subprocess.SubprocessError):
                        self.share_error = '共享未运行，将自动重试；请检查局域网、目录和端口'

    def supervise(self):
        while not self.stop_event.wait(15):
            self.tick()

    def start_multi(self):
        if self.config['share']['enabled'] or self.access.compatibility():
            raise Error('原共享运行中或兼容性检查失败')
        if self.multi and self.multi.poll() is None:
            return
        ips = addresses()
        if not ips and self.share_host is None:
            raise Error('未发现可用的局域网 IPv4 地址')
        self.certificates(ips)
        cmd = [sys.executable, str(Path(__file__).with_name('multi_dav.py')), '--data', str(self.data),
               '--root', str(self.root), '--port', str(self.port)]
        hosts = [self.share_host] if self.share_host else ips
        for host in hosts:
            cmd += ['--host', host]
        self.multi = subprocess.Popen(cmd, env=self.base_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Import and integrity-check latency differs on NAS flash; require real TLS readiness.
        context = ssl.create_default_context(cafile=str(self.data / 'ca.crt'))
        pending = set(hosts)
        deadline = time.monotonic() + 20
        while pending and time.monotonic() < deadline and self.multi.poll() is None:
            for host in list(pending):
                connection = http.client.HTTPSConnection(host, self.port, timeout=1, context=context)
                try:
                    connection.request('OPTIONS', '/')
                    response = connection.getresponse()
                    if response.status == 401 and 'NAS WebDAV' in response.getheader('WWW-Authenticate', ''):
                        pending.remove(host)
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
            if pending:
                time.sleep(0.1)
        if pending or self.multi.poll() is not None:
            self.stop_multi()
            raise Error('多用户共享未通过 HTTPS 就绪检查，已停止')
        self.multi_addresses, self.multi_error = ips, ''

    def stop_multi(self):
        if self.multi and self.multi.poll() is None:
            self.multi.terminate()
            try:
                self.multi.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.multi.kill()
                self.multi.wait()
        self.multi = None

    def close(self):
        self.stop_event.set()
        with self.lock:
            self.cancelled = True
            if self.process and self.process.poll() is None:
                self.process.terminate()
            self.stop_share()
            self.stop_multi()
        if self.worker:
            self.worker.join(timeout=10)
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait()
