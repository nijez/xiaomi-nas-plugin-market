"""Versioned, default-deny WebDAV grants independent of Xiaomi system accounts."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from pathlib import Path

from engine import Error, atomic_json, local_path, relative, text, validate_share_password

SCHEMA = 1
ROUNDS = 600_000


def host_identity(root):
    root = Path(root)
    info = root.stat()
    fingerprint = hashlib.sha256((sys.version + os.uname().release).encode())
    for name in ('/etc/os-release', '/etc/minas.version', '/etc/mi_version', '/etc/nas_version', '/etc/version'):
        release = Path(name)
        if release.is_file():
            fingerprint.update(name.encode() + release.read_bytes())
    firmware = fingerprint.hexdigest()
    return {'root': str(root.resolve()), 'device': info.st_dev, 'inode': info.st_ino,
            'uid': os.getuid(), 'firmware': firmware}


def folder_identity(root, path):
    folder = local_path(root, path)
    st = folder.stat()
    return {'device': st.st_dev, 'inode': st.st_ino}


def password_hash(value):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', value.encode(), salt, ROUNDS)
    return f'pbkdf2-sha256${ROUNDS}${salt.hex()}${digest.hex()}'


def verify_password(value, encoded):
    try:
        method, rounds, salt, digest = encoded.split('$')
        if method != 'pbkdf2-sha256' or int(rounds) != ROUNDS:
            return False
        expected = bytes.fromhex(digest)
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), ROUNDS), expected)
    except (ValueError, TypeError):
        return False


class AccessStore:
    def __init__(self, data, root):
        self.file = Path(data) / 'permissions.json'
        self.root = Path(root)
        self.error = ''
        self.config = {'schema': SCHEMA, 'revision': 0, 'enabled': False,
                       'acceptedHost': None, 'folders': [], 'users': []}
        if self.file.exists():
            try:
                cfg = json.loads(self.file.read_text())
                self.validate(cfg)
                self.config = cfg
            except (Error, ValueError, KeyError, TypeError, OSError):
                self.error = '权限文件损坏或版本不兼容；共享已拒绝启动，原文件未覆盖'

    @staticmethod
    def validate(cfg):
        if not isinstance(cfg, dict) or cfg.get('schema') != SCHEMA or type(cfg.get('enabled')) is not bool:
            raise Error('权限配置版本不兼容')
        if type(cfg.get('revision')) is not int or cfg['revision'] < 0:
            raise Error('权限配置修订号无效')
        host = cfg['acceptedHost']
        if host is not None and (not isinstance(host, dict)
                or set(host) != {'root', 'device', 'inode', 'uid', 'firmware'}
                or not isinstance(host['root'], str) or not Path(host['root']).is_absolute()
                or any(type(host[k]) is not int for k in ('device', 'inode', 'uid'))
                or not isinstance(host['firmware'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', host['firmware'])):
            raise Error('运行环境身份无效')
        folders, users = cfg['folders'], cfg['users']
        if not isinstance(folders, list) or not isinstance(users, list) or len(folders) > 32 or len(users) > 50:
            raise Error('最多 32 个目录、50 个账号')
        ids, paths = set(), []
        for folder in folders:
            ident, path = folder['id'], folder['path']
            if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', ident) or ident.lower() in ids:
                raise Error('目录标识不能重复，仅支持英文、数字、下划线和短横线')
            ids.add(ident.lower())
            if not isinstance(path, str) or not path or path != relative(path):
                raise Error('必须选择账号文件区中的子目录')
            if any(path == p or path.startswith(p + '/') or p.startswith(path + '/') for p in paths):
                raise Error('共享目录不能重叠，请避免同时授权父目录与子目录')
            paths.append(path)
            text(folder['name'], '目录名称')
            for k in ('device', 'inode'):
                if type(folder['identity'][k]) is not int:
                    raise Error('目录身份无效')
        exact_ids = {f['id'] for f in folders}
        names = set()
        for user in users:
            name = user['username']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or name.lower() in names:
                raise Error('账号不能重复，仅支持英文、数字、下划线和短横线')
            names.add(name.lower())
            if type(user['enabled']) is not bool or not isinstance(user['grants'], dict):
                raise Error('账号授权格式无效')
            if any(k not in exact_ids or v not in ('read', 'write') for k, v in user['grants'].items()):
                raise Error('授权包含不存在的目录或无效权限')
            if not re.fullmatch(r'pbkdf2-sha256\$600000\$[a-f0-9]{32}\$[a-f0-9]{64}', user['passwordHash']):
                raise Error('账号密码校验数据无效')

    def commit(self, cfg):
        if self.error:
            raise Error(self.error)
        self.validate(cfg)
        cfg['revision'] = self.config['revision'] + 1
        atomic_json(self.file, cfg)
        self.config = cfg

    def compatibility(self):
        issues = []
        if self.error:
            issues.append(self.error)
        try:
            if self.config['acceptedHost'] and self.config['acceptedHost'] != host_identity(self.root):
                issues.append('固件、运行用户或存储位置发生变化，需要重新确认兼容性')
            for folder in self.config['folders']:
                if folder_identity(self.root, folder['path']) != folder['identity']:
                    issues.append('已授权目录被替换，请重新选择并核对目录')
        except (OSError, Error):
            issues.append('已授权存储目录不可用，共享保持关闭')
        return list(dict.fromkeys(issues))

    def snapshot(self):
        result = copy.deepcopy(self.config)
        result.pop('acceptedHost', None)
        for user in result['users']:
            user['hasPassword'] = bool(user.pop('passwordHash'))
        for folder in result['folders']:
            folder.pop('identity', None)
        result.update(issues=self.compatibility())
        return result

    def mutate(self, action, body):
        if self.error:
            raise Error(self.error)
        cfg = copy.deepcopy(self.config)
        if cfg['enabled'] and action != 'stop':
            raise Error('请先关闭多用户共享，再修改授权')
        if action == 'folder/save':
            ident = text(body.get('id', ''), '目录标识', 48)
            path = relative(body.get('path', ''))
            folder = {'id': ident, 'name': text(body.get('name', ''), '目录名称'), 'path': path,
                      'identity': folder_identity(self.root, path)}
            cfg['folders'] = [f for f in cfg['folders'] if f['id'] != ident] + [folder]
        elif action == 'folder/remove':
            ident = body.get('id')
            cfg['folders'] = [f for f in cfg['folders'] if f['id'] != ident]
            for user in cfg['users']:
                user['grants'].pop(ident, None)
        elif action == 'user/save':
            name = text(body.get('username', ''), '账号', 64)
            old = next((u for u in cfg['users'] if u['username'] == name), {})
            password = body.get('password', '')
            if password != '':
                validate_share_password(password)
                encoded = password_hash(password)
            elif old:
                encoded = old['passwordHash']
            else:
                raise Error('新账号必须设置密码')
            user = {'username': name, 'enabled': body.get('enabled') is True,
                    'grants': body.get('grants', {}), 'passwordHash': encoded}
            cfg['users'] = [u for u in cfg['users'] if u['username'] != name] + [user]
        elif action == 'user/remove':
            cfg['users'] = [u for u in cfg['users'] if u['username'] != body.get('username')]
        elif action == 'accept-host':
            if body.get('confirm') is not True:
                raise Error('必须确认固件和存储身份变化')
            # Root identity can change after a remount; each directory still needs to match.
            for folder in cfg['folders']:
                if folder_identity(self.root, folder['path']) != folder['identity']:
                    raise Error('请先重新选择发生变化的目录')
            cfg['acceptedHost'] = host_identity(self.root)
        elif action == 'stop':
            cfg['enabled'] = False
        elif action == 'start':
            issues = self.compatibility()
            if issues:
                raise Error('；'.join(issues))
            if not cfg['acceptedHost'] or not any(u['enabled'] and u['grants'] for u in cfg['users']):
                raise Error('请先配置目录和至少一个有效授权账号')
            cfg['enabled'] = True
        else:
            raise Error('未知授权操作')
        if not cfg['acceptedHost'] and action in ('folder/save', 'user/save'):
            cfg['acceptedHost'] = host_identity(self.root)
        self.commit(cfg)

    def migration_preview(self, legacy):
        return {'available': bool(legacy.get('password')) and not self.config['users'] and not self.config['folders'],
                'username': legacy.get('username', ''), 'path': legacy.get('path', ''),
                'permission': 'read' if legacy.get('readOnly', True) else 'write',
                'newPath': '/legacy/', 'requiresStop': bool(legacy.get('enabled'))}

    def import_legacy(self, legacy, confirm=False):
        if not confirm or legacy.get('enabled') or not self.migration_preview(legacy)['available']:
            raise Error('导入需明确确认、关闭原共享，并保持新权限表为空')
        cfg = copy.deepcopy(self.config)
        cfg['folders'] = [{'id': 'legacy', 'name': '原共享目录', 'path': legacy['path'],
                           'identity': folder_identity(self.root, legacy['path'])}]
        cfg['users'] = [{'username': legacy['username'], 'enabled': True,
                         'passwordHash': password_hash(legacy['password']),
                         'grants': {'legacy': 'read' if legacy['readOnly'] else 'write'}}]
        cfg['acceptedHost'] = host_identity(self.root)
        self.commit(cfg)
