from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
from offline_dependencies import verify_bundle

from storelib import (
    InstallManager,
    StoreError,
    load_verified_catalog,
    safe_extract_bundle,
    validate_manifest,
    verify_detached_signature,
    installation_lock,
)


PROJECT = Path(__file__).resolve().parents[1]
CATALOG = PROJECT / "catalog"
PUBLIC_KEY = CATALOG / "repository-public.pem"


class StoreLibraryTests(unittest.TestCase):
    def test_old_online_requirements_bundle_cannot_activate(self) -> None:
        def without_lock(requirements):
            requirements.with_name('requirements.lock').unlink(missing_ok=True)
            return verify_bundle(requirements)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "data/plugin/u_test.list"
            registry.parent.mkdir(parents=True)
            registry.write_text("{}\n")
            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=True)
            with patch.object(manager, "_service_state", return_value=(False, "disabled")), \
                 patch.object(manager, "_run") as run, \
                 patch('storelib.verify_bundle', side_effect=without_lock):
                with self.assertRaisesRegex(StoreError, "requirements.lock"):
                    manager.install("115sync")
                run.assert_not_called()
            self.assertEqual(registry.read_text(), "{}\n")
            self.assertFalse((root / "data/plugin/115-sync/current").exists())

    def test_two_installers_cannot_enter_the_transaction_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "install.lock"
            with installation_lock(lock):
                with self.assertRaisesRegex(StoreError, "in progress"):
                    with installation_lock(lock):
                        self.fail("Competing installation entered")
            with installation_lock(lock):
                pass

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
                {"version": next(p['version'] for p in load_verified_catalog(CATALOG, PUBLIC_KEY)['packages'] if p['id'] == 'aliyundrivesync'), "managed": True},
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

    def test_failed_install_restores_files_and_service_prestate(self) -> None:
        for previous in (None, (False, "disabled"), (True, "enabled"), (True, "enabled-runtime")):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                registry = root / "data/plugin/u_test.list"
                registry.parent.mkdir(parents=True)
                registry.write_text("{}\n")
                manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=False)
                if previous is not None:
                    manager.install("aliyundrivesync")
                current = root / "data/plugin/aliyundrive-sync/current"
                old_target = current.readlink() if current.is_symlink() else None
                state_file = manager.state_dir / "aliyundrivesync.json"
                old_state = state_file.read_bytes() if state_file.exists() else None
                old_registry = registry.read_bytes()
                commands = []
                service = "xiaomi-aliyundrive-sync.service"
                def run(command):
                    commands.append(command)
                    if command == ["systemctl", "stop", service]:
                        self.assertTrue(current.exists(), "candidate was deleted while still running")
                manager.execute_system = True
                with patch.object(manager, "_service_state", return_value=previous or (False, "disabled")), \
                     patch.object(manager, "_run", side_effect=run), \
                     patch.object(manager, "_wait_for_health", side_effect=StoreError("fixture unhealthy")):
                    with self.assertRaisesRegex(StoreError, "fixture unhealthy"):
                        manager.install("aliyundrivesync")
                self.assertIn(["systemctl", "stop", service], commands)
                self.assertIn(["systemctl", "disable", service], commands)
                self.assertEqual(old_registry, registry.read_bytes())
                self.assertEqual(old_target, current.readlink() if current.is_symlink() else None)
                self.assertEqual(old_state, state_file.read_bytes() if state_file.exists() else None)
                starts = [command for command in commands if command[:2] == ["systemctl", "start"]]
                self.assertEqual(bool(previous and previous[0]), bool(starts))
                enables = [command for command in commands if command[:2] == ["systemctl", "enable"]]
                self.assertEqual(2 if previous and previous[1].startswith("enabled") else 1, len(enables))
                if previous and previous[1] == "enabled-runtime":
                    self.assertEqual(["systemctl", "enable", "--runtime", service], enables[-1])

    def test_cannot_stop_candidate_preserves_recovery_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "data/plugin/u_test.list"
            registry.parent.mkdir(parents=True)
            registry.write_text("{}\n")
            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=True)
            def run(command):
                if command[:2] == ["systemctl", "stop"]:
                    raise StoreError("fixture stop failed")
            with patch.object(manager, "_service_state", return_value=(False, "disabled")), \
                 patch.object(manager, "_run", side_effect=run), \
                 patch.object(manager, "_wait_for_health", side_effect=StoreError("fixture unhealthy")):
                with self.assertRaisesRegex(StoreError, "rollback incomplete"):
                    manager.install("aliyundrivesync")
            self.assertTrue((root / "data/plugin/aliyundrive-sync/current").exists())
            self.assertEqual("{}\n", registry.read_text())


if __name__ == "__main__":
    unittest.main()
