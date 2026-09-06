#!/usr/bin/env python3
"""Verified, declarative installer for Xiaomi NAS community plugins."""

from __future__ import annotations

import hashlib
import fcntl
from contextlib import contextmanager
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
import sys

if (Path(__file__).resolve().parents[2] / "shared").is_dir():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
from plugin_security import provision_proxy
from offline_dependencies import DependencyError, install_bundle, verify_bundle


ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
SERVICE_PATTERN = re.compile(r"^[a-z0-9-]+\.service$")
NGINX_PATTERN = re.compile(r"^[a-z0-9-]+\.conf$")
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 192 * 1024 * 1024
MAX_FILES = 5000


class StoreError(RuntimeError):
    pass


@contextmanager
def installation_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StoreError("Another NAS installation or removal is in progress") from error
        yield
    finally:
        os.close(descriptor)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_detached_signature(payload: Path, signature: Path, public_key: Path) -> None:
    result = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public_key),
            "-signature",
            str(signature),
            str(payload),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "signature mismatch").strip()
        raise StoreError(f"Signature verification failed: {detail}")


def _safe_member_path(member_name: str) -> PurePosixPath:
    path = PurePosixPath(member_name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise StoreError(f"Unsafe bundle path: {member_name!r}")
    return path


def safe_extract_bundle(bundle: Path, destination: Path) -> None:
    if bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise StoreError("Bundle exceeds the 64 MiB compressed size limit")
    expanded = 0
    with zipfile.ZipFile(bundle) as archive:
        members = archive.infolist()
        if len(members) > MAX_FILES:
            raise StoreError("Bundle contains too many files")
        for member in members:
            _safe_member_path(member.filename)
            expanded += member.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise StoreError("Bundle exceeds the 192 MiB expanded size limit")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
                raise StoreError(f"Unsupported special file in bundle: {member.filename}")
        archive.extractall(destination)


def validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise StoreError("manifest.json must be an object")
    required = {
        "schemaVersion",
        "id",
        "name",
        "version",
        "pluginId",
        "port",
        "paths",
        "service",
        "nginx",
        "healthPath",
        "registry",
    }
    unknown = set(manifest) - (required | {"requirements"})
    missing = required - set(manifest)
    if missing or unknown:
        raise StoreError(f"Invalid manifest fields; missing={sorted(missing)}, unknown={sorted(unknown)}")
    if manifest["schemaVersion"] != 1:
        raise StoreError("Unsupported manifest schema")
    if not isinstance(manifest["id"], str) or not ID_PATTERN.fullmatch(manifest["id"]):
        raise StoreError("Invalid plugin id")
    if not isinstance(manifest["name"], str) or not 1 <= len(manifest["name"]) <= 40:
        raise StoreError("Invalid plugin name")
    if not isinstance(manifest["version"], str) or not VERSION_PATTERN.fullmatch(manifest["version"]):
        raise StoreError("Invalid plugin version")
    if not isinstance(manifest["pluginId"], int) or manifest["pluginId"] < 1:
        raise StoreError("Invalid numeric plugin ID")
    if not isinstance(manifest["port"], int) or not 1024 <= manifest["port"] <= 65535:
        raise StoreError("Invalid service port")
    if not isinstance(manifest["service"], str) or not SERVICE_PATTERN.fullmatch(manifest["service"]):
        raise StoreError("Invalid systemd service name")
    if not isinstance(manifest["nginx"], str) or not NGINX_PATTERN.fullmatch(manifest["nginx"]):
        raise StoreError("Invalid Nginx config name")
    if not isinstance(manifest["healthPath"], str) or not manifest["healthPath"].startswith("/"):
        raise StoreError("Invalid health path")
    paths = manifest["paths"]
    if not isinstance(paths, dict) or set(paths) != {"releaseRoot", "uiKey", "iconName"}:
        raise StoreError("Invalid paths object")
    if not re.fullmatch(r"/data/plugin/[a-z0-9-]+", str(paths["releaseRoot"])):
        raise StoreError("Invalid release root")
    if not ID_PATTERN.fullmatch(str(paths["uiKey"])):
        raise StoreError("Invalid UI key")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.icon", str(paths["iconName"])):
        raise StoreError("Invalid icon name")
    if "requirements" in manifest and not re.fullmatch(r"runtime/[A-Za-z0-9._/-]+", str(manifest["requirements"])):
        raise StoreError("Invalid requirements path")
    if not isinstance(manifest["registry"], dict):
        raise StoreError("Invalid registry record")
    return manifest


def load_verified_catalog(catalog_dir: Path, public_key: Path) -> dict[str, Any]:
    catalog_path = catalog_dir / "catalog.json"
    verify_detached_signature(catalog_path, catalog_dir / "catalog.json.sig", public_key)
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StoreError(f"Cannot read catalog: {error}") from error
    if not isinstance(catalog, dict) or catalog.get("schemaVersion") != 1:
        raise StoreError("Unsupported catalog schema")
    packages = catalog.get("packages")
    if not isinstance(packages, list):
        raise StoreError("Catalog packages must be an array")
    seen: set[str] = set()
    for package in packages:
        if not isinstance(package, dict) or not ID_PATTERN.fullmatch(str(package.get("id", ""))):
            raise StoreError("Catalog contains an invalid package")
        if package["id"] in seen:
            raise StoreError(f"Duplicate package id: {package['id']}")
        seen.add(package["id"])
        if not re.fullmatch(r"[0-9a-f]{64}", str(package.get("sha256", ""))):
            raise StoreError(f"Invalid SHA-256 for {package['id']}")
        for field in ("bundle", "signature", "icon"):
            _safe_member_path(str(package.get(field, "")))
    return catalog


def _copy_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _write_json_atomic(path: Path, value: Any, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".community-store.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)
    os.chmod(path, mode)


class InstallManager:
    def __init__(
        self,
        catalog_dir: Path,
        public_key: Path,
        user_id: str,
        root: Path = Path("/"),
        execute_system: bool = True,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", user_id):
            raise StoreError("Invalid Xiaomi user id")
        self.catalog_dir = catalog_dir.resolve()
        self.public_key = public_key.resolve()
        self.user_id = user_id
        self.root = root.resolve()
        self.execute_system = execute_system
        self.state_dir = self._host("/data/plugin/community-store/state")
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _host(self, absolute: str) -> Path:
        if not absolute.startswith("/"):
            raise StoreError(f"Expected absolute target path: {absolute}")
        return self.root / absolute.lstrip("/")

    def _package_entry(self, package_id: str) -> dict[str, Any]:
        catalog = load_verified_catalog(self.catalog_dir, self.public_key)
        for package in catalog["packages"]:
            if package["id"] == package_id:
                return package
        raise StoreError(f"Package not found: {package_id}")

    def _catalog_file(self, relative: str) -> Path:
        candidate = (self.catalog_dir / relative).resolve()
        try:
            candidate.relative_to(self.catalog_dir)
        except ValueError as error:
            raise StoreError("Catalog path escaped its repository") from error
        if not candidate.is_file():
            raise StoreError(f"Catalog artifact is missing: {relative}")
        return candidate

    def _registry_path(self) -> Path:
        return self._host(f"/data/plugin/{self.user_id}.list")

    def _load_registry(self) -> dict[str, Any]:
        path = self._registry_path()
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoreError(f"Cannot read Xiaomi plugin registry: {error}") from error
        if not isinstance(value, dict):
            raise StoreError("Xiaomi plugin registry must be an object")
        return value

    def _registry_record(self, manifest: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        record = json.loads(json.dumps(manifest["registry"], ensure_ascii=False))
        record.update({"status": "running", "install": True, "enable": True})
        info = record.setdefault("info", {})
        info.update(
            {
                "plugin": manifest["id"],
                "name": manifest["name"],
                "id": manifest["pluginId"],
                "version": manifest["version"],
                "port": str(manifest["port"]),
                "system": False,
                "type": "standard",
            }
        )
        record["timestamp"] = now
        record["changetime"] = now
        return record

    def _check_registry_conflicts(self, registry: dict[str, Any], manifest: dict[str, Any]) -> None:
        for key, record in registry.items():
            if key == manifest["id"] or not isinstance(record, dict):
                continue
            info = record.get("info")
            if isinstance(info, dict) and str(info.get("id")) == str(manifest["pluginId"]):
                raise StoreError(f"Plugin ID {manifest['pluginId']} is already used by {key}")
            if isinstance(info, dict) and str(info.get("port")) == str(manifest["port"]):
                raise StoreError(f"Port {manifest['port']} is already declared by {key}")

    def _run(self, command: list[str]) -> None:
        if not self.execute_system:
            return
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise StoreError(f"{command[0]} failed: {detail}")

    def _service_state(self, service: str) -> tuple[bool, str]:
        if not self.execute_system:
            return False, "disabled"
        active = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True, check=False)
        enabled = subprocess.run(["systemctl", "is-enabled", service], capture_output=True, text=True, check=False)
        active_state = active.stdout.strip()
        enabled_state = enabled.stdout.strip()
        if not enabled_state and enabled.returncode != 0:
            loaded = subprocess.run(["systemctl", "show", service, "--property=LoadState", "--value"], capture_output=True, text=True, check=False)
            if loaded.stdout.strip() == "not-found":
                enabled_state = "not-found"
        if active_state not in {"active", "inactive", "failed", "unknown"}:
            raise StoreError(f"Cannot safely snapshot service state: {active_state or active.stderr.strip()}")
        if enabled_state == "not-found" or (not enabled_state and enabled.returncode == 4):
            enabled_state = "disabled"
        if enabled_state not in {"enabled", "enabled-runtime", "disabled", "static", "indirect"}:
            raise StoreError(f"Refusing to change unsupported service enablement: {enabled_state or enabled.stderr.strip()}")
        return active_state == "active", enabled_state

    def _install_requirements(self, extracted: Path, manifest: dict[str, Any], release: Path) -> None:
        requirements = manifest.get("requirements")
        if not requirements:
            return
        requirements_path = extracted / requirements
        if extracted.resolve() not in requirements_path.resolve().parents:
            raise StoreError("Requirements path escapes the bundle")
        try:
            verify_bundle(requirements_path)
            if self.execute_system:
                install_bundle(requirements_path, release / "lib", python="/usr/bin/python3")
        except DependencyError as error:
            raise StoreError(str(error)) from error

    def _wait_for_health(self, url: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status < 400:
                        return
                    last_error = StoreError(f"Health check returned HTTP {response.status}")
            except Exception as error:
                last_error = error
            time.sleep(0.4)
        raise StoreError(f"Health check failed after {timeout:g}s: {last_error}")

    def install(self, package_id: str) -> dict[str, Any]:
        with installation_lock(self._host("/data/plugin/community-store/install.lock")):
            return self._install_locked(package_id)

    def _install_locked(self, package_id: str) -> dict[str, Any]:
        package = self._package_entry(package_id)
        bundle = self._catalog_file(package["bundle"])
        signature = self._catalog_file(package["signature"])
        if sha256_file(bundle) != package["sha256"]:
            raise StoreError("Bundle checksum mismatch")
        verify_detached_signature(bundle, signature, self.public_key)

        operation = f"{int(time.time())}-{uuid.uuid4().hex}"
        staging_root = self._host(f"/data/plugin/community-store/staging/{operation}")
        backup_root = self._host(f"/data/plugin/community-store/backups/{operation}")
        staging_root.mkdir(parents=True, exist_ok=False)
        backup_root.mkdir(parents=True, exist_ok=False)
        try:
            safe_extract_bundle(bundle, staging_root)
            manifest_path = staging_root / "manifest.json"
            try:
                manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as error:
                raise StoreError(f"Cannot read bundle manifest: {error}") from error
            if manifest["id"] != package["id"] or manifest["version"] != package["version"]:
                raise StoreError("Catalog and bundle identity do not match")

            for required in ("runtime", "ui", "icon", f"config/{manifest['service']}", f"config/{manifest['nginx']}"):
                if not (staging_root / required).exists():
                    raise StoreError(f"Bundle payload is missing: {required}")

            registry = self._load_registry()
            self._check_registry_conflicts(registry, manifest)
            release_root = self._host(manifest["paths"]["releaseRoot"])
            release = release_root / "releases" / f"{manifest['version']}-{operation}"
            ui_target = self._host(f"/home/{self.user_id}/plugin/{manifest['paths']['uiKey']}/src/ui")
            icon_target = self._host(f"/data/plugin/www/icon/{manifest['paths']['iconName']}")
            service_target = self._host(f"/etc/systemd/system/{manifest['service']}")
            nginx_target = self._host(f"/etc/nginx/conf.d/luci/{manifest['nginx']}")
            registry_path = self._registry_path()

            tracked = {
                "ui": ui_target,
                "icon": icon_target,
                "service": service_target,
                "nginx": nginx_target,
                "registry": registry_path,
                "current": release_root / "current",
                "state": self.state_dir / f"{manifest['id']}.json",
            }
            existing: dict[str, bool] = {}
            for key, target in tracked.items():
                existing[key] = target.exists() or target.is_symlink()
                if existing[key]:
                    _copy_path(target, backup_root / key)

            was_active, was_enabled = self._service_state(manifest["service"])
            activation_started = False
            try:
                _copy_path(staging_root / "runtime", release)
                self._install_requirements(staging_root, manifest, release)
                _copy_path(staging_root / "ui", ui_target)
                _copy_path(staging_root / "icon", icon_target)
                service_text = (staging_root / "config" / manifest["service"]).read_text(encoding="utf-8")
                service_text = service_text.replace("__NAS_USER_ID__", self.user_id)
                nginx_text = (staging_root / "config" / manifest["nginx"]).read_text(encoding="utf-8")
                nginx_text = nginx_text.replace("__NAS_USER_ID__", self.user_id)
                service_target.parent.mkdir(parents=True, exist_ok=True)
                nginx_target.parent.mkdir(parents=True, exist_ok=True)
                service_target.write_text(service_text, encoding="utf-8")
                nginx_target.write_text(nginx_text, encoding="utf-8")
                os.chmod(service_target, 0o644)
                os.chmod(nginx_target, 0o644)
                if "__PLUGIN_PROXY_KEY__" in nginx_text:
                    provision_proxy(nginx_target, nginx_target, release_root / "proxy.key")
                current = release_root / "current"
                current.parent.mkdir(parents=True, exist_ok=True)
                temporary_link = current.with_name("current.community-store.tmp")
                if temporary_link.exists() or temporary_link.is_symlink():
                    temporary_link.unlink()
                temporary_link.symlink_to(release)
                if current.exists() and not current.is_symlink() and current.is_dir():
                    shutil.rmtree(current)
                temporary_link.replace(current)

                self._run(["nginx", "-t"])
                activation_started = True
                self._run(["systemctl", "daemon-reload"])
                self._run(["systemctl", "enable", manifest["service"]])
                self._run(["systemctl", "restart", manifest["service"]])
                self._run(["systemctl", "reload", "nginx"])
                if self.execute_system:
                    health = f"http://127.0.0.1:{manifest['port']}{manifest['healthPath']}"
                    self._wait_for_health(health)

                registry[manifest["id"]] = self._registry_record(manifest)
                _write_json_atomic(registry_path, registry)
                state = {
                    "managed": True,
                    "installedAt": int(time.time()),
                    "manifest": manifest,
                    "release": str(release),
                }
                _write_json_atomic(self.state_dir / f"{manifest['id']}.json", state, 0o600)
            except Exception as install_error:
                # Stop and disable the candidate BEFORE removing its unit or code.
                # A failed stop is not permission to destroy a running release.
                if activation_started:
                    try:
                        self._run(["systemctl", "stop", manifest["service"]])
                        self._run(["systemctl", "disable", manifest["service"]])
                    except Exception as rollback_error:
                        raise StoreError(f"Install failed: {install_error}; rollback incomplete: {rollback_error}; preserved {backup_root} and {release}") from install_error
                for key, target in tracked.items():
                    if target.exists() or target.is_symlink():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    if existing[key]:
                        _copy_path(backup_root / key, target)
                if release.exists():
                    shutil.rmtree(release)
                if activation_started:
                    try:
                        self._run(["systemctl", "daemon-reload"])
                        if was_enabled in {"enabled", "enabled-runtime"}:
                            command = ["systemctl", "enable"]
                            if was_enabled == "enabled-runtime":
                                command.append("--runtime")
                            self._run(command + [manifest["service"]])
                        if was_active:
                            self._run(["systemctl", "start", manifest["service"]])
                        self._run(["nginx", "-t"])
                        self._run(["systemctl", "reload", "nginx"])
                    except Exception as rollback_error:
                        raise StoreError(f"Install failed: {install_error}; rollback incomplete: {rollback_error}; backups at {backup_root}") from install_error
                raise
            return {"ok": True, "id": manifest["id"], "version": manifest["version"]}
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    def uninstall(self, package_id: str) -> dict[str, Any]:
        with installation_lock(self._host("/data/plugin/community-store/install.lock")):
            return self._uninstall_locked(package_id)

    def _uninstall_locked(self, package_id: str) -> dict[str, Any]:
        state_path = self.state_dir / f"{package_id}.json"
        if not state_path.is_file():
            raise StoreError("Only plugins installed by this store can be uninstalled here")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest = validate_manifest(state.get("manifest"))
        release_root = self._host(manifest["paths"]["releaseRoot"])
        release = Path(str(state.get("release", ""))).resolve()
        releases_root = (release_root / "releases").resolve()
        try:
            release.relative_to(releases_root)
        except ValueError as error:
            raise StoreError("Managed release path is outside the plugin release root") from error
        self._run(["systemctl", "disable", "--now", manifest["service"]])
        targets = [
            self._host(f"/etc/nginx/conf.d/luci/{manifest['nginx']}"),
            self._host(f"/etc/systemd/system/{manifest['service']}"),
            self._host(f"/home/{self.user_id}/plugin/{manifest['paths']['uiKey']}/src/ui"),
            self._host(f"/data/plugin/www/icon/{manifest['paths']['iconName']}"),
        ]
        for target in targets:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        current = release_root / "current"
        if current.is_symlink() and current.resolve() == release:
            current.unlink()
        if release.is_dir():
            shutil.rmtree(release)
        registry = self._load_registry()
        registry.pop(manifest["id"], None)
        _write_json_atomic(self._registry_path(), registry)
        state_path.unlink()
        self._run(["systemctl", "daemon-reload"])
        self._run(["nginx", "-t"])
        self._run(["systemctl", "reload", "nginx"])
        return {"ok": True, "id": package_id, "dataPreserved": True}

    def installed(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in self.state_dir.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                manifest = validate_manifest(state.get("manifest"))
                result[manifest["id"]] = manifest["version"]
            except Exception:
                continue
        return result

    def inventory(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for plugin_id, record in self._load_registry().items():
            if not isinstance(record, dict):
                continue
            info = record.get("info")
            if not isinstance(info, dict):
                continue
            version = info.get("version")
            if isinstance(version, str) and VERSION_PATTERN.fullmatch(version):
                result[plugin_id] = {"version": version, "managed": False}
        for plugin_id, version in self.installed().items():
            result[plugin_id] = {"version": version, "managed": True}
        return result
