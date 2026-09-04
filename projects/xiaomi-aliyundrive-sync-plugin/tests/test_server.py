import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath


TEST_ROOT = Path(tempfile.mkdtemp(prefix="xiaomi-aliyun-sync-root-"))
TEST_DATA = Path(tempfile.mkdtemp(prefix="xiaomi-aliyun-sync-data-"))
os.environ["LOCAL_ROOT"] = str(TEST_ROOT)
os.environ["DATA_DIR"] = str(TEST_DATA)

MODULE_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("xiaomi_aliyun_sync_server", MODULE_PATH)
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

REGISTER_PATH = Path(__file__).resolve().parents[1] / "deploy" / "register_plugin.py"
REGISTER_SPEC = importlib.util.spec_from_file_location("xiaomi_aliyun_sync_register", REGISTER_PATH)
assert REGISTER_SPEC and REGISTER_SPEC.loader
register = importlib.util.module_from_spec(REGISTER_SPEC)
REGISTER_SPEC.loader.exec_module(register)


WEB_APP_PATH = Path(__file__).resolve().parents[1] / "web" / "app.js"


class PathSafetyTests(unittest.TestCase):
    def test_local_path_cannot_escape_storage_pool(self):
        outside = Path(tempfile.mkdtemp(prefix="xiaomi-aliyun-sync-outside-"))
        with self.assertRaises(server.ServiceError) as context:
            server.normalize_local_path(str(outside))
        self.assertEqual(context.exception.status, 400)

    def test_destination_rejects_parent_segments(self):
        target = TEST_ROOT / "restore"
        target.mkdir(exist_ok=True)
        self.assertEqual(server.safe_destination(target, PurePosixPath("a/b.txt")), (target / "a/b.txt").resolve())
        with self.assertRaises(server.ServiceError):
            server.safe_destination(target, PurePosixPath("../outside.txt"))

    def test_remote_identifier_is_constrained(self):
        self.assertEqual(server.safe_remote_id("root"), "root")
        for bad_value in ("", "../root", "root/name", "name with spaces", "name\nnext"):
            with self.assertRaises(server.ServiceError):
                server.safe_remote_id(bad_value)


class OAuthAndTransferTests(unittest.TestCase):
    def test_pkce_challenge_is_urlsafe_sha256(self):
        verifier = "abc-123"
        expected = hashlib.sha256(verifier.encode("utf-8")).digest()
        expected = __import__("base64").urlsafe_b64encode(expected).decode("ascii").rstrip("=")
        self.assertEqual(server.base64url_sha256(verifier), expected)

    def test_sha1_and_proof_are_deterministic(self):
        source = TEST_ROOT / "payload.bin"
        payload = (b"1234567890" * 400) + b"end"
        source.write_bytes(payload)
        full, pre = server.sha1_for_file(source)
        self.assertEqual(full, hashlib.sha1(payload).hexdigest())
        self.assertEqual(pre, hashlib.sha1(payload[:1024]).hexdigest())
        token = "local-token"
        offset = int(hashlib.md5(token.encode()).hexdigest()[:16], 16) % len(payload)
        expected = __import__("base64").b64encode(payload[offset:offset + 8]).decode("ascii")
        self.assertEqual(server.proof_code_for_file(source, token), expected)

    def test_pre_hash_match_is_not_reported_as_a_generic_error(self):
        self.assertEqual(server.AliyunApi._data({"code": "PreHashMatched"}), {"code": "PreHashMatched"})


class RegistryTests(unittest.TestCase):
    def test_registry_refuses_duplicate_numeric_id(self):
        with self.assertRaises(RuntimeError):
            register.assert_id_is_available({"other": {"info": {"id": 1002, "name": "其他"}}}, 1002)

    def test_registry_keeps_existing_records(self):
        with tempfile.TemporaryDirectory(prefix="xiaomi-aliyun-sync-registry-") as temporary:
            registry_path = Path(temporary) / "u1.list"
            data = {"other": {"info": {"id": 999, "name": "既有插件"}}}
            data[register.PLUGIN_KEY] = register.plugin_record(1002, 1234)
            register.write_registry(registry_path, data)
            loaded = register.load_registry(registry_path)
            self.assertEqual(loaded["other"]["info"]["name"], "既有插件")
            self.assertEqual(
                loaded[register.PLUGIN_KEY]["frontend"]["url"][0]["url"],
                f"/index.html?v={register.PLUGIN_UI_REVISION}#/aliyunDriveSync_app",
            )


class FrontendRoutingTests(unittest.TestCase):
    def test_api_requests_are_anchored_to_the_plugin_script_url(self):
        script = WEB_APP_PATH.read_text(encoding="utf-8")
        self.assertIn("const apiUrl = (route) => new URL(`api/${route}`, assetBase).href;", script)
        self.assertIn("fetch(apiUrl(route),", script)


class AuthorizationBoundaryTests(unittest.TestCase):
    def setUp(self):
        server.STORE.mutate(lambda config: (config.clear(), config.update(server.default_config())))

    def tearDown(self):
        server.STORE.mutate(lambda config: (config.clear(), config.update(server.default_config())))

    def test_job_creation_requires_authorized_account(self):
        with self.assertRaises(server.ServiceError) as context:
            server.SERVICE.create_job({"direction": "upload", "localPath": str(TEST_ROOT), "remoteFolderId": "root"})
        self.assertEqual(context.exception.status, 401)

    def test_unbind_disables_scheduled_jobs(self):
        def authorize(config):
            config["client_id"] = "test-client"
            config["token"] = {"access_token": "token", "expires_at": server.epoch_now() + 3600}
            config["account"] = {"label": "test", "drive_id": "drive"}
            config["jobs"] = [{"id": "job1", "enabled": True, "schedule": "hourly"}]

        server.STORE.mutate(authorize)
        server.SERVICE.unbind()
        status = server.SERVICE.status()
        self.assertFalse(status["authorized"])
        self.assertFalse(status["jobs"][0]["enabled"])


if __name__ == "__main__":
    unittest.main()
