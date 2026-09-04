import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath


TEST_ROOT = Path(tempfile.mkdtemp(prefix="xiaomi-115-sync-test-root-"))
TEST_DATA = Path(tempfile.mkdtemp(prefix="xiaomi-115-sync-test-data-"))
os.environ["LOCAL_ROOT"] = str(TEST_ROOT)
os.environ["DATA_DIR"] = str(TEST_DATA)

MODULE_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("xiaomi_115_sync_server", MODULE_PATH)
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

REGISTER_PATH = Path(__file__).resolve().parents[1] / "deploy" / "register_plugin.py"
REGISTER_SPEC = importlib.util.spec_from_file_location("xiaomi_115_sync_register", REGISTER_PATH)
assert REGISTER_SPEC and REGISTER_SPEC.loader
register = importlib.util.module_from_spec(REGISTER_SPEC)
REGISTER_SPEC.loader.exec_module(register)


class PathSafetyTests(unittest.TestCase):
    def test_normalize_local_path_rejects_outside_storage_pool(self):
        outside = Path(tempfile.mkdtemp(prefix="xiaomi-115-sync-outside-"))
        with self.assertRaises(server.ServiceError) as context:
            server.normalize_local_path(str(outside))
        self.assertEqual(context.exception.status, 400)

    def test_safe_destination_keeps_files_below_selected_folder(self):
        target_root = TEST_ROOT / "restore"
        target_root.mkdir(exist_ok=True)
        destination = server.safe_destination(target_root, PurePosixPath("camera/2026/photo.jpg"))
        self.assertEqual(destination, target_root / "camera/2026/photo.jpg")
        self.assertTrue(destination.parent.is_dir())
        with self.assertRaises(server.ServiceError):
            server.safe_destination(target_root, PurePosixPath("../outside.jpg"))

    def test_remote_names_cannot_escape_the_backup_tree(self):
        for unsafe in ("", ".", "..", "a/b", "a\\b", "name\nnext"):
            with self.assertRaises(server.ServiceError):
                server.safe_component(unsafe)


class TransferMetadataTests(unittest.TestCase):
    def test_sha1_uses_full_file_and_first_128_kib_prefix(self):
        source = TEST_ROOT / "source.bin"
        payload = (b"abc123" * 30_000) + b"end"
        source.write_bytes(payload)
        full_sha1, prefix_sha1 = server.sha1_for_file(source)
        self.assertEqual(full_sha1, hashlib.sha1(payload).hexdigest().upper())
        self.assertEqual(prefix_sha1, hashlib.sha1(payload[: 128 * 1024]).hexdigest().upper())

    def test_conflict_name_keeps_original_extension(self):
        name = server.conflict_name("archive.tar.gz", "nas")
        self.assertRegex(name, r"^archive\.tar\.nas-\d{8}-\d{6}\.gz$")

    def test_token_response_never_accepts_missing_refresh_token(self):
        with self.assertRaises(server.ServiceError):
            server.SyncService._token_from_payload({"data": {"access_token": "token", "expires_in": 3600}})
        token = server.SyncService._token_from_payload(
            {"data": {"access_token": "token", "refresh_token": "refresh", "expires_in": 3600}}
        )
        self.assertEqual(token["access_token"], "token")
        self.assertEqual(token["refresh_token"], "refresh")
        self.assertGreater(token["expires_at"], server.epoch_now())


class RegistryTests(unittest.TestCase):
    def test_registration_refuses_to_reuse_another_plugins_id(self):
        registry = {"existing": {"info": {"id": 1000, "name": "其他插件"}}}
        with self.assertRaises(RuntimeError):
            register.assert_id_is_available(registry, 1000)

    def test_registration_only_adds_its_own_record(self):
        with tempfile.TemporaryDirectory(prefix="xiaomi-115-sync-registry-") as temporary:
            registry_path = Path(temporary) / "u1.list"
            registry = {"other": {"info": {"id": 999, "name": "现有插件"}}}
            registry[register.PLUGIN_KEY] = register.plugin_record(1000, 1234)
            register.write_registry(registry_path, registry)
            loaded = register.load_registry(registry_path)
            self.assertEqual(loaded["other"]["info"]["id"], 999)
            self.assertEqual(loaded[register.PLUGIN_KEY]["info"]["name"], "115 云备份")
            self.assertEqual(loaded[register.PLUGIN_KEY]["icon"], "/icon/115-sync.icon?v=115life-38.2.0")


class AuthorizationBoundaryTests(unittest.TestCase):
    def setUp(self):
        def reset(config):
            config.clear()
            config.update(server.default_config())

        server.STORE.mutate(reset)

    def tearDown(self):
        def reset(config):
            config.clear()
            config.update(server.default_config())

        server.STORE.mutate(reset)

    def test_task_creation_requires_an_authorized_account(self):
        payload = {
            "name": "需要授权",
            "direction": "upload",
            "localPath": str(TEST_ROOT),
            "remoteCid": "0",
            "remoteLabel": "115 根目录",
            "schedule": "hourly",
        }
        with self.assertRaises(server.ServiceError) as context:
            server.SERVICE.create_job(payload)
        self.assertEqual(context.exception.status, 401)

    def test_unbinding_pauses_scheduled_jobs(self):
        def authorize(config):
            config["client_id"] = "test-app"
            config["token"] = {
                "access_token": "test-access-token",
                "refresh_token": "test-refresh-token",
                "expires_at": server.epoch_now() + 3600,
            }
            config["account"] = {"label": "test account"}

        server.STORE.mutate(authorize)
        job = server.SERVICE.create_job(
            {
                "name": "定时备份",
                "direction": "upload",
                "localPath": str(TEST_ROOT),
                "remoteCid": "0",
                "remoteLabel": "115 根目录",
                "schedule": "hourly",
            }
        )
        self.assertTrue(job["enabled"])
        server.SERVICE.unbind()
        stored = server.SERVICE.status()["jobs"]
        self.assertFalse(stored[0]["enabled"])


if __name__ == "__main__":
    unittest.main()
