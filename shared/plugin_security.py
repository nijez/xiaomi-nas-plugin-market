"""Authentication of a private nginx channel and its verified Xiaomi owner.

This is not isolation between same-origin plugins or processes sharing a UID.
No client-provided forwarded address can bootstrap authentication.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import stat
from pathlib import Path


def load_proxy_key(path: Path) -> str:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            return ""
        value = path.read_text(encoding="ascii").strip()
        return value if re.fullmatch(r"[a-f0-9]{64}", value) else ""
    except (OSError, UnicodeError):
        return ""


def trusted_owner(headers, owner: str, proxy_key: str) -> bool:
    if not re.fullmatch(r"u[0-9]+", owner) or not re.fullmatch(r"[a-f0-9]{64}", proxy_key):
        return False
    supplied = headers.get("X-Plugin-Proxy-Key", "")
    if not re.fullmatch(r"[a-f0-9]{64}", supplied) or not hmac.compare_digest(supplied, proxy_key):
        return False
    if headers.get("X-Xiaomi-Client-Verify", "") != "SUCCESS":
        return False
    # nginx uses the RFC2253 subject DN. Reject ambiguous or escaped subjects.
    dn = headers.get("X-Xiaomi-Client-DN", "")
    if "\\" in dn or len(dn) > 2048:
        return False
    common_names = re.findall(r"(?:^|,)\s*CN=([^,]+)", dn)
    return len(common_names) == 1 and bool(re.fullmatch(r"nas\." + owner[1:] + r"\.[A-Za-z0-9.-]+", common_names[0]))


def session_key_v2(key: bytes, plugin: str, owner: str) -> bytes:
    # Invalidates sessions minted through the previous loopback/any-cert bypass.
    return hmac.new(key, f"owner-session-v2:{plugin}:{owner}".encode(), hashlib.sha256).digest()


def authorized_write(headers) -> bool:
    # Cross-origin forms cannot supply this header. No CORS/preflight is granted.
    return headers.get("X-Plugin-Request", "") == "1" and headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json"


def provision_proxy(template: Path, target: Path, keyfile: Path) -> None:
    text = template.read_text(encoding="utf-8")
    if "__PLUGIN_PROXY_KEY__" not in text:
        raise ValueError("Private proxy marker is missing")
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w") as output:
            output.write(secrets.token_hex(32) + "\n")
            output.flush()
            os.fsync(output.fileno())
    key = load_proxy_key(keyfile)
    if not key:
        raise ValueError("Proxy key missing, invalid or not private (0600 required)")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text.replace("__PLUGIN_PROXY_KEY__", key))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    provision_proxy(args.template, args.target, args.key_file)
