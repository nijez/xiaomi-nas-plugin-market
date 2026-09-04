#!/usr/bin/env python3
"""Path-aware reverse proxy for embedding Jingsu ERP in a Xiaomi NAS plugin."""

from __future__ import annotations

import http.client
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit


LISTEN_HOST = os.environ.get("ERP_PROXY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("ERP_PROXY_LISTEN_PORT", "18118"))
UPSTREAM_HOST = os.environ.get("ERP_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("ERP_UPSTREAM_PORT", "8080"))
UPSTREAM_TIMEOUT = float(os.environ.get("ERP_UPSTREAM_TIMEOUT", "30"))
MAX_REWRITE_BODY = int(os.environ.get("ERP_MAX_REWRITE_BODY", str(16 * 1024 * 1024)))

PLUGIN_BASE_RE = re.compile(r"^/plugin/[A-Za-z0-9_-]+/erplink$")
MOBILE_USER_AGENT_RE = re.compile(r"Android|iPhone|iPad|Mobile", re.I)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
REWRITABLE_PREFIXES = (
    "/api/",
    "/assets/",
    "/uploads/",
    "/help/",
    "/icon.svg",
    "/manifest.webmanifest",
    "/version.json",
)


def normalize_plugin_base(value: Optional[str]) -> str:
    candidate = (value or "").rstrip("/")
    if not PLUGIN_BASE_RE.fullmatch(candidate):
        raise ValueError("invalid plugin base")
    return candidate


def bootstrap_script(plugin_base: str, canonical_path: Optional[str] = None) -> str:
    encoded_base = json.dumps(plugin_base, ensure_ascii=True)
    encoded_prefixes = json.dumps(REWRITABLE_PREFIXES, ensure_ascii=True)
    encoded_canonical_path = json.dumps(canonical_path, ensure_ascii=True)
    return f"""<script>
(() => {{
  const base = {encoded_base};
  const prefixes = {encoded_prefixes};
  const canonicalPath = {encoded_canonical_path};
  if (canonicalPath && (window.location.pathname !== canonicalPath || window.location.hash)) {{
    window.history.replaceState(window.history.state, '', canonicalPath + window.location.search);
  }}
  const rewrite = (value) => {{
    if (typeof value !== 'string' || !value) return value;
    let parsed;
    try {{ parsed = new URL(value, window.location.href); }} catch (_) {{ return value; }}
    if (parsed.origin !== window.location.origin || parsed.pathname.startsWith(base + '/')) return value;
    if (!prefixes.some((prefix) => parsed.pathname === prefix || parsed.pathname.startsWith(prefix))) return value;
    return base + parsed.pathname + parsed.search + parsed.hash;
  }};

  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
    return originalOpen.call(this, method, rewrite(String(url)), ...rest);
  }};

  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {{
    if (typeof input === 'string' || input instanceof URL) return originalFetch(rewrite(String(input)), init);
    if (input instanceof Request) return originalFetch(new Request(rewrite(input.url), input), init);
    return originalFetch(input, init);
  }};

  const originalWindowOpen = window.open.bind(window);
  window.open = (url, ...rest) => originalWindowOpen(typeof url === 'string' ? rewrite(url) : url, ...rest);
}})();
</script>"""


def rewrite_html(
    body: bytes, plugin_base: str, canonical_path: Optional[str] = None
) -> bytes:
    text = body.decode("utf-8")
    attribute_pattern = re.compile(r"\b(src|href|action)=(['\"])/(?!/)", flags=re.I)
    text = attribute_pattern.sub(lambda match: f"{match.group(1)}={match.group(2)}{plugin_base}/", text)
    injection = (
        f'<base href="{plugin_base}/">'
        f'{bootstrap_script(plugin_base, canonical_path)}'
    )
    text = re.sub(r"<head(\s*[^>]*)>", lambda match: match.group(0) + injection, text, count=1, flags=re.I)
    return text.encode("utf-8")


def rewrite_text_paths(body: bytes, plugin_base: str) -> bytes:
    text = body.decode("utf-8")
    for prefix in REWRITABLE_PREFIXES:
        text = text.replace(prefix, f"{plugin_base}{prefix}")
    return text.encode("utf-8")


def rewrite_response_body(
    body: bytes,
    content_type: str,
    plugin_base: str,
    canonical_path: Optional[str] = None,
) -> bytes:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if len(body) > MAX_REWRITE_BODY:
        return body
    if media_type == "text/html":
        return rewrite_html(body, plugin_base, canonical_path)
    if media_type in {
        "application/json",
        "application/manifest+json",
        "text/css",
    }:
        return rewrite_text_paths(body, plugin_base)
    return body


def restore_request_body(body: bytes, content_type: str, plugin_base: str) -> bytes:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in {"application/json", "application/x-www-form-urlencoded", "text/plain"}:
        return body.replace(f"{plugin_base}/".encode(), b"/")
    return body


def rewrite_location(value: str, plugin_base: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname != UPSTREAM_HOST:
        return value
    if parsed.scheme and parsed.port not in {None, UPSTREAM_PORT}:
        return value
    path = parsed.path or "/"
    if path.startswith(plugin_base + "/"):
        return value
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{plugin_base}{path}{query}{fragment}"


def upstream_target(request_target: str, plugin_base: str) -> str:
    parsed = urlsplit(request_target)
    path = parsed.path
    if path == plugin_base:
        path = "/"
    elif path.startswith(plugin_base + "/"):
        path = path[len(plugin_base) :]
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{path or '/'}{query}"


def entry_target(
    request_target: str, plugin_base: str, user_agent: str
) -> Optional[Tuple[str, str]]:
    parsed = urlsplit(request_target)
    if parsed.path not in {
        plugin_base,
        plugin_base + "/",
        plugin_base + "/index.html",
    }:
        return None
    if MOBILE_USER_AGENT_RE.search(user_agent):
        return "/m/", plugin_base + "/m/"
    return "/", plugin_base + "/"


def forwarded_headers(headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for name, value in headers:
        lowered = name.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered in {"host", "content-length", "accept-encoding"}:
            continue
        result[name] = value
    result["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
    result["Accept-Encoding"] = "identity"
    return result


class ERPProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "JingsuERPPluginProxy/0.1"

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def _health(self) -> None:
        payload = json.dumps(
            {"status": "ok", "upstream": f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"},
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _proxy(self) -> None:
        if self.path == "/__health":
            self._health()
            return

        try:
            plugin_base = normalize_plugin_base(self.headers.get("X-ERP-Plugin-Base"))
        except ValueError:
            self.send_error(400, "Missing or invalid plugin base")
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        request_body = self.rfile.read(content_length) if content_length else b""
        request_body = restore_request_body(request_body, self.headers.get("Content-Type", ""), plugin_base)
        headers = forwarded_headers(self.headers.items())
        if request_body:
            headers["Content-Length"] = str(len(request_body))
        entry = entry_target(self.path, plugin_base, self.headers.get("User-Agent", ""))
        target, canonical_path = (
            entry if entry is not None else (upstream_target(self.path, plugin_base), None)
        )

        connection = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT)
        try:
            connection.request(self.command, target, body=request_body or None, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
        except (OSError, http.client.HTTPException) as error:
            self.send_error(502, f"ERP upstream unavailable: {error}")
            return
        finally:
            connection.close()

        content_type = response.getheader("Content-Type", "")
        try:
            rewritten_body = rewrite_response_body(
                response_body, content_type, plugin_base, canonical_path
            )
        except UnicodeDecodeError:
            rewritten_body = response_body

        self.send_response(response.status, response.reason)
        for name, value in response.getheaders():
            lowered = name.lower()
            if lowered in HOP_BY_HOP_HEADERS or lowered in {"content-length", "content-encoding"}:
                continue
            if lowered == "location":
                value = rewrite_location(value, plugin_base)
            elif lowered == "set-cookie":
                value = re.sub(r"(?i)\bPath=/([;]?)", f"Path={plugin_base}/\\1", value)
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(rewritten_body)))
        self.send_header("X-ERP-Proxy", "xiaomi-plugin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(rewritten_body)

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}", flush=True)


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ERPProxyHandler)
    print(
        f"Jingsu ERP plugin proxy listening on {LISTEN_HOST}:{LISTEN_PORT}, "
        f"upstream {UPSTREAM_HOST}:{UPSTREAM_PORT}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
