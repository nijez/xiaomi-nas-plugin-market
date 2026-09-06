"""Release privacy, ZIP, signature and checksum checks; no network or NAS writes."""
import hashlib
import argparse
import io
import json
import re
import sys
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / 'projects/xiaomi-community-app-store'
sys.path.insert(0, str(STORE))
from storelib import load_verified_catalog, safe_extract_bundle, validate_manifest, verify_detached_signature

PATTERNS = {
    'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
    'personal macOS path': rb'/Users/[A-Za-z0-9_-]+/',
    'private device address': rb'\b10\.0\.0\.[0-9]{1,3}\b',
    'device certificate identity': rb'nas\.[0-9]{5,}\.[a-f0-9]{32,}',
    'GitHub token': rb'(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{25,}',
    'API token': rb'sk-[A-Za-z0-9_-]{32,}',
}
FORBIDDEN = {'secrets', '.env', 'admin-token', 'session.key', 'permissions.json', 'config.json', 'settings.json',
             'nas-root-key', 'signing-key.pem', 'ca.key', 'server.key'}
EXCLUDED = {'.git', '__pycache__', 'node_modules', 'test-results', 'artifacts'}
findings = []
RCLONE_SHA256 = 'a7094d6e48c6c26cb069175ae93ee221db7dabfa18f57cb6bf3d3d5e1fb1cf3a'


def inspect(name, body, depth=0):
    parts = PurePosixPath(name).parts
    if set(parts) & FORBIDDEN or name.startswith('/') or '..' in parts:
        findings.append((name, 'forbidden distribution path'))
    if name.endswith(('.pyc', '.so', '.pyd')):
        findings.append((name, 'platform or bytecode artifact'))
    if name.endswith('.zip'):
        if depth > 2:
            raise RuntimeError('Nested archive limit exceeded')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            assert sum(x.file_size for x in archive.infolist()) < 512 * 1024 * 1024
            assert archive.testzip() is None
            for entry in archive.infolist():
                if not entry.is_dir():
                    assert not PurePosixPath(entry.filename).is_absolute() and '..' not in PurePosixPath(entry.filename).parts
                    inspect(name + '!' + entry.filename, archive.read(entry), depth + 1)
        return
    if name.endswith('/bin/rclone'):
        assert hashlib.sha256(body).hexdigest() == RCLONE_SHA256, 'Unexpected rclone binary'
        # The verified Go binary embeds PEM parser markers, not a personal key.
        assert not re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s+'
                             rb'[A-Za-z0-9+/=\r\n]{48,}-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----', body)
    for kind, pattern in PATTERNS.items():
        if kind == 'private key' and name.endswith('/bin/rclone'):
            continue
        scan = body
        if kind == 'personal macOS path' and name.endswith('vendor/wsgidav-4.3.5.dist-info/METADATA'):
            # Public upstream README log example; preserve the original attribution.
            example = b'/Users/' + b'martin/prj/git/wsgidav/wsgidav/dir_browser/htdocs'
            scan = scan.replace(example, b'<upstream-example>')
        if re.search(pattern, scan):
            findings.append((name, kind))


def inspect_staged(root=ROOT):
    """Scan index blobs, not unstaged replacements or unrelated local files."""
    names = subprocess.check_output([
        'git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z',
    ], cwd=root).split(b'\0')
    count = 0
    for raw in names:
        if not raw:
            continue
        name = raw.decode('utf-8')
        entry = subprocess.check_output(['git', 'ls-files', '--stage', '-z', '--', name], cwd=root)
        if not entry.startswith(b'100644 ') and not entry.startswith(b'100755 '):
            findings.append((name, 'non-regular staged file'))
            continue
        body = subprocess.check_output(['git', 'show', ':' + name], cwd=root)
        inspect(name, body)
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifacts', type=Path, default=ROOT / 'artifacts')
    parser.add_argument('--staged', action='store_true', help='Read-only privacy check of staged Git blobs; not release verification')
    args = parser.parse_args()
    findings.clear()
    if args.staged:
        count = inspect_staged()
        if findings:
            for name, kind in sorted(set(findings)):
                print(f'BLOCKED: {name}: {kind}')
            raise SystemExit('Commit blocked; no matching secret values were printed')
        print(json.dumps({'ok': True, 'stagedFilesScanned': count, 'releaseVerified': False}))
        return
    files = 0
    for file in ROOT.rglob('*'):
        relative = file.relative_to(ROOT)
        if set(relative.parts) & EXCLUDED or file.name.startswith('qa-') or not file.is_file():
            continue
        if file.suffix == '.pyc' or file.name == 'rclone' or file.suffix == '.zip':
            continue
        inspect(relative.as_posix(), file.read_bytes())
        files += 1
    artifacts = args.artifacts.resolve()
    for line in (artifacts / 'SHA256SUMS.txt').read_text().splitlines():
        digest, name = line.split('  ', 1)
        assert '/' not in name and '\\' not in name
        body = (artifacts / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == digest, name
        inspect(name, body)
    catalog = artifacts / 'catalog'
    key = catalog / 'repository-public.pem'
    packages = load_verified_catalog(catalog, key)['packages']
    assert {p['id'] for p in packages} == {'devicemanager', '115sync', 'aliyundrivesync', 'webdav', 'qbittorrent'}
    for package in packages:
        bundle = catalog / package['bundle']
        assert hashlib.sha256(bundle.read_bytes()).hexdigest() == package['sha256']
        verify_detached_signature(bundle, catalog / package['signature'], key)
        with tempfile.TemporaryDirectory() as tmp:
            safe_extract_bundle(bundle, Path(tmp))
            manifest = validate_manifest(json.loads((Path(tmp) / 'manifest.json').read_text()))
            assert (manifest['id'], manifest['version']) == (package['id'], package['version'])
    if findings:
        for name, kind in sorted(set(findings)):
            print(f'BLOCKED: {name}: {kind}')
        raise SystemExit('Release blocked; no matching secret values were printed')
    result = {'ok': True, 'sourceFilesScanned': files, 'signedPlugins': len(packages),
              'checksums': True, 'nestedZipIntegrity': True, 'privacyPatternScan': True,
              'physicalWindowsInstallTested': False}
    (artifacts / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
