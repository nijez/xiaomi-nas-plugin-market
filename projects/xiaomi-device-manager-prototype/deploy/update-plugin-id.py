#!/usr/bin/env python3
"""Safely move a Xiaomi NAS custom plugin away from a reserved app ID."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update exactly one custom plugin ID in a Xiaomi NAS registry."
    )
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--plugin", default="devicemanager")
    parser.add_argument("--expected-id", type=int, default=999)
    parser.add_argument("--new-id", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    registry_path = args.registry
    with registry_path.open("r", encoding="utf-8") as registry_file:
        registry = json.load(registry_file)

    plugin = registry.get(args.plugin)
    if not isinstance(plugin, dict):
        raise SystemExit(f"Plugin {args.plugin!r} was not found in {registry_path}.")

    info = plugin.get("info")
    if not isinstance(info, dict) or info.get("plugin") != args.plugin:
        raise SystemExit(f"Registry entry {args.plugin!r} is not the expected plugin.")

    previous_id = info.get("id")
    if previous_id != args.expected_id:
        raise SystemExit(
            f"Expected {args.plugin!r} to use ID {args.expected_id}, found {previous_id!r}."
        )

    for key, value in registry.items():
        if key == args.plugin or not isinstance(value, dict):
            continue
        existing_id = value.get("info", {}).get("id")
        if existing_id == args.new_id:
            raise SystemExit(f"ID {args.new_id} is already used by {key!r}.")

    backup_path = registry_path.with_name(
        f"{registry_path.name}.before-{args.plugin}-id-{int(time.time())}.bak"
    )
    shutil.copy2(registry_path, backup_path)

    info["id"] = args.new_id
    plugin["changetime"] = int(time.time())

    original_mode = stat.S_IMODE(registry_path.stat().st_mode)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=registry_path.parent, delete=False
    ) as temporary_file:
        json.dump(registry, temporary_file, ensure_ascii=False, indent="\t")
        temporary_file.write("\n")
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
        temporary_path = Path(temporary_file.name)

    os.chmod(temporary_path, original_mode)
    os.replace(temporary_path, registry_path)

    print(
        json.dumps(
            {
                "plugin": args.plugin,
                "previousId": previous_id,
                "newId": args.new_id,
                "backup": str(backup_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
