#!/usr/bin/env python3
"""Read-only recovery checklist; does not modify config, grant access, or restart."""
import argparse
import json
import os
import pwd
import re
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project))
from access import AccessStore
from compatibility import runtime_issues

parser = argparse.ArgumentParser()
parser.add_argument('--data', default='/data/plugin/webdav/data')
parser.add_argument('--root')
parser.add_argument('--user', default=os.getenv('NAS_USER_ID', ''))
args = parser.parse_args()
user = args.user
if not user:
    unit = Path('/etc/systemd/system/xiaomi-webdav.service')
    match = re.search(r'Environment=NAS_USER_ID=(u[0-9]+)', unit.read_text()) if unit.exists() else None
    user = match.group(1) if match else ''
if not args.root and not re.fullmatch(r'u[0-9]+', user):
    raise SystemExit('无法识别安装账号，请指定 --user；没有修改任何设置')
if user and os.getuid() == 0:
    account = pwd.getpwnam(user)
    os.initgroups(user, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
root = Path(args.root or f'/nas/pool0/{user}/data')
issues = runtime_issues(project)
store = AccessStore(args.data, root)
issues += store.compatibility()
checks = {'runtimeAndPolicy': not issues, 'systemdUnitPresent': Path('/etc/systemd/system/xiaomi-webdav.service').is_file(),
          'nginxRoutePresent': Path('/etc/nginx/conf.d/luci/xiaomi-webdav.conf').is_file(),
          'policyPresent': store.file.is_file()}
print(json.dumps({'checks': checks, 'issues': issues, 'changedAnything': False}, ensure_ascii=False, indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
