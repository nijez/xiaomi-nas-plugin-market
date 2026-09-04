#!/usr/bin/env python3
"""Register the 115 Sync app in one Xiaomi Smart Storage user's app list.

This script deliberately changes only the 115 Sync metadata record. It refuses
to reuse another app's numeric ID and writes a timestamped backup first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_KEY = "115sync"


def load_registry(registry_path: Path) -> dict[str, Any]:
    if not registry_path.exists():
        return {}
    try:
        loaded = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to read plugin registry: {error}") from error
    if not isinstance(loaded, dict):
        raise RuntimeError("Plugin registry must be a JSON object")
    return loaded


def plugin_record(plugin_id: int, now: int) -> dict[str, Any]:
    return {
        "status": "running",
        "install": True,
        "enable": True,
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
            "sortid": plugin_id,
            "widget": [],
        },
        "info": {
            "plugin": PLUGIN_KEY,
            "name": "115 云备份",
            "id": plugin_id,
            "version": "0.1.0",
            "tags": ["cloud", "backup"],
            "system": False,
            "port": "18115",
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
            name = str(info.get("name") or key)
            raise RuntimeError(f"Plugin ID {plugin_id} is already used by {name} ({key})")


def write_registry(registry_path: Path, registry: dict[str, Any]) -> None:
    temporary = registry_path.with_suffix(registry_path.suffix + ".115sync.tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(registry_path)
    os.chmod(registry_path, 0o644)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--plugin-id", type=int, default=1000)
    parser.add_argument("--registry", type=Path)
    arguments = parser.parse_args()

    if arguments.plugin_id < 1:
        raise RuntimeError("Plugin ID must be a positive integer")
    registry_path = arguments.registry or Path(f"/data/plugin/{arguments.user_id}.list")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = load_registry(registry_path)
    assert_id_is_available(registry, arguments.plugin_id)

    if registry_path.exists():
        backup = registry_path.with_name(f"{registry_path.name}.before-115sync-{int(time.time())}.bak")
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
        print(f"115 Sync registration refused: {error}", file=sys.stderr)
        raise SystemExit(1)
