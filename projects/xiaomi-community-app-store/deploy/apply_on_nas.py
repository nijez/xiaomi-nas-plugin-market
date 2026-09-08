#!/usr/bin/env python3
"""One shared activation/rollback path for Windows and macOS bootstraps.

Normal failures are rolled back. Power-loss recovery still requires a durable
transaction journal and is not claimed by this installer.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import shutil
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from storelib import InstallManager, StoreError, _copy_path, verify_detached_signature, installation_lock
from plugin_security import provision_proxy
from register_plugin import register_to

SERVICE = "xiaomi-community-store.service"


def apply_release(release: Path, user: str, plugin_id: int, root: Path = Path("/"), execute_system: bool = True) -> None:
    with installation_lock(root.resolve() / "data/plugin/community-store/install.lock"):
        _apply_release(release, user, plugin_id, root, execute_system)


def _apply_release(release: Path, user: str, plugin_id: int, root: Path, execute_system: bool) -> None:
    if not re.fullmatch(r"u[0-9]+", user) or plugin_id < 1:
        raise StoreError("Invalid owner or plugin ID")
    release = release.resolve()
    base = root.resolve() / "data/plugin/community-store"
    if release.parent != base / "releases" or not release.is_dir():
        raise StoreError("Candidate is outside the installer release directory")
    catalog = release / "catalog"
    verify_detached_signature(catalog / "catalog.json", catalog / "catalog.json.sig", catalog / "repository-public.pem")
    manager = InstallManager(catalog, catalog / "repository-public.pem", user, root=root, execute_system=execute_system)
    targets = {
        "service": manager._host("/etc/systemd/system/" + SERVICE),
        "nginx": manager._host("/etc/nginx/conf.d/luci/xiaomi-community-store.conf"),
        "current": base / "current",
        "registry": manager._registry_path(),
        "icon": manager._host("/data/plugin/www/icon/community-store-v4.icon"),
    }
    if targets["current"].is_symlink() and targets["current"].resolve() == release:
        raise StoreError("Candidate is already current; upload a fresh release directory")
    backup = base / "backups" / ("bootstrap-" + secrets.token_hex(12))
    backup.mkdir(parents=True, mode=0o700)
    existed = {}
    for name, target in targets.items():
        existed[name] = target.exists() or target.is_symlink()
        if existed[name]:
            _copy_path(target, backup / name)
    was_active, was_enabled = manager._service_state(SERVICE)
    activated = False
    try:
        token = base / "admin-token"
        try:
            descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as output:
                output.write(secrets.token_hex(16) + "\n")
        os.chmod(token, 0o600)
        targets["service"].parent.mkdir(parents=True, exist_ok=True)
        service = (release / "deploy" / SERVICE).read_text(encoding="utf-8")
        service = service.replace("__NAS_USER_ID__", user).replace("__URL_USER_ID__", user[1:])
        targets["service"].write_text(service, encoding="utf-8")
        targets["service"].chmod(0o644)
        provision_proxy(release / "deploy/xiaomi-community-store.nginx.conf", targets["nginx"], base / "proxy.key")
        _copy_path(release / "web/assets/community-store-v4.png", targets["icon"])
        link = base / ("current-" + secrets.token_hex(8))
        try:
            link.symlink_to(release)
            link.replace(targets["current"])
        finally:
            link.unlink(missing_ok=True)
        manager._run(["nginx", "-t"])
        activated = True
        manager._run(["systemctl", "daemon-reload"])
        manager._run(["systemctl", "enable", SERVICE])
        manager._run(["systemctl", "restart", SERVICE])
        manager._run(["systemctl", "reload", "nginx"])
        if execute_system:
            manager._wait_for_health("http://127.0.0.1:18119/healthz")
        register_to(targets["registry"], user, plugin_id)
    except Exception as failure:
        try:
            if activated:
                manager._run(["systemctl", "stop", SERVICE])
                manager._run(["systemctl", "disable", SERVICE])
            for name, target in targets.items():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
                if existed[name]:
                    _copy_path(backup / name, target)
            if activated:
                manager._run(["systemctl", "daemon-reload"])
                if was_enabled in {"enabled", "enabled-runtime"}:
                    manager._run(["systemctl", "enable"] + (["--runtime"] if was_enabled == "enabled-runtime" else []) + [SERVICE])
                if was_active:
                    manager._run(["systemctl", "start", SERVICE])
                manager._run(["nginx", "-t"])
                manager._run(["systemctl", "reload", "nginx"])
        except Exception as rollback_failure:
            raise StoreError(f"Install failed: {failure}; rollback incomplete: {rollback_failure}; retained candidate and backups at {backup}") from failure
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--plugin-id", type=int, default=11002)
    args = parser.parse_args()
    apply_release(PROJECT, args.user_id, args.plugin_id)
