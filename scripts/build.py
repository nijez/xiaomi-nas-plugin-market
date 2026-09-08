"""Build an isolated signed ecosystem snapshot; never uploads or deploys."""
import argparse
import hashlib
import shutil
import subprocess
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd or ROOT, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--signing-key', type=Path)
    parser.add_argument('--refresh-installer', action='store_true', help='Repackage only the installer using existing signed bundles')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts', help='Fresh output directory for this release')
    args = parser.parse_args()
    key = args.signing_key.expanduser().resolve() if args.signing_key else None
    if not args.refresh_installer and not key:
        raise SystemExit('--signing-key is required for plugin builds')
    if key and ROOT in key.parents:
        raise SystemExit('Signing keys must be outside the public repository')
    store = ROOT / 'projects/xiaomi-community-app-store'
    webdav = ROOT / 'projects/xiaomi-webdav-plugin'
    sys.path.insert(0, str(store / 'scripts'))
    from build_repository import PACKAGE_SPECS, assert_catalog_matches_sources, check_dependencies
    if not args.refresh_installer:
        for spec in PACKAGE_SPECS:
            check_dependencies(spec)
    if not args.refresh_installer and not (webdav / 'bin/rclone').is_file():
        raise SystemExit('Verified Linux ARM64 rclone is required; see BUILD.md')
    artifacts = args.output.resolve()
    stage = artifacts / 'catalog'
    if not args.refresh_installer:
        if stage.exists():
            raise SystemExit('Existing release output preserved; use a fresh checkout/output for a new build')
        run('python3', 'scripts/prepare_vendor.py', cwd=webdav)
        run('node', 'scripts/build_ui.cjs', cwd=webdav)
        run('python3', 'scripts/build_ui.py', cwd=ROOT / 'projects/xiaomi-qbittorrent-plugin')
        run('python3', 'scripts/build_repository.py', '--signing-key', str(key), '--output', str(stage),
            '--include-candidates', cwd=store)
    elif not (stage / 'catalog.json.sig').exists():
        raise SystemExit('No existing signed catalog to reuse')
    assert_catalog_matches_sources(stage)
    shutil.copytree(stage, store / 'catalog', dirs_exist_ok=True)
    run('python3', 'scripts/build_release.py', cwd=store)
    version = runpy.run_path(str(store / 'scripts/build_release.py'))['VERSION']
    file = store / 'dist' / f'xiaomi-plugin-market-{version}.zip'
    shutil.copy2(file, artifacts / file.name)
    for file in (stage / 'bundles').iterdir():
        shutil.copy2(file, artifacts / file.name)
    for name in ('repository-public.pem', 'repository-public.sha256', 'catalog.json', 'catalog.json.sig'):
        shutil.copy2(stage / name, artifacts / name)
    sums = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
                   for p in sorted(artifacts.iterdir()) if p.is_file() and p.name not in ('SHA256SUMS.txt', 'verification.json'))
    (artifacts / 'SHA256SUMS.txt').write_text(sums)
    print('Local release artifacts ready; no GitHub upload or NAS deployment performed')


if __name__ == '__main__':
    main()
