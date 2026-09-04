"""Offline runtime integrity checks. Never repairs or enables services implicitly."""
import hashlib
import json
import sys
from pathlib import Path


def runtime_issues(project=None):
    project = Path(project or Path(__file__).resolve().parent)
    issues = []
    if sys.version_info < (3, 9):
        issues.append('Python 版本低于 3.9')
    try:
        manifest = json.loads((project / 'vendor-integrity.json').read_text())
        if not manifest or not isinstance(manifest, dict):
            raise ValueError('invalid manifest')
        vendor = project / 'vendor'
        for name, digest in manifest.items():
            path = vendor / name
            if '..' in Path(name).parts or Path(name).is_absolute() or path.is_symlink() or not path.is_file():
                raise ValueError('invalid runtime')
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('changed runtime')
        if any(p.suffix in ('.so', '.pyd') for p in vendor.rglob('*')):
            raise ValueError('platform-specific extension')
    except (ValueError, OSError, TypeError):
        issues.append('插件依赖缺失或完整性校验失败，请用可信签名安装包修复')
    return issues
