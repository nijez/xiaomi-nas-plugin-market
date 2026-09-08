#!/usr/bin/env python3
"""Register the community store without changing unrelated Xiaomi apps."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


PLUGIN_KEY = "communitystore"


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Xiaomi plugin registry must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--plugin-id", type=int, default=11002)
    args = parser.parse_args()
    return register_to(Path(f"/data/plugin/{args.user_id}.list"), args.user_id, args.plugin_id)


def register_to(path: Path, user_id: str, plugin_id: int) -> int:
    args = argparse.Namespace(user_id=user_id, plugin_id=plugin_id)
    if not args.user_id.replace("_", "").replace("-", "").isalnum() or args.plugin_id < 1:
        raise RuntimeError("Invalid user ID or plugin ID")
    registry = load(path)
    for key, record in registry.items():
        if key == PLUGIN_KEY or not isinstance(record, dict):
            continue
        info = record.get("info", {})
        if isinstance(info, dict) and str(info.get("id")) == str(args.plugin_id):
            raise RuntimeError(f"Plugin ID {args.plugin_id} is already used by {key}")
    now = int(time.time())
    registry[PLUGIN_KEY] = {
        "status": "running",
        "install": True,
        "enable": True,
        "icon": "/icon/community-store-v4.icon",
        "frontend": {
            "title": "插件市场",
            "desc": "可信第三方插件仓库",
            "type": "url",
            "permission": ["admin"],
            "dev_type": [1, 2, 3, 4],
            "url": [
                {"dev_type": [1], "url": "/index.html#/communityStore_app"},
                {"dev_type": [2, 3, 4], "url": "/index.html#/communityStore_pc"},
            ],
            "sortid": args.plugin_id,
            "widget": [],
        },
        "info": {
            "plugin": PLUGIN_KEY,
            "name": "插件市场",
            "id": args.plugin_id,
            "version": "0.1.3",
            "tags": ["store", "community"],
            "desc": "安装、更新和管理经过签名的社区插件",
            "developer": "Kingwell Community",
            "publisher": "community",
            "system": False,
            "port": "18119",
            "type": "standard",
            "ext": {"admin": True},
        },
        "timestamp": now,
        "changetime": now,
    }
    if path.exists():
        backup = path.with_name(f"{path.name}.before-communitystore-{now}.bak")
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, 0o600)
    temporary = path.with_name(path.name + ".communitystore.tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
