#!/usr/bin/env python3
"""Local-only 115 OpenAPI backup service for Xiaomi Smart Storage.

The browser UI is served by the Xiaomi NAS web shell. This process stays on
127.0.0.1 and is exposed to that UI through a same-origin nginx route.
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
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "18115"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/plugin/115-sync/data"))
LOCAL_ROOT = Path(os.environ.get("LOCAL_ROOT", "/nas/pool0"))
CONFIG_PATH = DATA_DIR / "config.json"
EVENTS_PATH = DATA_DIR / "events.json"
MAX_EVENT_COUNT = 800
MAX_REMOTE_ITEMS = 200_000
REQUEST_TIMEOUT = 40
USER_AGENT = "Xiaomi-NAS-115Sync/0.1"

AUTH_BASE = "https://passportapi.115.com"
QRCODE_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
API_BASE = "https://proapi.115.com"


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
    message = str(value or fallback).replace("\n", " ").strip()
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
                self._events = [event for event in loaded_events if isinstance(event, dict)][-MAX_EVENT_COUNT:]
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
        event = {
            "at": iso_now(),
            "job_id": job_id,
            "level": level,
            "message": safe_message(message, "Unknown event"),
        }
        with self._lock:
            self._events.append(event)
            del self._events[:-MAX_EVENT_COUNT]
            atomic_json_write(EVENTS_PATH, self._events)

    def events_for(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events = self._events if job_id is None else [item for item in self._events if item.get("job_id") == job_id]
            return copy.deepcopy(events[-100:])


def clean_client_id(value: Any) -> str:
    client_id = str(value or "").strip()
    if not client_id:
        raise ServiceError(400, "115 App ID is required")
    if len(client_id) > 160 or any(character.isspace() for character in client_id):
        raise ServiceError(400, "115 App ID format is invalid")
    return client_id


def normalize_remote_id(value: Any) -> str:
    remote_id = str(value if value is not None else "0").strip() or "0"
    if not remote_id.isdigit():
        raise ServiceError(400, "115 folder ID must be numeric")
    return remote_id


def normalize_local_path(value: Any, allow_missing: bool = False) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ServiceError(400, "A NAS folder is required")
    try:
        root = LOCAL_ROOT.resolve(strict=True)
    except OSError as error:
        raise ServiceError(500, f"NAS storage root is unavailable: {error}") from error

    candidate = Path(raw).expanduser().resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ServiceError(400, "Only folders inside the NAS storage pool can be used") from error

    if candidate.exists() and not candidate.is_dir():
        raise ServiceError(400, "The NAS path must be a folder")
    if not allow_missing and not candidate.exists():
        raise ServiceError(400, "The NAS folder does not exist")
    return candidate


def safe_component(value: Any) -> str:
    name = str(value or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ServiceError(502, "115 returned an unsafe file name")
    if any(ord(character) < 32 for character in name):
        raise ServiceError(502, "115 returned an invalid file name")
    return name


def safe_destination(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ServiceError(502, "Unsafe remote path")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ServiceError(409, "A NAS folder in this backup path is a symlink")
        current.mkdir(mode=0o755, exist_ok=True)
    destination = current / relative.parts[-1]
    try:
        destination.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise ServiceError(502, "Unsafe destination path") from error
    return destination


def sha1_for_file(file_path: Path) -> tuple[str, str]:
    digest = hashlib.sha1()
    prefix = hashlib.sha1()
    prefix_remaining = 128 * 1024
    with file_path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if prefix_remaining:
                part = chunk[:prefix_remaining]
                prefix.update(part)
                prefix_remaining -= len(part)
    return digest.hexdigest().upper(), prefix.hexdigest().upper()


def conflict_name(name: str, origin: str) -> str:
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stem}.{origin}-{timestamp}{suffix}"


class HttpClient:
    @staticmethod
    def request_json(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, Any] | None = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        data = None
        request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if headers:
            request_headers.update(headers)
        if form is not None:
            data = urlencode({key: str(value) for key, value in form.items() if value is not None}).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = Request(url, data=data, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            try:
                detail = json.loads(body)
                detail = detail.get("message") or detail.get("error") or body
            except json.JSONDecodeError:
                detail = body
            raise ServiceError(502, f"115 API request failed ({error.code}): {safe_message(detail, 'request failed')}") from error
        except URLError as error:
            raise ServiceError(502, f"115 API is unreachable: {safe_message(error.reason, 'network error')}") from error
        except TimeoutError as error:
            raise ServiceError(504, "115 API request timed out") from error

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ServiceError(502, "115 API returned an invalid response") from error
        if not isinstance(payload, dict):
            raise ServiceError(502, "115 API returned an unexpected response")
        return payload

    @staticmethod
    def download(url: str, destination: Path) -> None:
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response, temporary.open("wb") as target:
                shutil.copyfileobj(response, target, length=1024 * 1024)
            temporary.replace(destination)
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ServiceError(502, f"File download failed: {safe_message(error, 'network error')}") from error


class SyncService:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.http = HttpClient()
        self.auth_lock = threading.Lock()
        self.runtime_lock = threading.RLock()
        self.pending_auth: dict[str, dict[str, Any]] = {}
        self.running: dict[str, dict[str, Any]] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="115-sync")
        self.scheduler = threading.Thread(target=self._scheduler_loop, daemon=True, name="115-scheduler")
        self.scheduler.start()

    def status(self) -> dict[str, Any]:
        config = self.store.snapshot()
        with self.runtime_lock:
            active_runs = copy.deepcopy(self.running)
        jobs = []
        for job in config.get("jobs", []):
            if not isinstance(job, dict):
                continue
            item = copy.deepcopy(job)
            item["run"] = active_runs.get(item.get("id"))
            item["logs"] = self.store.events_for(item.get("id"))[-5:]
            jobs.append(item)
        return {
            "ok": True,
            "clientConfigured": bool(config.get("client_id")),
            "authorized": bool((config.get("token") or {}).get("refresh_token")),
            "account": config.get("account"),
            "localRoot": str(LOCAL_ROOT),
            "uploadDependencyReady": self._oss_ready(),
            "jobs": jobs,
            "updatedAt": iso_now(),
        }

    def configure_client(self, client_id: Any) -> dict[str, Any]:
        client_id = clean_client_id(client_id)

        def update(config: dict[str, Any]) -> None:
            if config.get("client_id") != client_id:
                config["token"] = None
                config["account"] = None
                for job in config.get("jobs", []):
                    if isinstance(job, dict):
                        job["enabled"] = False
            config["client_id"] = client_id

        self.store.mutate(update)
        self.store.append_event(None, "info", "115 OpenAPI application ID was updated")
        return self.status()

    def start_auth(self) -> dict[str, Any]:
        config = self.store.snapshot()
        client_id = clean_client_id(config.get("client_id"))
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        payload = self.http.request_json(
            "POST",
            f"{AUTH_BASE}/open/authDeviceCode",
            form={
                "client_id": client_id,
                "code_challenge": challenge,
                "code_challenge_method": "sha256",
            },
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data.get("uid") or not data.get("qrcode") or not data.get("time") or not data.get("sign"):
            raise ServiceError(502, safe_message(payload.get("message"), "115 did not return a QR authorization request"))

        session_id = secrets.token_urlsafe(20)
        with self.auth_lock:
            self.pending_auth[session_id] = {
                "uid": str(data["uid"]),
                "time": str(data["time"]),
                "sign": str(data["sign"]),
                "verifier": verifier,
                "created_at": epoch_now(),
            }
        return {"session": session_id, "qrcode": str(data["qrcode"]), "expiresIn": 600}

    def auth_status(self, session_id: str) -> dict[str, Any]:
        with self.auth_lock:
            pending = copy.deepcopy(self.pending_auth.get(session_id))
        if not pending:
            raise ServiceError(404, "Authorization session has expired")
        if epoch_now() - int(pending["created_at"]) > 600:
            with self.auth_lock:
                self.pending_auth.pop(session_id, None)
            raise ServiceError(410, "Authorization QR code expired")

        payload = self.http.request_json(
            "GET",
            QRCODE_STATUS_URL + "?" + urlencode({"uid": pending["uid"], "time": pending["time"], "sign": pending["sign"]}),
        )
        if payload.get("state") == 0:
            with self.auth_lock:
                self.pending_auth.pop(session_id, None)
            return {"status": "expired"}

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        qr_status = int(data.get("status") or 0)
        if qr_status != 2:
            return {"status": "scanned" if qr_status == 1 else "waiting", "message": data.get("msg") or ""}

        token_payload = self.http.request_json(
            "POST",
            f"{AUTH_BASE}/open/deviceCodeToToken",
            form={"uid": pending["uid"], "code_verifier": pending["verifier"]},
        )
        token = self._token_from_payload(token_payload)
        account = self._lookup_account(token["access_token"])

        def update(config: dict[str, Any]) -> None:
            config["token"] = token
            config["account"] = account

        self.store.mutate(update)
        with self.auth_lock:
            self.pending_auth.pop(session_id, None)
        self.store.append_event(None, "info", "115 account was connected with QR authorization")
        return {"status": "authorized", "account": account}

    def unbind(self) -> dict[str, Any]:
        def update(config: dict[str, Any]) -> None:
            config["token"] = None
            config["account"] = None
            for job in config.get("jobs", []):
                if isinstance(job, dict):
                    job["enabled"] = False

        self.store.mutate(update)
        self.store.append_event(None, "info", "Local 115 authorization data was removed")
        return self.status()

    def local_folders(self, parent: str | None) -> dict[str, Any]:
        directory = normalize_local_path(parent or str(LOCAL_ROOT), allow_missing=False)
        folders = []
        try:
            for entry in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
                if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                    continue
                folders.append({"name": entry.name, "path": str(entry)})
                if len(folders) >= 300:
                    break
        except OSError as error:
            raise ServiceError(500, f"Unable to read NAS folders: {error}") from error
        return {"path": str(directory), "parent": str(directory.parent) if directory != LOCAL_ROOT else None, "folders": folders}

    def remote_folders(self, cid: Any) -> dict[str, Any]:
        folder_id = normalize_remote_id(cid)
        children = self._remote_children(folder_id, folders_only=True)
        folders = [
            {"id": str(item.get("fid")), "name": safe_component(item.get("fn"))}
            for item in children
            if str(item.get("fc")) == "0" and item.get("fid") is not None
        ]
        return {"cid": folder_id, "folders": folders}

    def create_remote_folder(self, parent_id: Any, file_name: Any) -> dict[str, Any]:
        parent = normalize_remote_id(parent_id)
        name = safe_component(file_name)
        payload = self._api_form("/open/folder/add", {"pid": parent, "file_name": name})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        folder_id = data.get("file_id")
        if folder_id is None:
            raise ServiceError(502, safe_message(payload.get("message"), "115 did not create the folder"))
        return {"id": str(folder_id), "name": str(data.get("file_name") or name)}

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._access_token()
        direction = str(payload.get("direction") or "").strip()
        if direction not in {"upload", "download"}:
            raise ServiceError(400, "Choose upload backup or download backup")
        local_path = normalize_local_path(payload.get("localPath"), allow_missing=direction == "download")
        remote_cid = normalize_remote_id(payload.get("remoteCid"))
        name = str(payload.get("name") or "").strip()[:80]
        if not name:
            name = "上传备份" if direction == "upload" else "下载备份"
        schedule = str(payload.get("schedule") or "manual")
        if schedule not in {"manual", "hourly", "every6h", "daily"}:
            raise ServiceError(400, "Unsupported backup schedule")
        remote_label = str(payload.get("remoteLabel") or "115 文件夹").strip()[:120] or "115 文件夹"
        job = {
            "id": uuid.uuid4().hex,
            "name": name,
            "direction": direction,
            "localPath": str(local_path),
            "remoteCid": remote_cid,
            "remoteLabel": remote_label,
            "schedule": schedule,
            "enabled": schedule != "manual",
            "conflictPolicy": "keep-both",
            "createdAt": iso_now(),
            "lastRunAt": None,
            "lastResult": None,
            "lastSummary": None,
        }

        def update(config: dict[str, Any]) -> None:
            config.setdefault("jobs", []).append(job)

        self.store.mutate(update)
        self.store.append_event(job["id"], "info", "Backup task created")
        return job

    def delete_job(self, job_id: str) -> dict[str, Any]:
        if self._is_running(job_id):
            raise ServiceError(409, "Stop the active backup before removing this task")

        removed = False

        def update(config: dict[str, Any]) -> None:
            nonlocal removed
            existing = config.get("jobs", [])
            updated = [job for job in existing if str(job.get("id")) != job_id]
            removed = len(updated) != len(existing)
            config["jobs"] = updated

        self.store.mutate(update)
        if not removed:
            raise ServiceError(404, "Backup task not found")
        self.store.append_event(job_id, "info", "Backup task removed; no NAS or 115 files were deleted")
        return self.status()

    def set_job_enabled(self, job_id: str, enabled: Any) -> dict[str, Any]:
        updated = False

        def update(config: dict[str, Any]) -> None:
            nonlocal updated
            for job in config.get("jobs", []):
                if str(job.get("id")) == job_id:
                    job["enabled"] = bool(enabled)
                    updated = True
                    break

        self.store.mutate(update)
        if not updated:
            raise ServiceError(404, "Backup task not found")
        return self.status()

    def queue_run(self, job_id: str, reason: str = "manual") -> dict[str, Any]:
        job = self._job(job_id)
        self._access_token()
        with self.runtime_lock:
            if job_id in self.running:
                raise ServiceError(409, "This backup is already running")
            self.running[job_id] = {"phase": "queued", "reason": reason, "startedAt": iso_now(), "progress": None}
        self.executor.submit(self._run_job, copy.deepcopy(job), reason)
        return {"queued": True, "jobId": job_id}

    def logs(self, job_id: str) -> dict[str, Any]:
        self._job(job_id)
        return {"jobId": job_id, "events": self.store.events_for(job_id)}

    def _job(self, job_id: str) -> dict[str, Any]:
        for job in self.store.snapshot().get("jobs", []):
            if str(job.get("id")) == job_id:
                return job
        raise ServiceError(404, "Backup task not found")

    def _is_running(self, job_id: str) -> bool:
        with self.runtime_lock:
            return job_id in self.running

    def _set_run(self, job_id: str, **values: Any) -> None:
        with self.runtime_lock:
            current = self.running.get(job_id)
            if current is not None:
                current.update(values)

    def _run_job(self, job: dict[str, Any], reason: str) -> None:
        job_id = str(job["id"])
        summary: dict[str, int] = {"copied": 0, "skipped": 0, "conflicts": 0, "failed": 0}
        result = "success"
        self.store.append_event(job_id, "info", f"Backup started ({reason})")
        try:
            self._set_run(job_id, phase="preparing", progress={"done": 0, "total": 0})
            if job["direction"] == "upload":
                summary = self._run_upload(job)
            else:
                summary = self._run_download(job)
            self.store.append_event(job_id, "success", f"Backup completed: {summary['copied']} copied, {summary['skipped']} unchanged, {summary['conflicts']} kept as copies")
        except Exception as error:  # The task must survive a bad remote response.
            result = "error"
            summary["failed"] += 1
            self.store.append_event(job_id, "error", safe_message(error, "Backup failed"))
        finally:
            def update(config: dict[str, Any]) -> None:
                for current in config.get("jobs", []):
                    if str(current.get("id")) == job_id:
                        current["lastRunAt"] = iso_now()
                        current["lastResult"] = result
                        current["lastSummary"] = summary
                        break

            self.store.mutate(update)
            with self.runtime_lock:
                self.running.pop(job_id, None)

    def _run_upload(self, job: dict[str, Any]) -> dict[str, int]:
        root = normalize_local_path(job["localPath"], allow_missing=False)
        local_files = list(self._local_files(root))
        remote_files, remote_folders = self._remote_tree(str(job["remoteCid"]))
        summary = {"copied": 0, "skipped": 0, "conflicts": 0, "failed": 0}
        self._set_run(job["id"], phase="uploading", progress={"done": 0, "total": len(local_files)})
        credential: dict[str, Any] | None = None

        for index, (relative, source) in enumerate(local_files, start=1):
            self._set_run(job["id"], progress={"done": index - 1, "total": len(local_files), "file": str(relative)})
            full_sha1, prefix_sha1 = sha1_for_file(source)
            current_remote = remote_files.get(relative)
            if current_remote and str(current_remote.get("sha1") or "").lower() == full_sha1.lower():
                summary["skipped"] += 1
                continue

            parent_relative = relative.parent
            target_cid = self._ensure_remote_folder(parent_relative, remote_folders, str(job["remoteCid"]))
            target_name = relative.name
            if current_remote:
                target_name = conflict_name(target_name, "nas")
                summary["conflicts"] += 1
                self.store.append_event(job["id"], "warning", f"115 already has {relative}; uploaded a separate copy")

            try:
                credential = self._upload_one(source, target_cid, target_name, full_sha1, prefix_sha1, credential)
                summary["copied"] += 1
            except Exception as error:
                summary["failed"] += 1
                self.store.append_event(job["id"], "error", f"Upload failed for {relative}: {safe_message(error, 'unknown error')}")
            self._set_run(job["id"], progress={"done": index, "total": len(local_files), "file": str(relative)})
        return summary

    def _run_download(self, job: dict[str, Any]) -> dict[str, int]:
        root = normalize_local_path(job["localPath"], allow_missing=True)
        root.mkdir(mode=0o755, parents=True, exist_ok=True)
        remote_files, _ = self._remote_tree(str(job["remoteCid"]))
        ordered = sorted(remote_files.items(), key=lambda item: str(item[0]).casefold())
        summary = {"copied": 0, "skipped": 0, "conflicts": 0, "failed": 0}
        self._set_run(job["id"], phase="downloading", progress={"done": 0, "total": len(ordered)})

        for index, (relative, remote) in enumerate(ordered, start=1):
            self._set_run(job["id"], progress={"done": index - 1, "total": len(ordered), "file": str(relative)})
            try:
                destination = safe_destination(root, relative)
                target = destination
                remote_sha1 = str(remote.get("sha1") or "")
                if destination.exists():
                    if destination.is_file() and remote_sha1:
                        local_sha1, _ = sha1_for_file(destination)
                        if local_sha1.lower() == remote_sha1.lower():
                            summary["skipped"] += 1
                            continue
                    target = destination.with_name(conflict_name(destination.name, "115"))
                    summary["conflicts"] += 1
                    self.store.append_event(job["id"], "warning", f"NAS already has {relative}; downloaded a separate copy")
                url = self._download_url(remote)
                self.http.download(url, target)
                summary["copied"] += 1
            except Exception as error:
                summary["failed"] += 1
                self.store.append_event(job["id"], "error", f"Download failed for {relative}: {safe_message(error, 'unknown error')}")
            self._set_run(job["id"], progress={"done": index, "total": len(ordered), "file": str(relative)})
        return summary

    @staticmethod
    def _local_files(root: Path):
        for current, directories, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                directory
                for directory in directories
                if not directory.startswith(".") and not (current_path / directory).is_symlink()
            ]
            for file_name in sorted(file_names, key=str.casefold):
                if file_name.startswith("."):
                    continue
                source = current_path / file_name
                if source.is_symlink() or not source.is_file():
                    continue
                relative = PurePosixPath(source.relative_to(root).as_posix())
                yield relative, source

    def _remote_tree(self, root_cid: str) -> tuple[dict[PurePosixPath, dict[str, Any]], dict[PurePosixPath, str]]:
        files: dict[PurePosixPath, dict[str, Any]] = {}
        folders: dict[PurePosixPath, str] = {PurePosixPath("."): root_cid}
        queue: list[tuple[str, PurePosixPath]] = [(root_cid, PurePosixPath("."))]
        visited = {root_cid}

        while queue:
            cid, parent = queue.pop(0)
            for child in self._remote_children(cid):
                name = safe_component(child.get("fn"))
                relative = parent / name if str(parent) != "." else PurePosixPath(name)
                if str(child.get("fc")) == "0":
                    child_id = str(child.get("fid") or "")
                    if not child_id:
                        continue
                    folders[relative] = child_id
                    if child_id not in visited:
                        visited.add(child_id)
                        queue.append((child_id, relative))
                else:
                    files[relative] = {
                        "fid": str(child.get("fid") or ""),
                        "pick_code": str(child.get("pc") or ""),
                        "sha1": str(child.get("sha1") or ""),
                        "size": int(child.get("fs") or 0),
                    }
                if len(files) + len(folders) > MAX_REMOTE_ITEMS:
                    raise ServiceError(413, "This 115 folder is too large for one safe backup run")
        return files, folders

    def _remote_children(self, cid: str, folders_only: bool = False) -> list[dict[str, Any]]:
        offset = 0
        limit = 1150
        output: list[dict[str, Any]] = []
        while True:
            query: dict[str, Any] = {"cid": cid, "limit": limit, "offset": offset, "show_dir": 1}
            payload = self._api_get("/open/ufile/files", query)
            data = payload.get("data")
            batch = data if isinstance(data, list) else []
            output.extend(item for item in batch if isinstance(item, dict))
            if folders_only or len(batch) < limit:
                break
            offset += len(batch)
            if offset > MAX_REMOTE_ITEMS:
                raise ServiceError(413, "115 folder listing exceeded the safety limit")
        return output

    def _ensure_remote_folder(self, relative: PurePosixPath, folders: dict[PurePosixPath, str], root_cid: str) -> str:
        if str(relative) in {"", "."}:
            return root_cid
        if relative in folders:
            return folders[relative]
        parent = relative.parent
        parent_id = self._ensure_remote_folder(parent, folders, root_cid)
        name = safe_component(relative.name)
        payload = self._api_form("/open/folder/add", {"pid": parent_id, "file_name": name})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        created_id = data.get("file_id")
        if created_id is None:
            raise ServiceError(502, safe_message(payload.get("message"), f"Unable to create 115 folder {name}"))
        folders[relative] = str(created_id)
        return str(created_id)

    def _upload_one(
        self,
        source: Path,
        target_cid: str,
        target_name: str,
        full_sha1: str,
        prefix_sha1: str,
        credential: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        form = {
            "file_name": target_name,
            "file_size": source.stat().st_size,
            "target": f"U_1_{target_cid}",
            "fileid": full_sha1,
            "preid": prefix_sha1,
            "topupload": 0,
        }
        payload = self._api_form("/open/upload/init", form, allow_state_error=True)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if int(payload.get("code") or 0) in {700, 701, 702}:
            sign_key = data.get("sign_key")
            sign_check = str(data.get("sign_check") or "")
            if not sign_key or not re.fullmatch(r"\d+-\d+", sign_check):
                raise ServiceError(502, "115 requested an unsupported upload verification")
            start, end = (int(value) for value in sign_check.split("-", 1))
            if start < 0 or end < start:
                raise ServiceError(502, "115 returned an invalid upload verification range")
            with source.open("rb") as handle:
                handle.seek(start)
                verification = hashlib.sha1(handle.read(end - start + 1)).hexdigest().upper()
            form.update({"sign_key": sign_key, "sign_val": verification})
            payload = self._api_form("/open/upload/init", form)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        elif payload.get("state") is False:
            raise ServiceError(502, safe_message(payload.get("message"), "115 rejected the upload"))

        status = int(data.get("status") or 0)
        if status == 2:
            return credential
        if status != 1:
            raise ServiceError(502, "115 did not provide an upload transfer")

        active_credential = credential or self._upload_credential()
        self._oss_upload(source, data, active_credential)
        return active_credential

    def _upload_credential(self) -> dict[str, Any]:
        payload = self._api_get("/open/upload/get_token", {})
        data = payload.get("data")
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            raise ServiceError(502, safe_message(payload.get("message"), "115 did not return an upload credential"))
        return data

    def _oss_upload(self, source: Path, upload: dict[str, Any], credential: dict[str, Any]) -> None:
        try:
            import oss2  # type: ignore[import-not-found]
        except ImportError as error:
            raise ServiceError(424, "The oss2 upload dependency is not installed") from error

        endpoint = str(credential.get("endpoint") or "").strip()
        if not endpoint:
            raise ServiceError(502, "115 upload credential did not contain an OSS endpoint")
        if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
            endpoint = "https://" + endpoint
        access_key_secret = credential.get("AccessKeySecret") or credential.get("AccessKeySecrett")
        access_key_id = credential.get("AccessKeyId")
        security_token = credential.get("SecurityToken")
        bucket_name = upload.get("bucket")
        object_name = upload.get("object")
        if not all([access_key_secret, access_key_id, security_token, bucket_name, object_name]):
            raise ServiceError(502, "115 upload transfer did not contain complete OSS data")

        auth = oss2.StsAuth(str(access_key_id), str(access_key_secret), str(security_token))
        bucket = oss2.Bucket(auth, endpoint, str(bucket_name), connect_timeout=REQUEST_TIMEOUT)
        headers: dict[str, str] = {}
        if upload.get("callback"):
            headers["x-oss-callback"] = str(upload["callback"])
        if upload.get("callback_var"):
            headers["x-oss-callback-var"] = str(upload["callback_var"])
        result = bucket.put_object_from_file(str(object_name), str(source), headers=headers or None)
        if int(getattr(result, "status", 0)) // 100 != 2:
            raise ServiceError(502, "Object storage upload was not accepted")

    def _download_url(self, remote: dict[str, Any]) -> str:
        pick_code = str(remote.get("pick_code") or "")
        if not pick_code:
            raise ServiceError(502, "115 did not return a download key for this file")
        payload = self._api_form("/open/ufile/downurl", {"pick_code": pick_code})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for item in data.values():
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                return url
        raise ServiceError(502, safe_message(payload.get("message"), "115 did not return a file download URL"))

    def _access_token(self) -> str:
        with self.auth_lock:
            config = self.store.snapshot()
            token = config.get("token") if isinstance(config.get("token"), dict) else None
            if not token or not token.get("access_token") or not token.get("refresh_token"):
                raise ServiceError(401, "Connect a 115 account first")
            if int(token.get("expires_at") or 0) > epoch_now() + 180:
                return str(token["access_token"])

            refreshed = self.http.request_json(
                "POST",
                f"{AUTH_BASE}/open/refreshToken",
                form={"refresh_token": token["refresh_token"]},
            )
            updated_token = self._token_from_payload(refreshed, previous_refresh=str(token["refresh_token"]))

            def update(config_data: dict[str, Any]) -> None:
                config_data["token"] = updated_token

            self.store.mutate(update)
            return str(updated_token["access_token"])

    @staticmethod
    def _token_from_payload(payload: dict[str, Any], previous_refresh: str | None = None) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        access_token = str(data.get("access_token") or "")
        refresh_token = str(data.get("refresh_token") or previous_refresh or "")
        expires_in = int(data.get("expires_in") or 0)
        if not access_token or not refresh_token or expires_in <= 0:
            raise ServiceError(502, safe_message(payload.get("message"), "115 authorization did not return usable tokens"))
        return {"access_token": access_token, "refresh_token": refresh_token, "expires_at": epoch_now() + expires_in}

    def _lookup_account(self, access_token: str) -> dict[str, Any] | None:
        try:
            payload = self.http.request_json("GET", f"{API_BASE}/open/user/info", headers={"Authorization": f"Bearer {access_token}"})
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            if not data:
                return None
            label = data.get("user_name") or data.get("nick_name") or data.get("user_id") or data.get("uid")
            return {"label": str(label)} if label else {"label": "115 account"}
        except ServiceError:
            return {"label": "115 account"}

    def _api_get(self, path: str, query: dict[str, Any]) -> dict[str, Any]:
        suffix = "?" + urlencode({key: str(value) for key, value in query.items() if value is not None}) if query else ""
        return self._api_request("GET", path + suffix)

    def _api_form(self, path: str, form: dict[str, Any], allow_state_error: bool = False) -> dict[str, Any]:
        return self._api_request("POST", path, form=form, allow_state_error=allow_state_error)

    def _api_request(self, method: str, path: str, form: dict[str, Any] | None = None, allow_state_error: bool = False) -> dict[str, Any]:
        token = self._access_token()
        payload = self.http.request_json(
            method,
            API_BASE + path,
            headers={"Authorization": f"Bearer {token}"},
            form=form,
        )
        if payload.get("state") is False and not allow_state_error:
            raise ServiceError(502, safe_message(payload.get("message"), "115 rejected the request"))
        return payload

    @staticmethod
    def _oss_ready() -> bool:
        try:
            import oss2  # type: ignore[import-not-found,unused-ignore]

            return bool(oss2)
        except ImportError:
            return False

    def _scheduler_loop(self) -> None:
        while True:
            try:
                now = epoch_now()
                for job in self.store.snapshot().get("jobs", []):
                    if not isinstance(job, dict) or not job.get("enabled") or self._is_running(str(job.get("id"))):
                        continue
                    interval = {"hourly": 3600, "every6h": 21600, "daily": 86400}.get(str(job.get("schedule")), 0)
                    if not interval:
                        continue
                    # A newly scheduled task waits for its selected interval.
                    # The first data transfer always remains an explicit user action.
                    last_run = job.get("lastRunAt") or job.get("createdAt")
                    last_epoch = 0
                    if isinstance(last_run, str):
                        try:
                            last_epoch = int(datetime.fromisoformat(last_run).timestamp())
                        except ValueError:
                            last_epoch = 0
                    if now - last_epoch >= interval:
                        try:
                            self.queue_run(str(job["id"]), reason="scheduled")
                        except ServiceError:
                            pass
            except Exception:
                pass
            time.sleep(15)


STORE = StateStore()
SERVICE = SyncService(STORE)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "Xiaomi115Sync/0.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise ServiceError(413, "Request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ServiceError(400, "Request body must be JSON") from error
        if not isinstance(parsed, dict):
            raise ServiceError(400, "Request body must be an object")
        return parsed

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        path = parsed.path
        marker = "/api/"
        index = path.find(marker)
        if index < 0:
            raise ServiceError(404, "Not found")
        return path[index + len(marker):].strip("/"), parse_qs(parsed.query)

    def do_GET(self) -> None:  # noqa: N802
        try:
            route, query = self._route()
            if route == "status":
                payload = SERVICE.status()
            elif route == "local/folders":
                payload = SERVICE.local_folders((query.get("parent") or [None])[0])
            elif route == "remote/folders":
                payload = SERVICE.remote_folders((query.get("cid") or ["0"])[0])
            elif route == "auth/status":
                payload = SERVICE.auth_status((query.get("session") or [""])[0])
            else:
                match = re.fullmatch(r"jobs/([a-f0-9]{32})/logs", route)
                if not match:
                    raise ServiceError(404, "Not found")
                payload = SERVICE.logs(match.group(1))
            self._json(HTTPStatus.OK, payload)
        except ServiceError as error:
            self._json(error.status, {"ok": False, "error": error.message})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Internal server error"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            route, _query = self._route()
            body = self._body()
            if route == "config/client":
                payload = SERVICE.configure_client(body.get("clientId"))
            elif route == "auth/start":
                payload = SERVICE.start_auth()
            elif route == "auth/unbind":
                payload = SERVICE.unbind()
            elif route == "jobs":
                payload = SERVICE.create_job(body)
            elif route == "remote/folders":
                payload = SERVICE.create_remote_folder(body.get("parentId"), body.get("name"))
            else:
                match = re.fullmatch(r"jobs/([a-f0-9]{32})/(run|enabled|delete)", route)
                if not match:
                    raise ServiceError(404, "Not found")
                job_id, action = match.groups()
                if action == "run":
                    payload = SERVICE.queue_run(job_id)
                elif action == "enabled":
                    payload = SERVICE.set_job_enabled(job_id, body.get("enabled"))
                else:
                    payload = SERVICE.delete_job(job_id)
            self._json(HTTPStatus.OK, payload)
        except ServiceError as error:
            self._json(error.status, {"ok": False, "error": error.message})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Internal server error"})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), ApiHandler)
    print(f"115 Sync API listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
