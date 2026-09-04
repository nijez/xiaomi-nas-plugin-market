#!/usr/bin/env python3
"""Register only the Aliyun Drive Sync record in one Xiaomi user registry."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_KEY = "aliyundrivesync"
PLUGIN_UI_REVISION = "aliyundrive-20260902-2"


def load_registry(registry_path: Path) -> dict[str, Any]:
    if not registry_path.exists():
        return {}
    try:
        value = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取插件注册表：{error}") from error
    if not isinstance(value, dict):
        raise RuntimeError("插件注册表必须是 JSON 对象")
    return value


def plugin_record(plugin_id: int, now: int) -> dict[str, Any]:
    return {
        "status": "running",
        "install": True,
        "enable": True,
        "icon": "/icon/aliyundrive-sync.icon?v=aliyundrive-official-20260902",
        "frontend": {
            "title": "阿里云盘备份",
            "type": "url",
            "permission": ["admin"],
            "dev_type": [1, 2, 3, 4],
            "url": [
                {"dev_type": [1], "url": f"/index.html?v={PLUGIN_UI_REVISION}#/aliyunDriveSync_app"},
                {"dev_type": [2, 3, 4], "url": f"/index.html?v={PLUGIN_UI_REVISION}#/aliyunDriveSync_pc"},
            ],
            "sortid": plugin_id,
            "widget": [],
        },
        "info": {
            "plugin": PLUGIN_KEY,
            "name": "阿里云盘备份",
            "id": plugin_id,
            "version": "0.1.0",
            "tags": ["cloud", "backup"],
            "system": False,
            "port": "18117",
            "type": "standard",
            "ext": {"admin": True},
        },
        "timestamp": now,
        "changetime": now,
    }


def assert_id_is_available(registry: dict[str, Any], plugin_id: int) -> None:
    for key, record in registry.items():
        if key == PLUGIN_KEY or not isinstance(record, dict):
            continue
        info = record.get("info")
        if isinstance(info, dict) and str(info.get("id")) == str(plugin_id):
            raise RuntimeError(f"插件编号 {plugin_id} 已被 {info.get('name') or key} 使用")


def write_registry(registry_path: Path, registry: dict[str, Any]) -> None:
    temporary = registry_path.with_suffix(registry_path.suffix + ".aliyundrive-sync.tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(registry_path)
    os.chmod(registry_path, 0o644)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--plugin-id", type=int, default=1002)
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args()
    if args.plugin_id < 1:
        raise RuntimeError("插件编号必须为正整数")
    registry_path = args.registry or Path(f"/data/plugin/{args.user_id}.list")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = load_registry(registry_path)
    assert_id_is_available(registry, args.plugin_id)
    if registry_path.exists():
        backup = registry_path.with_name(f"{registry_path.name}.before-aliyundrive-sync-{int(time.time())}.bak")
        backup.write_bytes(registry_path.read_bytes())
        os.chmod(backup, 0o600)
    registry[PLUGIN_KEY] = plugin_record(args.plugin_id, int(time.time()))
    write_registry(registry_path, registry)
    print(f"registered {PLUGIN_KEY} with plugin ID {args.plugin_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Aliyun Drive Sync registration refused: {error}", file=sys.stderr)
        raise SystemExit(1)
