#!/usr/bin/env python3
"""Create the shareable one-time bootstrap package without private material."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
VERSION = "0.1.1"
DIST = PROJECT / "dist"
PACKAGE_NAME = f"xiaomi-plugin-market-{VERSION}"
INCLUDE = [
    "install.sh",
    "install-macos.command",
    "install-windows.cmd",
    "install-windows.ps1",
    "WINDOWS-安装说明.txt",
    "README.md",
    "server.py",
    "storelib.py",
    "web",
    "catalog",
    "deploy",
    "schemas",
    "docs",
]


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / f"{PACKAGE_NAME}.zip"
    with tempfile.TemporaryDirectory(prefix="xiaomi-community-store-release-") as temporary:
        root = Path(temporary) / PACKAGE_NAME
        root.mkdir()
        for relative in INCLUDE:
            source = PROJECT / relative
            destination = root / relative
            if source.is_dir():
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, destination)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                relative = path.relative_to(root.parent).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(2026, 9, 2, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o100755 if path.suffix in (".sh", ".py", ".command") else 0o100644
                info.external_attr = (mode & 0xFFFF) << 16
                archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (DIST / "SHA256SUMS.txt").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"{target}\nSHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
