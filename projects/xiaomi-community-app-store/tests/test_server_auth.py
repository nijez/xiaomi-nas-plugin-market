from __future__ import annotations

import http.client
import json
import threading
import unittest

from server import StoreServer


class ServerAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = StoreServer(("127.0.0.1", 0), False, None, "a" * 32, "u123", "b" * 64)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(encoded))
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, response_headers, payload

    def test_untrusted_request_cannot_open_catalog(self) -> None:
        status, _, _ = self.request("GET", "/api/catalog")
        self.assertEqual(401, status)
        status, headers, payload = self.request("POST", "/api/unlock", {"code": "a" * 32})
        self.assertEqual(404, status)

    def test_public_icons_do_not_expose_bundles(self) -> None:
        status, _, _ = self.request("GET", "/catalog/icons/devicemanager.png")
        self.assertEqual(200, status)
        status, _, _ = self.request("GET", "/catalog/bundles/devicemanager-0.4.1.bundle.zip")
        self.assertEqual(401, status)

    def test_verified_xiaomi_client_gets_session_from_index(self) -> None:
        status, headers, payload = self.request(
            "GET",
            "/index.html",
            headers={"X-Xiaomi-Client-Verify": "SUCCESS", "X-Xiaomi-Client-DN": "CN=nas.123.test.2", "X-Plugin-Proxy-Key": "b" * 64},
        )
        self.assertEqual(200, status)
        html = payload.decode("utf-8")
        marker = '<meta name="session-token" content="'
        session = html.split(marker, 1)[1].split('"', 1)[0]
        self.assertTrue(session)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, payload = self.request("GET", "/api/catalog", headers={"X-Community-Session": session})
        self.assertEqual(200, status)
        self.assertEqual(5, len(json.loads(payload)["catalog"]["packages"]))

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = StoreServer(("127.0.0.1", 0), False, None, "a" * 32, "u123", "b" * 64)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        status, _, _ = self.request("GET", "/api/catalog", headers={"Cookie": cookie})
        self.assertEqual(200, status)

    def test_loopback_and_spoofed_or_other_owner_certificate_mint_no_session(self) -> None:
        for headers in (
            {"X-Real-IP": "127.1.0.110"},
            {"X-Xiaomi-Client-Verify": "SUCCESS"},
            {"X-Xiaomi-Client-Verify": "SUCCESS", "X-Xiaomi-Client-DN": "CN=nas.123.test.2"},
            {"X-Xiaomi-Client-Verify": "SUCCESS", "X-Xiaomi-Client-DN": "CN=nas.999.test.2", "X-Plugin-Proxy-Key": "b" * 64},
        ):
            status, result_headers, payload = self.request("GET", "/index.html", headers=headers)
            self.assertEqual(200, status)
            self.assertNotIn("Set-Cookie", result_headers)
            self.assertIn(b'<meta name="session-token" content=""', payload)

    def test_invalid_session_is_not_reflected_into_html(self) -> None:
        _, _, payload = self.request("GET", "/index.html", headers={"X-Community-Session": '\"><img src=x onerror=alert(1)>'})
        self.assertNotIn(b"onerror=", payload)


if __name__ == "__main__":
    unittest.main()
