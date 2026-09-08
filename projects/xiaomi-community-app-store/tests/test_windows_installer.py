from __future__ import annotations

import http.server
import subprocess
import sys
import threading
import shutil
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        pass


class WindowsInstallerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh") and Path("/bin/sh").exists(), "Requires pwsh and a POSIX fixture shell; not a real Windows install")
    def test_native_transport_and_certificate_discovery(self) -> None:
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(PROJECT / "tests/windows_transport.ps1"), "-Installer", str(PROJECT / "install-windows.ps1")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("PASS:", result.stdout)

    def test_healthcheck_works_without_remote_shell_quoting(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT / "deploy" / "healthcheck.py"),
                    "--url",
                    f"http://127.0.0.1:{port}/healthz",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_platform_installers_use_the_same_activation_transaction(self) -> None:
        installer = (PROJECT / "install-windows.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn("python3 -c", installer)
        self.assertIn('"deploy\\apply_on_nas.py"', installer)
        self.assertIn("$remoteRelease/deploy/apply_on_nas.py", installer)
        self.assertIn("${REMOTE_RELEASE}/deploy/apply_on_nas.py", (PROJECT / "deploy/install-on-nas.sh").read_text())
        self.assertNotIn("systemctl restart", installer)
        self.assertNotIn("$sessionSecret", installer)

    def test_offline_installer_helper_is_shipped_on_both_platforms(self) -> None:
        windows = (PROJECT / "install-windows.ps1").read_text(encoding="utf-8-sig")
        shell = (PROJECT / "deploy/install-on-nas.sh").read_text()
        release = (PROJECT / "scripts/build_release.py").read_text()
        for source in (windows, shell, release):
            self.assertIn("offline_dependencies.py", source)
        cloud = (PROJECT.parent / "xiaomi-115-sync-plugin/deploy/install-on-nas.sh").read_text()
        self.assertNotIn("pip install", cloud)
        self.assertNotIn("select-pip-mirror.sh", cloud)
        self.assertLess(cloud.index("verify --requirements"), cloud.index('ssh "${SSH_OPTIONS[@]}"'))
        self.assertLess(cloud.index("install --requirements"), cloud.index('tar -C "${PROJECT_DIR}/web"'))


if __name__ == "__main__":
    unittest.main()
