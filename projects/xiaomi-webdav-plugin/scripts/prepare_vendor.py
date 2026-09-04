"""Normalize generated dependencies to pure Python and record their integrity."""
import hashlib
import csv
import json
import shutil
from pathlib import Path, PurePosixPath

project = Path(__file__).resolve().parents[1]
vendor = project / 'vendor'
# MarkupSafe has an upstream pure-Python fallback; never ship a Mac extension to Linux.
for path in list(vendor.rglob('*')):
    if path.is_file() and path.suffix in ('.so', '.pyd', '.pyc'):
        path.unlink()
for path in list(vendor.rglob('__pycache__')):
    shutil.rmtree(path)
for name in ('bin', 'wsgidav/samples', 'wsgidav/server'):
    path = vendor / name
    if path.exists():
        shutil.rmtree(path)
# pip may record absolute build-machine bytecode paths. Keep only shipped files.
for record in vendor.glob('*.dist-info/RECORD'):
    with record.open(newline='') as stream:
        rows = list(csv.reader(stream))
    rows = [row for row in rows if row and not PurePosixPath(row[0]).is_absolute()
            and '..' not in PurePosixPath(row[0]).parts and (vendor / row[0]).is_file()]
    with record.open('w', newline='') as stream:
        csv.writer(stream).writerows(rows)
manifest = {p.relative_to(vendor).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(vendor.rglob('*')) if p.is_file()}
(project / 'vendor-integrity.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
print(f'Pure-Python runtime: {len(manifest)} integrity-checked files')
