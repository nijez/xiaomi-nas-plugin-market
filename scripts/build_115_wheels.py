"""Run inside the pinned non-root, network-disabled ARM64 builder container."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile


def main():
    if os.getuid() == 0 or platform.system() != 'Linux' or platform.machine() != 'aarch64' or sys.version_info[:2] != (3, 12):
        raise SystemExit('Requires non-root Linux ARM64 Python 3.12 builder')
    source = Path('/inputs')
    output = Path('/output')
    records = json.loads((source / 'inputs.json').read_text())['files']
    for item in records:
        file = source / item['path']
        if source not in file.resolve().parents or file.is_symlink():
            raise SystemExit('Invalid build input path')
        if hashlib.sha256(file.read_bytes()).hexdigest() != item['sha256']:
            raise SystemExit('Build input digest mismatch')
    wheels = output / 'wheelhouse'
    wheels.mkdir(exist_ok=False)
    for item in records:
        if item['path'].startswith('runtime-wheels/'):
            shutil.copy2(source / item['path'], wheels)
        elif item['group'] == 'bootstrap':
            bootstrap = output / 'pip-bootstrap'
            bootstrap.mkdir(exist_ok=True)
            shutil.copy2(source / item['path'], bootstrap)
    env = {key: value for key, value in os.environ.items() if not key.startswith(('PIP_', 'PYTHON'))}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1', HOME='/tmp', SOURCE_DATE_EPOCH='1788566400')
    with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
        temporary = Path(temporary)
        tools = temporary / 'tools'
        command = [sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile', '--target', str(tools)]
        command += [str(source / item['path']) for item in records if item['group'] == 'build']
        subprocess.run(command, env=env, check=True)
        env['PYTHONPATH'] = str(tools)
        for item in records:
            if not item['path'].startswith('sources/'):
                continue
            unpacked = temporary / item['name']
            unpacked.mkdir()
            with tarfile.open(source / item['path']) as archive:
                members = archive.getmembers()
                if sum(member.size for member in members) > 64 * 1024 * 1024:
                    raise SystemExit('Oversized source archive')
                if any(not (member.isfile() or member.isdir()) for member in members):
                    raise SystemExit('Non-regular source archive entry')
                archive.extractall(unpacked, filter='data')
            roots = list(unpacked.iterdir())
            if len(roots) != 1 or not (roots[0] / 'setup.py').is_file():
                raise SystemExit('Unexpected source layout')
            subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-index', '--no-deps', '--no-build-isolation',
                            '--no-cache-dir', '--wheel-dir', str(wheels), str(roots[0])], env=env, check=True)
    shutil.copy2(source / 'requirements.txt', output / 'requirements.txt')
    sys.path.insert(0, '/helpers')
    from offline_dependencies import create_lock, install_bundle
    create_lock(output / 'requirements.txt')
    install_bundle(output / 'requirements.txt', output / 'verified-lib')
    report = {'python': platform.python_version(), 'architecture': platform.machine(), 'libc': platform.libc_ver(),
              'inputs': records, 'wheels': {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in sorted(wheels.iterdir())}}
    (output / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ARM64 wheels built; full offline dependency installation passed.')


if __name__ == '__main__':
    main()
