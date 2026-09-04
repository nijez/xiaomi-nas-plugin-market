#!/usr/bin/env python3
"""Register only the community Device Manager in a Xiaomi user registry."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_KEY = "devicemanager"


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to read plugin registry: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError("Plugin registry must be a JSON object")
    return value


def plugin_record(plugin_id: int, now: int) -> dict[str, Any]:
    return {
        "status": "running",
        "install": True,
        "enable": True,
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
            "sortid": plugin_id,
            "widget": [],
        },
        "info": {
            "plugin": PLUGIN_KEY,
            "name": "设备管家",
            "id": plugin_id,
            "version": "0.4.1",
            "tags": ["tool", "monitoring"],
            "desc": "监控 NAS 系统、网络、硬盘健康与 Docker 容器资源",
            "developer": "Kingwell",
            "publisher": "community",
            "system": False,
            "port": "18080",
            "type": "standard",
            "ext": {"admin": True},
        },
        "timestamp": now,
        "changetime": now,
    }


def assert_id_available(registry: dict[str, Any], plugin_id: int) -> None:
    for key, record in registry.items():
        if key == PLUGIN_KEY or not isinstance(record, dict):
            continue
        info = record.get("info")
        if isinstance(info, dict) and str(info.get("id")) == str(plugin_id):
            raise RuntimeError(
                f"Plugin ID {plugin_id} is already used by {info.get('name') or key} ({key})"
            )


def write_registry(path: Path, registry: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".devicemanager.tmp")
    temporary.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    os.chmod(path, 0o644)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--plugin-id", type=int, default=11001)
    parser.add_argument("--registry", type=Path)
    arguments = parser.parse_args()
    if arguments.plugin_id < 1:
        raise RuntimeError("Plugin ID must be positive")
    registry_path = arguments.registry or Path(f"/data/plugin/{arguments.user_id}.list")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = load_registry(registry_path)
    assert_id_available(registry, arguments.plugin_id)
    if registry_path.exists():
        backup = registry_path.with_name(
            f"{registry_path.name}.before-devicemanager-{int(time.time())}.bak"
        )
        backup.write_bytes(registry_path.read_bytes())
        os.chmod(backup, 0o600)
    registry[PLUGIN_KEY] = plugin_record(arguments.plugin_id, int(time.time()))
    write_registry(registry_path, registry)
    print(f"Registered {PLUGIN_KEY} in {registry_path} with plugin ID {arguments.plugin_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Device Manager registration refused: {error}", file=sys.stderr)
        raise SystemExit(1)
