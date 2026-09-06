#!/usr/bin/env python3
"""Build deterministic, signed bundle-v1 packages from local projects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]
WORK = PROJECT.parent
CATALOG = PROJECT / "catalog"
sys.path.insert(0, str(PROJECT.parents[1] / "shared"))
from offline_dependencies import DependencyError, verify_bundle


PACKAGE_SPECS: list[dict[str, Any]] = [
    {
        "id": "qbittorrent", "name": "qB 下载", "version": "0.1.0-rc1",
        "summary": "磁力与种子下载、暂停继续和限速；首次启动需拉取独立 Docker 镜像",
        "project": WORK / "xiaomi-qbittorrent-plugin", "pluginId": 11004, "port": 18122,
        "releaseRoot": "/data/plugin/qbittorrent", "uiKey": "qbittorrent",
        "iconSource": "web/assets/qb.png", "iconName": "qbittorrent.icon",
        "runtime": {"server.py": "server.py", "engine.py": "engine.py", "web": "web", "licenses": "licenses", "README.md": "README.md"},
        "ui": "web", "serviceSource": "deploy/xiaomi-qbittorrent.service",
        "service": "xiaomi-qbittorrent.service", "nginxSource": "deploy/xiaomi-qbittorrent.nginx.conf",
        "nginx": "xiaomi-qbittorrent.conf", "healthPath": "/healthz",
        "registry": {
            "icon": "/icon/qbittorrent.icon?v=0.1.0-rc1",
            "frontend": {"title": "qB 下载", "desc": "下载任务管理", "type": "url",
                "permission": ["admin"], "dev_type": [1, 2, 3, 4],
                "url": [{"dev_type": [1], "url": "/index.html#/qbDownloads_app"},
                        {"dev_type": [2, 3, 4], "url": "/index.html#/qbDownloads_pc"}],
                "sortid": 11004, "widget": []},
            "info": {"tags": ["download"], "publisher": "community", "ext": {"admin": True}}
        }
    },
    {
        "id": "webdav",
        "name": "WebDAV 文件桥",
        "version": "0.2.0-rc5",
        "summary": "NAS HTTPS 文件共享，以及远程 WebDAV 上传、下载与定时备份",
        "project": WORK / "xiaomi-webdav-plugin",
        "pluginId": 11003,
        "port": 18120,
        "releaseRoot": "/data/plugin/webdav",
        "uiKey": "webdav",
        "iconSource": "web/assets/webdav.png",
        "iconName": "webdav-file-bridge.icon",
        "runtime": {"server.py": "server.py", "engine.py": "engine.py", "access.py": "access.py", "multi_dav.py": "multi_dav.py", "compatibility.py": "compatibility.py", "vendor": "vendor", "vendor-integrity.json": "vendor-integrity.json", "scripts/check_after_upgrade.py": "scripts/check_after_upgrade.py", "requirements-dav.txt": "requirements-dav.txt", "bin": "bin", "web": "web", "licenses": "licenses", "README.md": "README.md"},
        "ui": "web",
        "serviceSource": "deploy/xiaomi-webdav.service",
        "service": "xiaomi-webdav.service",
        "nginxSource": "deploy/xiaomi-webdav.nginx.conf",
        "nginx": "xiaomi-webdav.conf",
        "healthPath": "/healthz",
        "registry": {
            "icon": "/icon/webdav-file-bridge.icon?v=0.1.0",
            "frontend": {
                "title": "WebDAV 文件桥", "desc": "文件共享与备份", "type": "url",
                "permission": ["admin"], "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/webDavBridge_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/webDavBridge_pc"}
                ],
                "sortid": 11003, "widget": []
            },
            "info": {"tags": ["files", "backup"], "publisher": "community", "ext": {"admin": True}}
        }
    },
    {
        "id": "devicemanager",
        "name": "设备管家",
        "version": "0.4.1",
        "summary": "2 秒实时监控系统、网络、磁盘健康与 Docker 容器资源",
        "project": WORK / "xiaomi-device-manager-prototype",
        "pluginId": 11001,
        "port": 18080,
        "releaseRoot": "/data/plugin/xiaomi-device-manager",
        "uiKey": "devicemanager",
        "iconSource": "assets/xiaomi-device-manager-v2.png",
        "iconName": "xiaomi-device-manager-v2.icon",
        "runtime": {
            "scripts/nas_status_server.py": "server.py",
            "dist/client": "public",
        },
        "ui": "dist/client",
        "serviceSource": "deploy/xiaomi-device-manager.service",
        "service": "xiaomi-device-manager.service",
        "nginxSource": "deploy/xiaomi-device-manager.nginx.conf",
        "nginx": "xiaomi-device-manager.conf",
        "healthPath": "/healthz",
        "registry": {
            "icon": "/icon/xiaomi-device-manager-v2.icon?v=2",
            "frontend": {
                "title": "设备管家",
                "desc": "设备状态管家",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/deviceManager_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/deviceManager_pc"},
                ],
                "sortid": 11001,
                "widget": [],
            },
            "info": {
                "tags": ["tool", "monitoring"],
                "desc": "监控 NAS 系统、网络、硬盘健康与 Docker 容器资源",
                "developer": "Kingwell Community",
                "publisher": "community",
                "ext": {"admin": True},
            },
        },
    },
    {
        "id": "115sync",
        "name": "115 云备份",
        "version": "0.1.0",
        "summary": "使用 115 官方 OpenAPI 同步与备份 NAS 文件",
        "project": WORK / "xiaomi-115-sync-plugin",
        "pluginId": 1000,
        "port": 18115,
        "releaseRoot": "/data/plugin/115-sync",
        "uiKey": "115sync",
        "iconSource": "web/assets/115-sync-icon.png",
        "iconName": "115-sync.icon",
        "runtime": {"server.py": "server.py", "requirements.txt": "requirements.txt"},
        "requirements": "runtime/requirements.txt",
        "ui": "web",
        "serviceSource": "deploy/xiaomi-115-sync.service",
        "service": "xiaomi-115-sync.service",
        "nginxSource": "deploy/xiaomi-115-sync.nginx.conf",
        "nginx": "xiaomi-115-sync.conf",
        "healthPath": "/healthz",
        "registry": {
            "icon": "/icon/115-sync.icon?v=115life-38.2.0",
            "frontend": {
                "title": "115 云备份",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/115Sync_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/115Sync_pc"},
                ],
                "sortid": 1000,
                "widget": [],
            },
            "info": {"tags": ["cloud", "backup"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "aliyundrivesync",
        "name": "阿里云盘备份",
        "version": "0.1.0",
        "summary": "扫码连接阿里云盘并同步 NAS 文件",
        "project": WORK / "xiaomi-aliyundrive-sync-plugin",
        "pluginId": 1002,
        "port": 18117,
        "releaseRoot": "/data/plugin/aliyundrive-sync",
        "uiKey": "aliyundrivesync",
        "iconSource": "web/assets/aliyundrive-icon.png",
        "iconName": "aliyundrive-sync.icon",
        "runtime": {"server.py": "server.py"},
        "ui": "web",
        "serviceSource": "deploy/xiaomi-aliyundrive-sync.service",
        "service": "xiaomi-aliyundrive-sync.service",
        "nginxSource": "deploy/xiaomi-aliyundrive-sync.nginx.conf",
        "nginx": "xiaomi-aliyundrive-sync.conf",
        "healthPath": "/healthz",
        "registry": {
            "icon": "/icon/aliyundrive-sync.icon?v=0.1.0",
            "frontend": {
                "title": "阿里云盘备份",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/aliyunDriveSync_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/aliyunDriveSync_pc"},
                ],
                "sortid": 1002,
                "widget": [],
            },
            "info": {"tags": ["cloud", "backup"], "publisher": "community", "ext": {"admin": True}},
        },
    },
]


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def ensure_key(private_key: Path, create: bool) -> Path:
    public_key = CATALOG / "repository-public.pem"
    if not private_key.exists():
        if not create:
            raise SystemExit(f"Signing key does not exist: {private_key}; pass --create-key once")
        private_key.parent.mkdir(parents=True, exist_ok=True)
        run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(private_key)])
        os.chmod(private_key, 0o600)
    CATALOG.mkdir(parents=True, exist_ok=True)
    with public_key.open("wb") as output:
        subprocess.run(["openssl", "ec", "-in", str(private_key), "-pubout"], stdout=output, check=True)
    return public_key


def copy_required(source: Path, target: Path) -> None:
    if not source.exists():
        raise SystemExit(f"Missing package source: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    else:
        shutil.copy2(source, target)


def write_deterministic_zip(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 9, 2, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())


def sign(payload: Path, private_key: Path, signature: Path) -> None:
    run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(signature), str(payload)])


def check_dependencies(spec: dict[str, Any]) -> None:
    if spec.get("requirements"):
        try:
            requirements = Path(spec["project"]) / "requirements.txt"
            verify_bundle(requirements)
            from offline_dependencies import verify_pip_bootstrap
            verify_pip_bootstrap(requirements)
        except DependencyError as error:
            raise SystemExit(f"Dependency release gate ({spec['id']}): {error}") from error


def populate_payload(spec: dict[str, Any], root: Path) -> None:
    project = Path(spec["project"])
    check_dependencies(spec)
    for source, destination in spec["runtime"].items():
        copy_required(project / source, root / "runtime" / destination)
    if spec.get("requirements"):
        for name in ("requirements.lock", "wheelhouse", "pip-bootstrap"):
            copy_required(project / name, root / "runtime" / name)
        copy_required(PROJECT.parents[1] / "shared/offline_dependencies.py", root / "runtime/offline_dependencies.py")
    if spec["id"] in {"115sync", "aliyundrivesync", "webdav", "qbittorrent"}:
        copy_required(PROJECT.parents[1] / "shared/plugin_security.py", root / "runtime/plugin_security.py")
    copy_required(project / spec["ui"], root / "ui")
    copy_required(project / spec["iconSource"], root / "icon")
    copy_required(project / spec["serviceSource"], root / "config" / spec["service"])
    copy_required(project / spec["nginxSource"], root / "config" / spec["nginx"])


def assert_catalog_matches_sources(catalog_dir: Path) -> None:
    """Do not ship an installer claiming source fixes with old signed payloads."""
    catalog = json.loads((catalog_dir / "catalog.json").read_text(encoding="utf-8"))
    specs = {spec["id"]: spec for spec in PACKAGE_SPECS}
    for package in catalog["packages"]:
        spec = specs.get(package["id"])
        if spec is None or package["version"] != spec["version"]:
            raise SystemExit(f"Catalog identity does not match current source: {package['id']}")
        bundle = (catalog_dir / package["bundle"]).resolve()
        if catalog_dir.resolve() not in bundle.parents:
            raise SystemExit("Bundle path escapes the catalog")
        with tempfile.TemporaryDirectory(prefix="nas-source-gate-") as temporary:
            root = Path(temporary)
            populate_payload(spec, root)
            expected = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in root.rglob("*") if path.is_file()}
            with zipfile.ZipFile(bundle) as archive:
                actual = {info.filename: hashlib.sha256(archive.read(info)).hexdigest()
                          for info in archive.infolist() if not info.is_dir() and info.filename != "manifest.json"}
                manifest = json.loads(archive.read("manifest.json"))
            if expected != actual or manifest.get("healthPath") != spec["healthPath"]:
                raise SystemExit(f"Stale signed bundle: {package['id']}. Rebuild and sign plugin payloads; installer-only refresh is blocked.")


def build_bundle(spec: dict[str, Any], private_key: Path) -> dict[str, Any]:
    project = Path(spec["project"])
    with tempfile.TemporaryDirectory(prefix=f"xiaomi-store-{spec['id']}-") as temporary:
        root = Path(temporary)
        populate_payload(spec, root)
        manifest = {
            "schemaVersion": 1,
            "id": spec["id"],
            "name": spec["name"],
            "version": spec["version"],
            "pluginId": spec["pluginId"],
            "port": spec["port"],
            "paths": {
                "releaseRoot": spec["releaseRoot"],
                "uiKey": spec["uiKey"],
                "iconName": spec["iconName"],
            },
            "service": spec["service"],
            "nginx": spec["nginx"],
            "healthPath": spec["healthPath"],
            "registry": spec["registry"],
        }
        if spec.get("requirements"):
            manifest["requirements"] = spec["requirements"]
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        bundle_name = f"{spec['id']}-{spec['version']}.bundle.zip"
        bundle = CATALOG / "bundles" / bundle_name
        write_deterministic_zip(root, bundle)
    signature = bundle.with_suffix(bundle.suffix + ".sig")
    sign(bundle, private_key, signature)
    icon_name = f"{spec['id']}.png"
    copy_required(project / spec["iconSource"], CATALOG / "icons" / icon_name)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    return {
        "id": spec["id"],
        "name": spec["name"],
        "version": spec["version"],
        "summary": spec["summary"],
        "bundle": f"bundles/{bundle_name}",
        "signature": f"bundles/{signature.name}",
        "sha256": digest,
        "icon": f"icons/{icon_name}",
        "channel": "candidate" if "-rc" in spec["version"] else "stable",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--create-key", action="store_true")
    parser.add_argument("--output", type=Path, help="Write an isolated release catalog")
    parser.add_argument("--include-candidates", action="store_true", help="Explicitly include labeled test packages")
    args = parser.parse_args()
    global CATALOG
    if args.output:
        CATALOG = args.output.expanduser().resolve()
    if args.include_candidates and (not args.output or CATALOG == PROJECT / 'catalog'):
        raise SystemExit('Candidate publication requires a separate --output directory')
    if any('-rc' in spec['version'] for spec in PACKAGE_SPECS) and not args.include_candidates:
        raise SystemExit('Candidate package present: use the plugin candidate builder; stable catalog was not updated')
    for spec in PACKAGE_SPECS:
        check_dependencies(spec)
    ensure_key(args.signing_key.expanduser(), args.create_key)
    (CATALOG / "bundles").mkdir(parents=True, exist_ok=True)
    (CATALOG / "icons").mkdir(parents=True, exist_ok=True)
    packages = [build_bundle(spec, args.signing_key.expanduser()) for spec in PACKAGE_SPECS]
    catalog = {
        "schemaVersion": 1,
        "name": "小米智能存储插件市场",
        "generatedAt": int(time.time()),
        "packages": packages,
    }
    catalog_path = CATALOG / "catalog.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sign(catalog_path, args.signing_key.expanduser(), CATALOG / "catalog.json.sig")
    fingerprint = subprocess.check_output(
        ["openssl", "pkey", "-pubin", "-in", str(CATALOG / "repository-public.pem"), "-outform", "DER"]
    )
    fingerprint_text = hashlib.sha256(fingerprint).hexdigest()
    (CATALOG / "repository-public.sha256").write_text(fingerprint_text + "\n", encoding="ascii")
    print(f"Built {len(packages)} signed bundles; public-key fingerprint: {fingerprint_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
