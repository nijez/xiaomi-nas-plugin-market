import importlib.util
import os
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "proxy_server.py"
SPEC = importlib.util.spec_from_file_location("erp_proxy_server", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ERPProxyRewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = "/plugin/u123456/erplink"

    def test_plugin_base_validation(self) -> None:
        self.assertEqual(MODULE.normalize_plugin_base(self.base + "/"), self.base)
        with self.assertRaises(ValueError):
            MODULE.normalize_plugin_base("/plugin/../../erplink")

    def test_html_gets_base_bootstrap_and_asset_prefix(self) -> None:
        source = b'<html><head><script src="/assets/app.js"></script></head><body></body></html>'
        output = MODULE.rewrite_html(source, self.base, self.base + "/m/").decode()
        self.assertIn(f'<base href="{self.base}/">', output)
        self.assertIn(f'src="{self.base}/assets/app.js"', output)
        self.assertIn(f'const canonicalPath = "{self.base}/m/"', output)
        self.assertIn("window.history.replaceState", output)
        self.assertLess(output.index("XMLHttpRequest.prototype.open"), output.index("/assets/app.js"))

    def test_json_paths_are_scoped_and_request_paths_are_restored(self) -> None:
        source = b'{"image":"/uploads/products/a.png","api":"/api/auth/me"}'
        rewritten = MODULE.rewrite_response_body(source, "application/json", self.base)
        self.assertIn(f'"{self.base}/uploads/products/a.png"'.encode(), rewritten)
        self.assertEqual(MODULE.restore_request_body(rewritten, "application/json", self.base), source)

    def test_external_redirect_is_not_rewritten(self) -> None:
        self.assertEqual(
            MODULE.rewrite_location("https://example.com/login", self.base),
            "https://example.com/login",
        )

    def test_plugin_prefix_is_removed_before_upstream_request(self) -> None:
        self.assertEqual(
            MODULE.upstream_target(f"{self.base}/api/health?full=1", self.base),
            "/api/health?full=1",
        )

    def test_mobile_entry_is_served_without_an_http_redirect(self) -> None:
        self.assertEqual(
            MODULE.entry_target(
                self.base + "/index.html", self.base, "Mozilla/5.0 Android"
            ),
            ("/m/", self.base + "/m/"),
        )
        self.assertEqual(
            MODULE.entry_target(self.base + "/index.html", self.base, "Mozilla/5.0 Macintosh"),
            ("/", self.base + "/"),
        )
        self.assertIsNone(
            MODULE.entry_target(self.base + "/login", self.base, "Mozilla/5.0 Android")
        )


if __name__ == "__main__":
    unittest.main()
