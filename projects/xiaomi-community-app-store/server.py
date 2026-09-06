#!/usr/bin/env python3
"""Local-only web service for the Xiaomi NAS community application store."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
import sys
import re
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from storelib import InstallManager, StoreError, load_verified_catalog

if (Path(__file__).resolve().parents[2] / "shared").is_dir():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
from plugin_security import load_proxy_key, trusted_owner, session_key_v2


PROJECT = Path(__file__).resolve().parent
WEB = PROJECT / "web"
CATALOG = PROJECT / "catalog"
PUBLIC_KEY = CATALOG / "repository-public.pem"
COOKIE_NAME = "xiaomi_community_store_session"
SESSION_TTL = 30 * 24 * 60 * 60
BASE_PATH = os.environ.get("BASE_PATH", "/")
ACTION_LOCK = threading.Lock()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class StoreHandler(BaseHTTPRequestHandler):
    server_version = "XiaomiCommunityStore/0.1"

    @property
    def app(self) -> "StoreServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} [{self.log_date_time_string()}] {format_string % args}")

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object, headers: dict[str, str] | None = None) -> None:
        self._send(status, json_bytes(value), "application/json; charset=utf-8", headers)

    def _session_id(self) -> str | None:
        header_session = self.headers.get("X-Community-Session", "").strip()
        if header_session:
            return header_session
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _session(self) -> dict[str, object] | None:
        session_id = self._session_id()
        if not session_id or not re.fullmatch(r"[0-9]{1,12}\.[A-Za-z0-9_-]{32}\.[a-f0-9]{64}", session_id):
            return None
        try:
            payload, provided = session_id.rsplit(".", 1)
            expiry_text, _random = payload.split(".", 1)
            expiry = int(expiry_text)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self.app.session_key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if expiry < int(time.time()) or not secrets.compare_digest(expected, provided):
            return None
        csrf = hmac.new(self.app.session_key, f"csrf:{session_id}".encode("ascii"), hashlib.sha256).hexdigest()
        return {"csrf": csrf, "expires": expiry}

    def _new_session(self) -> tuple[str, dict[str, object]]:
        expiry = int(time.time()) + SESSION_TTL
        payload = f"{expiry}.{secrets.token_urlsafe(24)}"
        signature = hmac.new(self.app.session_key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        session_id = f"{payload}.{signature}"
        csrf = hmac.new(self.app.session_key, f"csrf:{session_id}".encode("ascii"), hashlib.sha256).hexdigest()
        session = {"csrf": csrf, "expires": expiry}
        return session_id, session

    def _session_cookie(self, session_id: str) -> str:
        secure = "" if self.app.dev else "; Secure"
        cookie_path = "/" if self.app.dev else BASE_PATH
        return f"{COOKIE_NAME}={session_id}; Path={cookie_path}; HttpOnly; SameSite=Strict{secure}; Max-Age={SESSION_TTL}"

    def _trusted_xiaomi_client(self) -> bool:
        return trusted_owner(self.headers, self.app.owner, self.app.proxy_key)

    def _require_session(self, write: bool = False) -> dict[str, object] | None:
        session = self._session()
        if not session:
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "请从小米智能存储客户端重新打开插件市场"})
            return None
        if write and not secrets.compare_digest(str(session["csrf"]), self.headers.get("X-CSRF-Token", "")):
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "操作令牌已失效，请重新打开页面"})
            return None
        return session

    def _safe_file(self, root: Path, relative: str) -> Path | None:
        candidate = (root / unquote(relative).lstrip("/")).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _serve_file(self, path: Path) -> None:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        cache = "public, max-age=86400" if path.name != "index.html" else "no-store"
        self._send(HTTPStatus.OK, path.read_bytes(), mime, {"Cache-Control": cache})

    def _serve_index(self) -> None:
        session = self._session()
        session_id: str | None = None
        if not session and (self.app.dev or self._trusted_xiaomi_client()):
            session_id, session = self._new_session()
        csrf = str(session["csrf"]) if session else ""
        html = (WEB / "index.html").read_text(encoding="utf-8")
        html = html.replace("__CSRF_TOKEN__", csrf).replace("__SESSION_TOKEN__", (session_id or self._session_id() or "") if session else "")
        headers = {}
        if session_id:
            headers["Set-Cookie"] = self._session_cookie(session_id)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8", headers)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index()
            return
        if path == "/api/status":
            if not self._require_session():
                return
            self._json(HTTPStatus.OK, {"ok": True, "version": "0.1.3", "mode": "preview" if self.app.dev else "active"})
            return
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/catalog":
            if not self._require_session():
                return
            try:
                catalog = load_verified_catalog(CATALOG, PUBLIC_KEY)
                installed = self.app.manager.inventory() if self.app.manager else {}
                for package in catalog["packages"]:
                    inventory = installed.get(package["id"], {})
                    package["installedVersion"] = inventory.get("version")
                    package["managed"] = bool(inventory.get("managed"))
                self._json(HTTPStatus.OK, {"ok": True, "catalog": catalog, "preview": self.app.dev})
            except StoreError as error:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(error)})
            return
        if path.startswith("/catalog/icons/"):
            file_path = self._safe_file(CATALOG, path.removeprefix("/catalog/"))
        elif path.startswith("/catalog/"):
            if not self._require_session():
                return
            file_path = self._safe_file(CATALOG, path.removeprefix("/catalog/"))
        else:
            file_path = self._safe_file(WEB, path)
        if file_path:
            self._serve_file(file_path)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def _request_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise StoreError("Invalid request length") from error
        if length <= 0 or length > 4096:
            raise StoreError("Invalid request body")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise StoreError("Request body must be JSON") from error
        if not isinstance(value, dict):
            raise StoreError("Request body must be an object")
        return value

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/install", "/api/uninstall"):
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not self._require_session(write=True):
            return
        if self.app.dev or not self.app.manager:
            self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "本地预览模式不会修改 NAS"})
            return
        try:
            body = self._request_json()
            package_id = str(body.get("id", ""))
            if not ACTION_LOCK.acquire(blocking=False):
                raise StoreError("另一个安装任务正在执行")
            try:
                result = self.app.manager.install(package_id) if path == "/api/install" else self.app.manager.uninstall(package_id)
            finally:
                ACTION_LOCK.release()
            self._json(HTTPStatus.OK, result)
        except StoreError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
        except Exception as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"操作失败：{error}"})


class StoreServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], dev: bool, manager: InstallManager | None, admin_token: str, owner: str = "", proxy_key: str = ""):
        super().__init__(address, StoreHandler)
        self.dev = dev
        self.manager = manager
        self.owner = owner or (manager.user_id if manager else "")
        self.proxy_key = proxy_key
        self.session_key = session_key_v2(hashlib.sha256(admin_token.encode("utf-8")).digest(), "communitystore", self.owner)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "18119")))
    parser.add_argument("--user-id", default=os.environ.get("NAS_USER_ID", ""))
    parser.add_argument("--admin-token-file", type=Path, default=Path(os.environ.get("ADMIN_TOKEN_FILE", "/data/plugin/community-store/admin-token")))
    args = parser.parse_args()
    manager = None
    if not args.dev:
        if not args.user_id:
            raise SystemExit("NAS_USER_ID is required outside preview mode")
        manager = InstallManager(CATALOG, PUBLIC_KEY, args.user_id)
        try:
            admin_token = args.admin_token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise SystemExit(f"Cannot read admin token: {error}") from error
        if len(admin_token) < 20:
            raise SystemExit("Admin token is too short")
    else:
        admin_token = "preview-only-token-not-for-production"
    server = StoreServer((args.host, args.port), args.dev, manager, admin_token, args.user_id,
                         load_proxy_key(args.admin_token_file.parent / "proxy.key"))
    print(f"Community store listening on http://{args.host}:{args.port} ({'preview' if args.dev else 'active'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
