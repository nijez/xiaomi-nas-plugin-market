from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from storelib import (
    InstallManager,
    StoreError,
    load_verified_catalog,
    safe_extract_bundle,
    validate_manifest,
    verify_detached_signature,
)


PROJECT = Path(__file__).resolve().parents[1]
CATALOG = PROJECT / "catalog"
PUBLIC_KEY = CATALOG / "repository-public.pem"


class StoreLibraryTests(unittest.TestCase):
    def test_catalog_and_all_bundles_verify(self) -> None:
        catalog = load_verified_catalog(CATALOG, PUBLIC_KEY)
        self.assertEqual({'devicemanager', '115sync', 'aliyundrivesync', 'webdav', 'qbittorrent'}, {p['id'] for p in catalog['packages']})
        for package in catalog["packages"]:
            verify_detached_signature(
                CATALOG / package["bundle"],
                CATALOG / package["signature"],
                PUBLIC_KEY,
            )

    def test_all_bundle_manifests_are_valid(self) -> None:
        catalog = load_verified_catalog(CATALOG, PUBLIC_KEY)
        with tempfile.TemporaryDirectory() as temporary:
            for package in catalog["packages"]:
                target = Path(temporary) / package["id"]
                target.mkdir()
                safe_extract_bundle(CATALOG / package["bundle"], target)
                manifest = validate_manifest(json.loads((target / "manifest.json").read_text(encoding="utf-8")))
                self.assertEqual(package["id"], manifest["id"])
                self.assertEqual(package["version"], manifest["version"])

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bad.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("../escape", "bad")
            with self.assertRaises(StoreError):
                safe_extract_bundle(bundle, Path(temporary) / "out")

    def test_tampered_catalog_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "catalog.json"
            signature = root / "catalog.json.sig"
            payload.write_text("{}\n", encoding="utf-8")
            signature.write_bytes((CATALOG / "catalog.json.sig").read_bytes())
            with self.assertRaises(StoreError):
                verify_detached_signature(payload, signature, PUBLIC_KEY)

    def test_dry_run_install_and_uninstall_preserve_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "data/plugin/u_test.list"
            registry.parent.mkdir(parents=True)
            registry.write_text("{}\n", encoding="utf-8")
            data_dir = root / "data/plugin/aliyundrive-sync/data"
            data_dir.mkdir(parents=True)
            marker = data_dir / "keep.txt"
            marker.write_text("user data", encoding="utf-8")
            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=False)
            result = manager.install("aliyundrivesync")
            self.assertTrue(result["ok"])
            self.assertIn("aliyundrivesync", json.loads(registry.read_text(encoding="utf-8")))
            self.assertEqual(
                {"version": "0.1.0", "managed": True},
                manager.inventory()["aliyundrivesync"],
            )
            current = root / "data/plugin/aliyundrive-sync/current"
            release = current.resolve()
            self.assertTrue(release.is_dir())
            result = manager.uninstall("aliyundrivesync")
            self.assertTrue(result["dataPreserved"])
            self.assertEqual("user data", marker.read_text(encoding="utf-8"))
            self.assertFalse(current.exists())
            self.assertFalse(release.exists())
            self.assertNotIn("aliyundrivesync", json.loads(registry.read_text(encoding="utf-8")))

    def test_inventory_marks_registry_only_plugin_as_external(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "data/plugin/u_test.list"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps({"devicemanager": {"info": {"version": "0.4.1"}}}),
                encoding="utf-8",
            )
            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=False)
            self.assertEqual(
                {"version": "0.4.1", "managed": False},
                manager.inventory()["devicemanager"],
            )


if __name__ == "__main__":
    unittest.main()
