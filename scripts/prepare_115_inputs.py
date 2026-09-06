"""Fetch official fixed-version inputs; never execute downloaded source code."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import urllib.request
import urllib.error
import time
from urllib.parse import urlparse

try:
    from packaging.tags import cpython_tags, compatible_tags
    from packaging.utils import parse_wheel_filename
except ImportError:
    from pip._vendor.packaging.tags import cpython_tags, compatible_tags
    from pip._vendor.packaging.utils import parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
BUILD_PINS = {'setuptools': '80.9.0', 'wheel': '0.45.1', 'packaging': '25.0'}


def validate_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'pypi.org', 'files.pythonhosted.org'}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Only official HTTPS package hosts are allowed')


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    validate_url(url)
    opener = urllib.request.build_opener(OfficialRedirect())
    partial = b''
    for attempt in range(8):
        try:
            request = urllib.request.Request(url, headers={'Range': f'bytes={len(partial)}-'} if partial else {})
            with opener.open(request, timeout=60) as response:
                validate_url(response.url)
                if response.status == 206:
                    if not response.headers.get('Content-Range', '').startswith(f'bytes {len(partial)}-'):
                        raise ValueError('Unexpected resume offset')
                else:
                    partial = b''
                try:
                    return partial + response.read()
                except http.client.IncompleteRead as error:
                    partial += error.partial
                    raise
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError) as error:
            if attempt == 7:
                raise RuntimeError('Official download failed after eight bounded attempts') from error
            time.sleep(min(attempt + 1, 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true', help='Reuse only files that still match official hashes')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=args.resume)
    # Bookworm builds must also run on the measured RP05 glibc 2.39 host.
    platforms = [f'manylinux_2_{minor}_aarch64' for minor in range(36, 16, -1)] + ['manylinux2014_aarch64']
    tags = list(cpython_tags((3, 12), ['cp312'], platforms))
    tags += list(compatible_tags((3, 12), 'cp312', platforms))
    rank = {tag: index for index, tag in enumerate(tags)}
    requirements = ROOT / 'projects/xiaomi-115-sync-plugin/requirements.txt'
    pins = dict(line.strip().split('==') for line in requirements.read_text().splitlines()
                if line.strip() and not line.startswith('#'))
    records = []
    for group, packages in [('runtime', pins), ('build', BUILD_PINS), ('bootstrap', {'pip': '25.2'})]:
        for name, version in packages.items():
            metadata_url = f'https://pypi.org/pypi/{name}/{version}/json'
            metadata = json.loads(fetch(metadata_url))
            candidates = []
            for entry in metadata['urls']:
                if entry.get('yanked'):
                    continue
                if entry['packagetype'] == 'bdist_wheel':
                    matching = set(parse_wheel_filename(entry['filename'])[3]) & rank.keys()
                    if matching:
                        candidates.append((min(rank[tag] for tag in matching), entry))
            if candidates:
                entry = min(candidates, key=lambda item: (item[0], item[1]['filename']))[1]
                folder = 'pip-bootstrap' if group == 'bootstrap' else group + '-wheels'
            else:
                sources = [entry for entry in metadata['urls'] if entry['packagetype'] == 'sdist' and not entry.get('yanked')]
                if group != 'runtime' or len(sources) != 1:
                    raise RuntimeError(f'No unique reviewed build input for {name}=={version}')
                entry = sources[0]
                folder = 'sources'
            filename = entry['filename']
            if Path(filename).name != filename or '/' in filename or '\\' in filename:
                raise ValueError('Invalid upstream filename')
            destination = args.output / folder / filename
            body = destination.read_bytes() if destination.is_file() else fetch(entry['url'])
            digest = hashlib.sha256(body).hexdigest()
            if digest != entry['digests']['sha256'] or len(body) != entry['size']:
                raise RuntimeError(f'Upstream integrity mismatch: {name}')
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(body)
            records.append({'name': name, 'version': version, 'group': group, 'path': f'{folder}/{filename}',
                            'sha256': digest, 'size': len(body), 'url': entry['url'], 'metadata': metadata_url})
            (args.output / 'inputs.json').write_text(json.dumps({'schemaVersion': 1, 'files': records}, indent=2) + '\n')
            print(f'{name}=={version}: {folder}/{filename}', flush=True)
    (args.output / 'requirements.txt').write_bytes(requirements.read_bytes())
    print('Inputs downloaded and hashed. Source review and isolated build are still required.')


if __name__ == '__main__':
    main()
