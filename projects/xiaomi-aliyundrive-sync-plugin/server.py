#!/usr/bin/env python3
"""Local-only Aliyun Drive backup service for Xiaomi Smart Storage.

The Xiaomi client serves the browser UI. This process binds only to loopback and
is reached through the NAS's same-origin nginx plugin route. Account tokens are
kept in the plugin's protected data directory and are never returned to the UI.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "18117"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/plugin/aliyundrive-sync/data"))
LOCAL_ROOT = Path(os.environ.get("LOCAL_ROOT", "/nas/pool0"))
CONFIG_PATH = DATA_DIR / "config.json"
EVENTS_PATH = DATA_DIR / "events.json"
MAX_EVENT_COUNT = 800
MAX_REMOTE_ITEMS = 200_000
MAX_PART_SIZE = 2 * 1024 * 1024
REQUEST_TIMEOUT = 45
USER_AGENT = "Xiaomi-NAS-AliyunDriveSync/0.1"
API_BASE = "https://openapi.alipan.com"
BUNDLE_ID = "com.xiaomi.smartstorage.aliyundrive.sync"
ALLOWED_SCHEDULES = {"manual", "hourly", "every6h", "daily"}


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def epoch_now() -> int:
    return int(time.time())


def safe_message(value: Any, fallback: str) -> str:
    message = str(value or fallback).replace("\n", " ").replace("\r", " ").strip()
    return message[:360] or fallback


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass


def atomic_json_write(path: Path, payload: Any, mode: int = 0o600) -> None:
    ensure_data_dir()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, mode)
    except OSError:
        pass
    temporary.replace(path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "client_id": "",
        "token": None,
        "account": None,
        "jobs": [],
    }


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = default_config()
        self._events: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        ensure_data_dir()
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = {**default_config(), **loaded}
                if not isinstance(self._data.get("jobs"), list):
                    self._data["jobs"] = []
        except (OSError, json.JSONDecodeError):
            pass
        try:
            loaded_events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded_events, list):
                self._events = [item for item in loaded_events if isinstance(item, dict)][-MAX_EVENT_COUNT:]
        except (OSError, json.JSONDecodeError):
            pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def mutate(self, mutation: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            mutation(self._data)
            atomic_json_write(CONFIG_PATH, self._data)
            return copy.deepcopy(self._data)

    def append_event(self, job_id: str | None, level: str, message: str) -> None:
        event = {"at": iso_now(), "job_id": job_id, "level": level, "message": safe_message(message, "Unknown event")}
        with self._lock:
            self._events.append(event)
            self._events = self._events[-MAX_EVENT_COUNT:]
            atomic_json_write(EVENTS_PATH, self._events)

    def events(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = self._events if not job_id else [item for item in self._events if item.get("job_id") == job_id]
            return copy.deepcopy(items[-160:][::-1])


def normalize_local_path(raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ServiceError(400, "请选择 NAS 文件夹")
    try:
        candidate = Path(raw_path).expanduser().resolve(strict=False)
        root = LOCAL_ROOT.resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, ValueError) as error:
        raise ServiceError(400, "NAS 文件夹必须位于 /nas/pool0 内") from error
    return candidate


def safe_component(value: Any) -> str:
    name = str(value or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name or "\n" in name or "\r" in name:
        raise ServiceError(400, "文件名包含不安全字符")
    return name[:255]


def safe_remote_id(value: Any, *, field: str = "云端文件夹") -> str:
    remote_id = str(value or "").strip()
    if not remote_id or len(remote_id) > 180 or not re.fullmatch(r"[A-Za-z0-9._:-]+", remote_id):
        raise ServiceError(400, f"{field}无效")
    return remote_id


def safe_destination(base: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ServiceError(400, "云端文件路径无效")
    for part in relative.parts:
        safe_component(part)
    destination = (base / Path(*relative.parts)).resolve(strict=False)
    try:
        destination.relative_to(base.resolve(strict=False))
    except ValueError as error:
        raise ServiceError(400, "云端文件路径越界") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def conflict_name(name: str, origin: str) -> str:
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stem}.{origin}-{timestamp}{suffix}"


def sha1_for_file(file_path: Path) -> tuple[str, str]:
    digest = hashlib.sha1()
    pre_hash = hashlib.sha1()
    pre_hash_remaining = 1024
    with file_path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if pre_hash_remaining:
                part = chunk[:pre_hash_remaining]
                pre_hash.update(part)
                pre_hash_remaining -= len(part)
    return digest.hexdigest(), pre_hash.hexdigest()


def proof_code_for_file(file_path: Path, access_token: str) -> str:
    size = file_path.stat().st_size
    if size <= 0:
        return ""
    offset = int(hashlib.md5(access_token.encode("utf-8")).hexdigest()[:16], 16) % size
    with file_path.open("rb") as source:
        source.seek(offset)
        payload = source.read(8)
    return base64.b64encode(payload).decode("ascii")


def base64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def parse_json_object(raw: bytes, error_message: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as error:
        raise ServiceError(502, error_message) from error
    if not isinstance(payload, dict):
        raise ServiceError(502, error_message)
    return payload


class HttpClient:
    @staticmethod
    def json_request(method: str, url: str, *, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(url, method=method, data=data, headers=request_headers)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = parse_json_object(response.read(), "阿里云盘接口返回异常")
        except HTTPError as error:
            detail = safe_message(error.read().decode("utf-8", "replace"), "请求失败")
            raise ServiceError(502, f"阿里云盘接口请求失败（{error.code}）：{detail}") from error
        except URLError as error:
            raise ServiceError(502, f"无法连接阿里云盘接口：{safe_message(error.reason, '网络错误')}") from error
        except TimeoutError as error:
            raise ServiceError(504, "阿里云盘接口请求超时") from error
        return payload

    @staticmethod
    def put_file(upload_url: str, source: Path, start: int, length: int) -> None:
        with source.open("rb") as input_file:
            input_file.seek(start)
            payload = input_file.read(length)
        request = Request(upload_url, method="PUT", data=payload, headers={"User-Agent": USER_AGENT, "Content-Length": str(len(payload))})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                if response.status not in {200, 201, 204, 409}:
                    raise ServiceError(502, f"上传分片失败（{response.status}）")
        except HTTPError as error:
            if error.code == 409:
                return
            raise ServiceError(502, f"上传分片失败（{error.code}）") from error
        except (URLError, TimeoutError) as error:
            raise ServiceError(502, f"上传分片网络错误：{safe_message(error, '网络错误')}") from error

    @staticmethod
    def download(url: str, destination: Path) -> None:
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            temporary.replace(destination)
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ServiceError(502, f"文件下载失败：{safe_message(error, '网络错误')}") from error


class AliyunApi:
    @staticmethod
    def _data(payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("code") or "") == "PreHashMatched":
            return {"code": "PreHashMatched"}
        if payload.get("code") not in {None, "", 0, "0", "Success"} and not payload.get("access_token"):
            message = payload.get("message") or payload.get("error_description") or payload.get("error") or "阿里云盘接口返回错误"
            raise ServiceError(502, safe_message(message, "阿里云盘接口返回错误"))
        nested = payload.get("data")
        return nested if isinstance(nested, dict) else payload

    @staticmethod
    def _headers(access_token: str) -> dict[str, str]:
        return {"Authorization": access_token}

    def start_qrcode(self, client_id: str, verifier: str) -> dict[str, Any]:
        payload = HttpClient.json_request(
            "POST",
            f"{API_BASE}/oauth/authorize/qrcode",
            body={
                "client_id": client_id,
                "bundle_id": BUNDLE_ID,
                "scopes": ["user:base", "file:all:read", "file:all:write"],
                "source": "app",
                "code_challenge": base64url_sha256(verifier),
                "code_challenge_method": "S256",
            },
        )
        data = self._data(payload)
        if not data.get("sid") or not data.get("qrCodeUrl"):
            raise ServiceError(502, "阿里云盘未返回授权二维码")
        return data

    def qrcode_status(self, sid: str) -> dict[str, Any]:
        return self._data(HttpClient.json_request("GET", f"{API_BASE}/oauth/qrcode/{sid}/status"))

    def exchange_code(self, client_id: str, code: str, verifier: str) -> dict[str, Any]:
        return self._data(HttpClient.json_request(
            "POST",
            f"{API_BASE}/oauth/access_token",
            body={"client_id": client_id, "grant_type": "authorization_code", "code": code, "code_verifier": verifier},
        ))

    def user_info(self, access_token: str) -> dict[str, Any]:
        return self._data(HttpClient.json_request("GET", f"{API_BASE}/oauth/users/info", headers=self._headers(access_token)))

    def drive_info(self, access_token: str) -> dict[str, Any]:
        return self._data(HttpClient.json_request("POST", f"{API_BASE}/adrive/v1.0/user/getDriveInfo", body={}, headers=self._headers(access_token)))

    def space_info(self, access_token: str) -> dict[str, Any]:
        return self._data(HttpClient.json_request("POST", f"{API_BASE}/adrive/v1.0/user/getSpaceInfo", body={}, headers=self._headers(access_token)))

    def list_items(self, access_token: str, drive_id: str, parent_file_id: str) -> list[dict[str, Any]]:
        marker = ""
        items: list[dict[str, Any]] = []
        while True:
            body: dict[str, Any] = {"drive_id": drive_id, "parent_file_id": parent_file_id, "limit": 100, "order_by": "name", "order_direction": "ASC"}
            if marker:
                body["marker"] = marker
            data = self._data(HttpClient.json_request("POST", f"{API_BASE}/adrive/v1.0/openFile/list", body=body, headers=self._headers(access_token)))
            page = data.get("items") or data.get("file_list") or []
            if not isinstance(page, list):
                raise ServiceError(502, "阿里云盘文件列表格式异常")
            items.extend(item for item in page if isinstance(item, dict))
            if len(items) > MAX_REMOTE_ITEMS:
                raise ServiceError(400, "云端目录文件过多，已停止本次同步")
            marker = str(data.get("next_marker") or data.get("marker") or "")
            if not marker:
                return items

    def create_folder(self, access_token: str, drive_id: str, parent_file_id: str, name: str) -> dict[str, Any]:
        return self._data(HttpClient.json_request(
            "POST",
            f"{API_BASE}/adrive/v1.0/openFile/create",
            body={"drive_id": drive_id, "parent_file_id": parent_file_id, "name": name, "type": "folder", "check_name_mode": "refuse"},
            headers=self._headers(access_token),
        ))

    def create_upload(self, access_token: str, drive_id: str, parent_file_id: str, file_path: Path, name: str, include_full_hash: bool) -> dict[str, Any]:
        size = file_path.stat().st_size
        if size > MAX_PART_SIZE * 1000:
            raise ServiceError(400, "单个文件超过当前官方分片上传上限")
        part_count = max(1, (size + MAX_PART_SIZE - 1) // MAX_PART_SIZE)
        body: dict[str, Any] = {
            "drive_id": drive_id,
            "parent_file_id": parent_file_id,
            "name": name,
            "type": "file",
            "check_name_mode": "auto_rename",
            "size": size,
            "part_info_list": [{"part_number": index + 1} for index in range(part_count)],
        }
        full_hash, pre_hash = sha1_for_file(file_path)
        if include_full_hash:
            body.update({"content_hash": full_hash, "content_hash_name": "sha1", "proof_code": proof_code_for_file(file_path, access_token), "proof_version": "v1"})
        elif size >= 512 * 1024:
            body["pre_hash"] = pre_hash
        return self._data(HttpClient.json_request("POST", f"{API_BASE}/adrive/v1.0/openFile/create", body=body, headers=self._headers(access_token)))

    def complete_upload(self, access_token: str, drive_id: str, file_id: str, upload_id: str) -> dict[str, Any]:
        return self._data(HttpClient.json_request(
            "POST",
            f"{API_BASE}/adrive/v1.0/openFile/complete",
            body={"drive_id": drive_id, "file_id": file_id, "upload_id": upload_id},
            headers=self._headers(access_token),
        ))

    def download_url(self, access_token: str, drive_id: str, file_id: str) -> str:
        data = self._data(HttpClient.json_request(
            "POST",
            f"{API_BASE}/adrive/v1.0/openFile/getDownloadUrl",
            body={"drive_id": drive_id, "file_id": file_id, "expire_sec": 3600},
            headers=self._headers(access_token),
        ))
        url = str(data.get("url") or "")
        if not url.startswith("https://"):
            raise ServiceError(502, "阿里云盘未返回安全下载地址")
        return url


class SyncService:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.api = AliyunApi()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aliyundrive-sync")
        self._runs: dict[str, dict[str, Any]] = {}
        self._runs_lock = threading.RLock()
        self._auth: dict[str, dict[str, Any]] = {}
        self._auth_lock = threading.RLock()
        self._scheduler = threading.Thread(target=self._scheduler_loop, daemon=True, name="aliyundrive-scheduler")
        self._scheduler.start()

    def _authorized_config(self) -> dict[str, Any]:
        config = self.store.snapshot()
        token = config.get("token")
        if not isinstance(token, dict) or not token.get("access_token"):
            raise ServiceError(401, "请先完成阿里云盘授权")
        if int(token.get("expires_at") or 0) <= epoch_now() + 30:
            raise ServiceError(401, "阿里云盘授权已过期，请重新扫码授权")
        if not isinstance(config.get("account"), dict) or not config["account"].get("drive_id"):
            raise ServiceError(401, "阿里云盘授权信息不完整，请重新扫码授权")
        return config

    def status(self) -> dict[str, Any]:
        config = self.store.snapshot()
        token = config.get("token") if isinstance(config.get("token"), dict) else None
        authorized = bool(token and token.get("access_token") and int(token.get("expires_at") or 0) > epoch_now() + 30 and config.get("account"))
        with self._runs_lock:
            runs = copy.deepcopy(self._runs)
        jobs: list[dict[str, Any]] = []
        for raw_job in config.get("jobs", []):
            if not isinstance(raw_job, dict):
                continue
            job = copy.deepcopy(raw_job)
            job["run"] = runs.get(str(job.get("id")))
            jobs.append(job)
        account = copy.deepcopy(config.get("account")) if authorized else None
        return {
            "ok": True,
            "clientConfigured": bool(config.get("client_id")),
            "authorized": authorized,
            "account": account,
            "jobs": jobs,
            "localRoot": str(LOCAL_ROOT),
            "updatedAt": iso_now(),
            "bundleId": BUNDLE_ID,
        }

    def configure_client(self, client_id: Any) -> None:
        value = str(client_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{4,160}", value):
            raise ServiceError(400, "App ID 格式不正确")

        def mutate(config: dict[str, Any]) -> None:
            changed = config.get("client_id") != value
            config["client_id"] = value
            if changed:
                config["token"] = None
                config["account"] = None
                for job in config.get("jobs", []):
                    if isinstance(job, dict):
                        job["enabled"] = False

        self.store.mutate(mutate)
        self.store.append_event(None, "info", "已保存阿里云盘 App ID，等待扫码授权")

    def start_auth(self) -> dict[str, Any]:
        config = self.store.snapshot()
        client_id = str(config.get("client_id") or "")
        if not client_id:
            raise ServiceError(400, "请先填写阿里云盘 App ID")
        verifier = secrets.token_urlsafe(64)
        data = self.api.start_qrcode(client_id, verifier)
        sid = safe_remote_id(data.get("sid"), field="授权会话")
        with self._auth_lock:
            self._auth[sid] = {"verifier": verifier, "expires_at": epoch_now() + 600}
        return {"sid": sid, "qrcode": str(data["qrCodeUrl"]), "expiresAt": epoch_now() + 600}

    def poll_auth(self, sid: str) -> dict[str, Any]:
        sid = safe_remote_id(sid, field="授权会话")
        with self._auth_lock:
            pending = self._auth.get(sid)
        if not pending or int(pending.get("expires_at") or 0) <= epoch_now():
            raise ServiceError(410, "授权二维码已过期，请重新生成")
        status = self.api.qrcode_status(sid)
        stage = str(status.get("status") or "WaitLogin")
        if stage in {"WaitLogin", "Waiting"}:
            return {"stage": "waiting"}
        if stage in {"ScanSuccess", "Scanned"}:
            return {"stage": "scanned"}
        if stage in {"QRCodeExpired", "Expired"}:
            with self._auth_lock:
                self._auth.pop(sid, None)
            raise ServiceError(410, "授权二维码已过期，请重新生成")
        if stage not in {"LoginSuccess", "Success"}:
            message = status.get("message") or status.get("error") or "授权状态异常"
            raise ServiceError(502, safe_message(message, "授权状态异常"))
        code = str(status.get("authCode") or status.get("auth_code") or "")
        if not code:
            raise ServiceError(502, "阿里云盘未返回授权码")
        config = self.store.snapshot()
        token_data = self.api.exchange_code(str(config.get("client_id") or ""), code, str(pending["verifier"]))
        access_token = str(token_data.get("access_token") or "")
        if not access_token:
            raise ServiceError(502, "阿里云盘未返回访问令牌")
        expires_in = int(token_data.get("expires_in") or 0)
        if expires_in <= 0:
            expires_in = 30 * 24 * 3600
        user = self.api.user_info(access_token)
        drive = self.api.drive_info(access_token)
        drive_id = str(drive.get("default_drive_id") or drive.get("drive_id") or "")
        if not drive_id:
            raise ServiceError(502, "阿里云盘未返回默认云盘")
        label = str(user.get("nick_name") or user.get("user_name") or user.get("name") or "已连接阿里云盘账户")
        token = {"access_token": access_token, "expires_at": epoch_now() + expires_in}
        account = {"label": label[:120], "drive_id": drive_id}

        def mutate(saved: dict[str, Any]) -> None:
            saved["token"] = token
            saved["account"] = account

        self.store.mutate(mutate)
        with self._auth_lock:
            self._auth.pop(sid, None)
        self.store.append_event(None, "success", "阿里云盘账户已完成授权")
        return {"stage": "authorized"}

    def unbind(self) -> None:
        def mutate(config: dict[str, Any]) -> None:
            config["token"] = None
            config["account"] = None
            for job in config.get("jobs", []):
                if isinstance(job, dict):
                    job["enabled"] = False

        self.store.mutate(mutate)
        self.store.append_event(None, "info", "已清除本机保存的阿里云盘授权")

    def local_folders(self, raw_path: str | None) -> dict[str, Any]:
        current = normalize_local_path(raw_path or str(LOCAL_ROOT))
        if not current.exists() or not current.is_dir():
            raise ServiceError(404, "NAS 文件夹不存在")
        folders: list[dict[str, str]] = []
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError as error:
            raise ServiceError(502, f"无法读取 NAS 文件夹：{safe_message(error, '读取失败')}") from error
        for child in children:
            try:
                if child.is_symlink() or not child.is_dir():
                    continue
                normalized = normalize_local_path(str(child))
                folders.append({"name": child.name, "path": str(normalized)})
            except (OSError, ServiceError):
                continue
        parent = current.parent if current != LOCAL_ROOT.resolve(strict=False) else None
        parent_text = None
        if parent:
            try:
                parent.relative_to(LOCAL_ROOT.resolve(strict=False))
                parent_text = str(parent)
            except ValueError:
                parent_text = None
        return {"path": str(current), "parent": parent_text, "folders": folders}

    def remote_folders(self, parent_id: Any) -> dict[str, Any]:
        config = self._authorized_config()
        parent = safe_remote_id(parent_id or "root", field="云端文件夹")
        account = config["account"]
        token = config["token"]
        entries = self.api.list_items(str(token["access_token"]), str(account["drive_id"]), parent)
        folders = []
        for item in entries:
            if str(item.get("type") or "") != "folder":
                continue
            file_id = str(item.get("file_id") or item.get("id") or "")
            name = str(item.get("name") or "")
            if file_id and name:
                folders.append({"id": file_id, "name": name})
        return {"parentId": parent, "folders": folders}

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._authorized_config()
        direction = str(payload.get("direction") or "upload")
        if direction not in {"upload", "download"}:
            raise ServiceError(400, "同步方向无效")
        local_path = normalize_local_path(str(payload.get("localPath") or ""))
        remote_folder_id = safe_remote_id(payload.get("remoteFolderId") or "root", field="阿里云盘文件夹")
        remote_label = str(payload.get("remoteLabel") or "阿里云盘根目录").strip()[:180]
        schedule = str(payload.get("schedule") or "manual")
        if schedule not in ALLOWED_SCHEDULES:
            raise ServiceError(400, "执行方式无效")
        name = str(payload.get("name") or ("NAS 上传备份" if direction == "upload" else "阿里云盘下载备份")).strip()
        if not name:
            raise ServiceError(400, "请输入任务名称")
        job = {
            "id": uuid.uuid4().hex,
            "name": name[:80],
            "direction": direction,
            "localPath": str(local_path),
            "remoteFolderId": remote_folder_id,
            "remoteLabel": remote_label,
            "schedule": schedule,
            "enabled": schedule != "manual",
            "lastRunAt": None,
            "lastResult": None,
            "lastSummary": None,
            "lastScheduledAt": None,
            "manifest": {},
        }

        def mutate(config: dict[str, Any]) -> None:
            config["jobs"].append(job)

        self.store.mutate(mutate)
        self.store.append_event(job["id"], "info", f"已创建同步任务：{job['name']}")
        return job

    def toggle_job(self, job_id: str, enabled: bool) -> None:
        def mutate(config: dict[str, Any]) -> None:
            job = self._job_from_config(config, job_id)
            job["enabled"] = bool(enabled)

        self.store.mutate(mutate)

    def remove_job(self, job_id: str) -> None:
        with self._runs_lock:
            if job_id in self._runs:
                raise ServiceError(409, "任务正在同步，完成后再删除")

        def mutate(config: dict[str, Any]) -> None:
            jobs = config.get("jobs", [])
            original_count = len(jobs)
            config["jobs"] = [job for job in jobs if not isinstance(job, dict) or str(job.get("id")) != job_id]
            if len(config["jobs"]) == original_count:
                raise ServiceError(404, "同步任务不存在")

        self.store.mutate(mutate)
        self.store.append_event(job_id, "info", "已删除同步任务；NAS 和云端文件不会被删除")

    @staticmethod
    def _job_from_config(config: dict[str, Any], job_id: str) -> dict[str, Any]:
        for job in config.get("jobs", []):
            if isinstance(job, dict) and str(job.get("id")) == job_id:
                return job
        raise ServiceError(404, "同步任务不存在")

    def run_job(self, job_id: str, reason: str = "manual") -> None:
        config = self._authorized_config()
        job = next((item for item in config.get("jobs", []) if isinstance(item, dict) and str(item.get("id")) == job_id), None)
        if not job:
            raise ServiceError(404, "同步任务不存在")
        with self._runs_lock:
            if job_id in self._runs:
                raise ServiceError(409, "该任务正在同步")
            if self._runs:
                raise ServiceError(409, "已有任务正在同步，请稍后再试")
            self._runs[job_id] = {"startedAt": iso_now(), "progress": {"done": 0, "total": 0, "file": "准备同步"}, "reason": reason}
        self.executor.submit(self._execute_job, copy.deepcopy(job), reason)

    def _set_progress(self, job_id: str, done: int, total: int, file_name: str) -> None:
        with self._runs_lock:
            run = self._runs.get(job_id)
            if run:
                run["progress"] = {"done": done, "total": total, "file": file_name[:180]}

    def _execute_job(self, job: dict[str, Any], reason: str) -> None:
        job_id = str(job["id"])
        summary: dict[str, int] = {"copied": 0, "skipped": 0, "failed": 0}
        try:
            self.store.append_event(job_id, "info", "开始同步" if reason == "manual" else "开始定时同步")
            config = self._authorized_config()
            if job["direction"] == "upload":
                summary = self._sync_upload(job, config)
            else:
                summary = self._sync_download(job, config)
            self._finish_job(job_id, "success", summary)
            self.store.append_event(job_id, "success", f"同步完成：复制 {summary['copied']}，跳过 {summary['skipped']}")
        except Exception as error:  # Keep the service alive after one job failure.
            message = error.message if isinstance(error, ServiceError) else safe_message(error, "同步失败")
            self._finish_job(job_id, "error", summary)
            self.store.append_event(job_id, "error", f"同步失败：{message}")
        finally:
            with self._runs_lock:
                self._runs.pop(job_id, None)

    def _finish_job(self, job_id: str, result: str, summary: dict[str, int]) -> None:
        def mutate(config: dict[str, Any]) -> None:
            job = self._job_from_config(config, job_id)
            job["lastRunAt"] = iso_now()
            job["lastResult"] = result
            job["lastSummary"] = summary

        self.store.mutate(mutate)

    def _save_manifest(self, job_id: str, manifest: dict[str, Any]) -> None:
        def mutate(config: dict[str, Any]) -> None:
            job = self._job_from_config(config, job_id)
            job["manifest"] = manifest

        self.store.mutate(mutate)

    def _access(self, config: dict[str, Any]) -> tuple[str, str]:
        token = config.get("token") or {}
        account = config.get("account") or {}
        return str(token["access_token"]), str(account["drive_id"])

    def _remote_folder(self, access_token: str, drive_id: str, parent_id: str, name: str, cache: dict[tuple[str, str], str]) -> str:
        key = (parent_id, name)
        if key in cache:
            return cache[key]
        for item in self.api.list_items(access_token, drive_id, parent_id):
            if str(item.get("type") or "") == "folder" and str(item.get("name") or "") == name:
                folder_id = str(item.get("file_id") or item.get("id") or "")
                if folder_id:
                    cache[key] = folder_id
                    return folder_id
        created = self.api.create_folder(access_token, drive_id, parent_id, name)
        folder_id = str(created.get("file_id") or created.get("id") or "")
        if not folder_id:
            # A simultaneous create can be reported as a conflict; recover by listing once.
            for item in self.api.list_items(access_token, drive_id, parent_id):
                if str(item.get("type") or "") == "folder" and str(item.get("name") or "") == name:
                    folder_id = str(item.get("file_id") or item.get("id") or "")
                    if folder_id:
                        break
        if not folder_id:
            raise ServiceError(502, f"无法创建云端文件夹：{name}")
        cache[key] = folder_id
        return folder_id

    def _sync_upload(self, job: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
        local_root = normalize_local_path(str(job["localPath"]))
        if not local_root.exists() or not local_root.is_dir():
            raise ServiceError(404, "NAS 源文件夹不存在")
        access_token, drive_id = self._access(config)
        root_folder_id = safe_remote_id(job["remoteFolderId"], field="阿里云盘文件夹")
        files: list[Path] = []
        for candidate in local_root.rglob("*"):
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                files.append(candidate)
                if len(files) > MAX_REMOTE_ITEMS:
                    raise ServiceError(400, "NAS 文件过多，已停止本次同步")
            except OSError:
                continue
        files.sort(key=lambda item: str(item.relative_to(local_root)).casefold())
        manifest = job.get("manifest") if isinstance(job.get("manifest"), dict) else {}
        manifest = copy.deepcopy(manifest)
        folder_cache: dict[tuple[str, str], str] = {}
        summary = {"copied": 0, "skipped": 0, "failed": 0}
        for index, file_path in enumerate(files, start=1):
            relative = file_path.relative_to(local_root)
            relative_key = relative.as_posix()
            stat = file_path.stat()
            fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
            existing = manifest.get(relative_key)
            self._set_progress(str(job["id"]), index - 1, len(files), relative_key)
            if isinstance(existing, dict) and existing.get("fingerprint") == fingerprint:
                summary["skipped"] += 1
                self._set_progress(str(job["id"]), index, len(files), relative_key)
                continue
            parent_id = root_folder_id
            for part in relative.parts[:-1]:
                parent_id = self._remote_folder(access_token, drive_id, parent_id, safe_component(part), folder_cache)
            remote = self.api.create_upload(access_token, drive_id, parent_id, file_path, safe_component(file_path.name), include_full_hash=False)
            if str(remote.get("code") or "") == "PreHashMatched":
                remote = self.api.create_upload(access_token, drive_id, parent_id, file_path, safe_component(file_path.name), include_full_hash=True)
            if remote.get("rapid_upload"):
                manifest[relative_key] = {"fingerprint": fingerprint, "remoteFileId": str(remote.get("file_id") or "")}
                summary["copied"] += 1
                self._set_progress(str(job["id"]), index, len(files), relative_key)
                continue
            file_id = str(remote.get("file_id") or "")
            upload_id = str(remote.get("upload_id") or "")
            parts = remote.get("part_info_list") or []
            if not file_id or not upload_id or not isinstance(parts, list):
                raise ServiceError(502, f"云端未返回上传信息：{relative_key}")
            part_size = max(1, (stat.st_size + max(len(parts), 1) - 1) // max(len(parts), 1))
            for part_index, part in enumerate(parts):
                if not isinstance(part, dict):
                    raise ServiceError(502, "上传分片信息异常")
                upload_url = str(part.get("upload_url") or "")
                if not upload_url.startswith("https://"):
                    raise ServiceError(502, "云端未返回安全上传地址")
                start = part_index * part_size
                length = min(part_size, max(stat.st_size - start, 0))
                HttpClient.put_file(upload_url, file_path, start, length)
            self.api.complete_upload(access_token, drive_id, file_id, upload_id)
            manifest[relative_key] = {"fingerprint": fingerprint, "remoteFileId": file_id}
            summary["copied"] += 1
            self._set_progress(str(job["id"]), index, len(files), relative_key)
            self._save_manifest(str(job["id"]), manifest)
        self._save_manifest(str(job["id"]), manifest)
        return summary

    def _remote_files(self, access_token: str, drive_id: str, folder_id: str, prefix: PurePosixPath = PurePosixPath(".")) -> list[tuple[PurePosixPath, dict[str, Any]]]:
        result: list[tuple[PurePosixPath, dict[str, Any]]] = []
        for item in self.api.list_items(access_token, drive_id, folder_id):
            name = safe_component(item.get("name"))
            current = prefix / name
            if str(item.get("type") or "") == "folder":
                child_id = str(item.get("file_id") or item.get("id") or "")
                if child_id:
                    result.extend(self._remote_files(access_token, drive_id, child_id, current))
            else:
                result.append((current, item))
                if len(result) > MAX_REMOTE_ITEMS:
                    raise ServiceError(400, "云端文件过多，已停止本次同步")
        return result

    def _sync_download(self, job: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
        local_root = normalize_local_path(str(job["localPath"]))
        local_root.mkdir(parents=True, exist_ok=True)
        access_token, drive_id = self._access(config)
        remote_root = safe_remote_id(job["remoteFolderId"], field="阿里云盘文件夹")
        remote_files = self._remote_files(access_token, drive_id, remote_root)
        manifest = job.get("manifest") if isinstance(job.get("manifest"), dict) else {}
        manifest = copy.deepcopy(manifest)
        summary = {"copied": 0, "skipped": 0, "failed": 0}
        for index, (relative, item) in enumerate(remote_files, start=1):
            relative_key = relative.as_posix()
            file_id = str(item.get("file_id") or item.get("id") or "")
            if not file_id:
                continue
            size = int(item.get("size") or 0)
            stamp = f"{file_id}:{size}:{item.get('updated_at') or item.get('modified_at') or ''}"
            self._set_progress(str(job["id"]), index - 1, len(remote_files), relative_key)
            destination = safe_destination(local_root, relative)
            existing = manifest.get(relative_key)
            if isinstance(existing, dict) and existing.get("stamp") == stamp and destination.exists() and destination.stat().st_size == size:
                summary["skipped"] += 1
                self._set_progress(str(job["id"]), index, len(remote_files), relative_key)
                continue
            if destination.exists() and destination.is_file() and destination.stat().st_size == size:
                manifest[relative_key] = {"stamp": stamp, "fileId": file_id}
                summary["skipped"] += 1
                self._set_progress(str(job["id"]), index, len(remote_files), relative_key)
                continue
            if destination.exists():
                destination = safe_destination(local_root, relative.with_name(conflict_name(relative.name, "aliyun")))
            HttpClient.download(self.api.download_url(access_token, drive_id, file_id), destination)
            manifest[relative_key] = {"stamp": stamp, "fileId": file_id}
            summary["copied"] += 1
            self._set_progress(str(job["id"]), index, len(remote_files), relative_key)
            self._save_manifest(str(job["id"]), manifest)
        self._save_manifest(str(job["id"]), manifest)
        return summary

    def _scheduler_loop(self) -> None:
        while True:
            try:
                now = epoch_now()
                config = self.store.snapshot()
                for job in config.get("jobs", []):
                    if not isinstance(job, dict) or not job.get("enabled"):
                        continue
                    schedule = str(job.get("schedule") or "manual")
                    interval = {"hourly": 3600, "every6h": 21600, "daily": 86400}.get(schedule)
                    if not interval:
                        continue
                    last_scheduled = int(job.get("lastScheduledAt") or 0)
                    if now - last_scheduled < interval:
                        continue
                    job_id = str(job.get("id") or "")
                    if not job_id:
                        continue
                    try:
                        self.run_job(job_id, reason="scheduled")
                        def mark(config_data: dict[str, Any], target_id: str = job_id, timestamp: int = now) -> None:
                            self._job_from_config(config_data, target_id)["lastScheduledAt"] = timestamp
                        self.store.mutate(mark)
                    except ServiceError:
                        pass
            except Exception:
                pass
            time.sleep(30)


STORE = StateStore()
SERVICE = SyncService(STORE)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "XiaomiAliyunDriveSync/0.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 512 * 1024:
            raise ServiceError(400, "请求内容无效")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ServiceError(400, "请求内容不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise ServiceError(400, "请求内容格式无效")
        return payload

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        raw = self.path.split("?", 1)
        return raw[0].rstrip("/"), parse_qs(raw[1] if len(raw) > 1 else "")

    def do_GET(self) -> None:  # noqa: N802
        try:
            route, query = self._route()
            if route == "/api/status":
                self._send(200, SERVICE.status())
            elif route == "/api/local/folders":
                self._send(200, {"ok": True, **SERVICE.local_folders((query.get("path") or [None])[0])})
            elif route == "/api/remote/folders":
                self._send(200, {"ok": True, **SERVICE.remote_folders((query.get("parent_id") or ["root"])[0])})
            elif route == "/api/auth/status":
                self._send(200, {"ok": True, **SERVICE.poll_auth((query.get("sid") or [""])[0])})
            elif route == "/api/events":
                self._send(200, {"ok": True, "events": STORE.events((query.get("job_id") or [None])[0])})
            else:
                raise ServiceError(404, "接口不存在")
        except ServiceError as error:
            self._send(error.status, {"ok": False, "error": error.message})
        except Exception:
            self._send(500, {"ok": False, "error": "服务内部错误"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            route, _query = self._route()
            payload = self._read_json()
            if route == "/api/client":
                SERVICE.configure_client(payload.get("clientId"))
                self._send(200, {"ok": True})
            elif route == "/api/auth/start":
                self._send(200, {"ok": True, **SERVICE.start_auth()})
            elif route == "/api/auth/unbind":
                SERVICE.unbind()
                self._send(200, {"ok": True})
            elif route == "/api/jobs":
                self._send(201, {"ok": True, "job": SERVICE.create_job(payload)})
            elif re.fullmatch(r"/api/jobs/[A-Za-z0-9]+/run", route):
                job_id = route.split("/")[-2]
                SERVICE.run_job(job_id)
                self._send(202, {"ok": True})
            elif re.fullmatch(r"/api/jobs/[A-Za-z0-9]+/toggle", route):
                job_id = route.split("/")[-2]
                SERVICE.toggle_job(job_id, bool(payload.get("enabled")))
                self._send(200, {"ok": True})
            else:
                raise ServiceError(404, "接口不存在")
        except ServiceError as error:
            self._send(error.status, {"ok": False, "error": error.message})
        except Exception:
            self._send(500, {"ok": False, "error": "服务内部错误"})

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            route, _query = self._route()
            if not re.fullmatch(r"/api/jobs/[A-Za-z0-9]+", route):
                raise ServiceError(404, "接口不存在")
            SERVICE.remove_job(route.split("/")[-1])
            self._send(200, {"ok": True})
        except ServiceError as error:
            self._send(error.status, {"ok": False, "error": error.message})
        except Exception:
            self._send(500, {"ok": False, "error": "服务内部错误"})


def main() -> None:
    ensure_data_dir()
    server = ThreadingHTTPServer((HOST, PORT), ApiHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
