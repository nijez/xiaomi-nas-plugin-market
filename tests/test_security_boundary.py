"""Local fixtures only. Never connect to a NAS or real cloud account."""
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
from plugin_security import authorized_write, load_proxy_key, provision_proxy, session_key_v2, trusted_owner

KEY = "b" * 64
OWNER = {"X-Plugin-Proxy-Key": KEY, "X-Xiaomi-Client-Verify": "SUCCESS", "X-Xiaomi-Client-DN": "CN=nas.123.test.2,O=Xiaomi"}


class TrustTests(unittest.TestCase):
    def test_owner_requires_private_proxy_channel_and_exact_identity(self):
        self.assertTrue(trusted_owner(OWNER, "u123", KEY))
        for change in (
            {"X-Plugin-Proxy-Key": ""}, {"X-Plugin-Proxy-Key": "c" * 64},
            {"X-Xiaomi-Client-Verify": "FAILED", "X-Real-IP": "127.0.0.1"},
            {"X-Xiaomi-Client-DN": "CN=nas.1234.test.2"},
            {"X-Xiaomi-Client-DN": "CN=nas.999.test.2"},
            {"X-Xiaomi-Client-DN": "CN=nas.123.test.2,CN=nas.999.test.2"},
            {"X-Xiaomi-Client-DN": "OU=CN=nas.123.test.2"},
        ):
            self.assertFalse(trusted_owner(dict(OWNER, **change), "u123", KEY), change)
        self.assertFalse(trusted_owner(OWNER, "u123", ""))
        self.assertFalse(trusted_owner({"X-Real-IP": "::1"}, "u123", KEY))

    def test_new_sessions_are_bound_to_owner_plugin_and_auth_version(self):
        original = b"fixture-key"
        derived = session_key_v2(original, "webdav", "u123")
        self.assertNotEqual(original, derived)
        self.assertNotEqual(derived, session_key_v2(original, "webdav", "u456"))
        self.assertNotEqual(derived, session_key_v2(original, "qbittorrent", "u123"))
        payload = b"fixture-session"
        self.assertNotEqual(hmac.new(original, payload, hashlib.sha256).digest(), hmac.new(derived, payload, hashlib.sha256).digest())

    def test_provision_is_private_idempotent_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keyfile, template, config = root / "proxy.key", root / "template", root / "nginx.conf"
            template.write_text('proxy_set_header X-Plugin-Proxy-Key "__PLUGIN_PROXY_KEY__";')
            provision_proxy(template, config, keyfile)
            key = load_proxy_key(keyfile)
            self.assertEqual(64, len(key))
            self.assertEqual(0o600, config.stat().st_mode & 0o777)
            self.assertEqual(0o600, keyfile.stat().st_mode & 0o777)
            provision_proxy(template, config, keyfile)
            self.assertEqual(key, load_proxy_key(keyfile))
            keyfile.chmod(0o644)
            self.assertEqual("", load_proxy_key(keyfile))
            with self.assertRaises(ValueError):
                provision_proxy(template, config, keyfile)

    def test_write_requires_non_simple_request(self):
        self.assertTrue(authorized_write({"X-Plugin-Request": "1", "Content-Type": "application/json"}))
        self.assertFalse(authorized_write({"Content-Type": "text/plain"}))
        self.assertFalse(authorized_write({"Content-Type": "application/json"}))


class CloudBoundaryTests(unittest.TestCase):
    def test_cloud_endpoints_reject_unauthenticated_requests_before_side_effects(self):
        for project in ("xiaomi-115-sync-plugin", "xiaomi-aliyundrive-sync-plugin"):
            with self.subTest(project=project), tempfile.TemporaryDirectory() as directory:
                spec = importlib.util.spec_from_file_location(project.replace("-", "_"), ROOT / "projects" / project / "server.py")
                module = importlib.util.module_from_spec(spec)
                with patch.dict(os.environ, {"DATA_DIR": directory, "LOCAL_ROOT": directory}), \
                     patch.object(threading.Thread, "start"):
                    spec.loader.exec_module(module)
                service = Mock()
                service.status.return_value = {"ok": True, "fixture": True}
                service.unbind.return_value = {"ok": True}
                module.SERVICE = service
                server = ThreadingHTTPServer(("127.0.0.1", 0), module.ApiHandler)
                server.owner, server.proxy_key = "u123", KEY
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def request(method, route, headers=None):
                    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                    connection.request(method, route, body=b"{}" if method != "GET" else None, headers=headers or {})
                    response = connection.getresponse()
                    status, body = response.status, response.read()
                    connection.close()
                    return status, body
                try:
                    for method, route in (("GET", "/api/status"), ("GET", "/api/local/folders"), ("POST", "/api/auth/unbind"), ("POST", "/api/jobs")):
                        self.assertEqual(401, request(method, route)[0])
                        self.assertEqual(401, request(method, route, {"X-Real-IP": "127.0.0.1", "X-Xiaomi-Client-Verify": "SUCCESS", "X-Xiaomi-Client-DN": "CN=nas.123.test.2"})[0])
                    self.assertEqual([], service.mock_calls)
                    self.assertEqual((200, {"ok": True}), (lambda r: (r[0], json.loads(r[1])))(request("GET", "/healthz")))
                    self.assertEqual([], service.mock_calls)
                    self.assertEqual(200, request("GET", "/api/status", OWNER)[0])
                    self.assertEqual(403, request("POST", "/api/auth/unbind", OWNER)[0])
                    service.unbind.assert_not_called()
                    self.assertEqual(200, request("POST", "/api/auth/unbind", dict(OWNER, **{"Content-Type": "application/json", "X-Plugin-Request": "1"}))[0])
                    service.unbind.assert_called_once()
                    if project == "xiaomi-aliyundrive-sync-plugin":
                        self.assertEqual(401, request("DELETE", "/api/jobs/abc")[0])
                        service.remove_job.assert_not_called()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(3)


if __name__ == "__main__":
    unittest.main()
