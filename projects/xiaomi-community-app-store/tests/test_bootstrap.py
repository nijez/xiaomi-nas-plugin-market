import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "deploy"))
from apply_on_nas import apply_release, SERVICE
from storelib import InstallManager, StoreError


class BootstrapTests(unittest.TestCase):
    def candidate(self, root, name):
        release = root / "data/plugin/community-store/releases" / name
        (release / "catalog").mkdir(parents=True)
        for item in ("catalog.json", "catalog.json.sig", "repository-public.pem"):
            shutil.copy2(PROJECT / "catalog" / item, release / "catalog" / item)
        shutil.copytree(PROJECT / "deploy", release / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
        (release / "web/assets").mkdir(parents=True)
        shutil.copy2(PROJECT / "web/assets/community-store-v4.png", release / "web/assets/community-store-v4.png")
        return release.resolve()

    def test_bootstrap_success_keeps_secret_on_nas_and_renders_proxy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self.candidate(root, "candidate")
            apply_release(release, "u123", 11002, root=root, execute_system=False)
            current = root / "data/plugin/community-store/current"
            self.assertEqual(release, current.resolve())
            registry = json.loads((root / "data/plugin/u123.list").read_text())
            self.assertEqual(11002, registry["communitystore"]["info"]["id"])
            nginx = root / "etc/nginx/conf.d/luci/xiaomi-community-store.conf"
            self.assertNotIn("__PLUGIN_PROXY_KEY__", nginx.read_text())
            self.assertEqual(0o600, nginx.stat().st_mode & 0o777)
            self.assertNotIn("__NAS_USER_ID__", (root / "etc/systemd/system" / SERVICE).read_text())

    def test_bootstrap_failure_restores_first_install_or_upgrade(self):
        for upgrade in (False, True):
            for fault in ("nginx", "health", "registry"):
                with self.subTest(upgrade=upgrade, fault=fault), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    if upgrade:
                        old = self.candidate(root, "old")
                        apply_release(old, "u123", 11002, root=root, execute_system=False)
                    current = root / "data/plugin/community-store/current"
                    old_current = current.readlink() if current.is_symlink() else None
                    registry = root / "data/plugin/u123.list"
                    old_registry = registry.read_bytes() if registry.exists() else None
                    candidate = self.candidate(root, "new")
                    commands = []
                    failed = False
                    def run(manager, command):
                        nonlocal failed
                        commands.append(command)
                        if fault == "nginx" and command == ["nginx", "-t"] and not failed:
                            failed = True
                            raise StoreError("fixture nginx failure")
                        if command == ["systemctl", "stop", SERVICE]:
                            self.assertEqual(candidate, current.resolve())
                    with patch.object(InstallManager, "_service_state", return_value=(upgrade, "enabled" if upgrade else "disabled")), \
                         patch.object(InstallManager, "_run", autospec=True, side_effect=run), \
                         patch.object(InstallManager, "_wait_for_health", side_effect=StoreError("fixture health failure") if fault == "health" else None), \
                         patch("apply_on_nas.register_to", side_effect=StoreError("fixture registry failure") if fault == "registry" else None):
                        with self.assertRaises(StoreError):
                            apply_release(candidate, "u123", 11002, root=root)
                    self.assertEqual(old_current, current.readlink() if current.is_symlink() else None)
                    self.assertEqual(old_registry, registry.read_bytes() if registry.exists() else None)
                    if fault == "nginx":
                        self.assertNotIn(["systemctl", "stop", SERVICE], commands)
                    else:
                        self.assertIn(["systemctl", "stop", SERVICE], commands)
                        self.assertIn(["systemctl", "disable", SERVICE], commands)
                        self.assertEqual(upgrade, ["systemctl", "start", SERVICE] in commands)

    def test_foreign_release_is_rejected_before_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(StoreError, "outside"):
                apply_release(Path(temporary), "u123", 11002, root=Path(temporary), execute_system=False)
