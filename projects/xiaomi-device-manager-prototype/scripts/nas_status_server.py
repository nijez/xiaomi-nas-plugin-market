#!/usr/bin/env python3
import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

PORT = int(os.environ.get("PORT", "18080"))
HOST = os.environ.get("HOST", "0.0.0.0")
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).resolve().parent / "public"))
POOL_MOUNT = os.environ.get("POOL_MOUNT", "/nas/pool0")
DOCKER_BIN = os.environ.get("DOCKER_BIN", "/data/docker/docker")
SMARTCTL_BIN = os.environ.get("SMARTCTL_BIN", "/usr/sbin/smartctl")
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "/data/plugin/xiaomi-device-manager/history.json"))
SAMPLE_INTERVAL = max(30, int(os.environ.get("SAMPLE_INTERVAL", "60")))
MAX_HISTORY_POINTS = max(60, int(os.environ.get("MAX_HISTORY_POINTS", "1440")))
CPU_SAMPLE_WINDOW = max(0.05, min(1.0, float(os.environ.get("CPU_SAMPLE_WINDOW", "1.0"))))
DRIVE_HEALTH_TTL = max(60, int(os.environ.get("DRIVE_HEALTH_TTL", "300")))
DOCKER_STATS_TTL = max(5, int(os.environ.get("DOCKER_STATS_TTL", "10")))
history_lock = threading.Lock()
history = []
drive_cache_lock = threading.Lock()
drive_cache = []
drive_cache_at = 0.0
docker_stats_cache_lock = threading.Lock()
docker_stats_cache = {}
docker_stats_cache_at = 0.0


def run(command, timeout=3):
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout.strip()
    except Exception:
        return ""


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def bytes_label(kb):
    try:
        gb = float(kb) / 1024 / 1024
    except Exception:
        return "0G"
    if gb >= 1024:
        tb = gb / 1024
        return f"{tb:.0f}T" if tb >= 10 else f"{tb:.1f}T"
    return f"{round(gb)}G"


def parse_cpu_times(proc_stat):
    first_line = proc_stat.splitlines()[0].split() if proc_stat else []
    if not first_line or first_line[0] != "cpu" or len(first_line) < 5:
        return None
    try:
        values = [int(value) for value in first_line[1:]]
    except ValueError:
        return None
    values.extend([0] * (8 - len(values)))
    user, nice, system, idle, iowait, irq, softirq, steal = values[:8]
    idle_total = idle + iowait
    busy_total = user + nice + system + irq + softirq + steal
    return busy_total + idle_total, idle_total


def cpu_percent_between(first, second):
    if first is None or second is None:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return None
    return max(0, min(100, round(((total_delta - idle_delta) / total_delta) * 100)))


def measure_cpu_percent(load1, cores):
    first = parse_cpu_times(read_text("/proc/stat"))
    if first is not None:
        time.sleep(CPU_SAMPLE_WINDOW)
        measured = cpu_percent_between(first, parse_cpu_times(read_text("/proc/stat")))
        if measured is not None:
            return measured, "proc_stat"
    fallback = max(0, min(100, round((float(load1) / cores) * 100)))
    return fallback, "load_average_fallback"


def active_network_interfaces():
    interfaces = []
    for interface in Path("/sys/class/net").glob("*"):
        name = interface.name
        if name == "lo" or "/virtual/" in str(interface.resolve()):
            continue
        if read_text(interface / "operstate") == "up":
            interfaces.append(name)
    return sorted(interfaces)


def parse_network_counters(proc_net_dev, interfaces=None):
    allowed = set(interfaces) if interfaces else None
    counters = {"rx": 0, "tx": 0}
    for line in proc_net_dev.splitlines():
        if ":" not in line:
            continue
        name, payload = line.split(":", 1)
        name = name.strip()
        if name == "lo" or (allowed is not None and name not in allowed):
            continue
        fields = payload.split()
        if len(fields) < 9:
            continue
        try:
            counters["rx"] += int(fields[0])
            counters["tx"] += int(fields[8])
        except ValueError:
            continue
    return counters


def physical_disk_names():
    names = []
    for block in Path("/sys/block").glob("*"):
        if re.fullmatch(r"sd[a-z]+|nvme\d+n\d+", block.name):
            names.append(block.name)
    return sorted(names)


def parse_disk_counters(proc_diskstats, devices=None):
    allowed = set(devices) if devices else None
    counters = {"read": 0, "write": 0}
    for line in proc_diskstats.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        name = fields[2]
        if not re.fullmatch(r"sd[a-z]+|nvme\d+n\d+", name):
            continue
        if allowed is not None and name not in allowed:
            continue
        try:
            counters["read"] += int(fields[5]) * 512
            counters["write"] += int(fields[9]) * 512
        except ValueError:
            continue
    return counters


def counter_rates(first, second, elapsed, first_key, second_key):
    if elapsed <= 0:
        return {first_key: 0, second_key: 0}
    return {
        first_key: max(0, round((second[first_key] - first[first_key]) / elapsed)),
        second_key: max(0, round((second[second_key] - first[second_key]) / elapsed)),
    }


def measure_system_activity(load1, cores):
    interfaces = active_network_interfaces()
    devices = physical_disk_names()
    first_cpu = parse_cpu_times(read_text("/proc/stat"))
    first_network = parse_network_counters(read_text("/proc/net/dev"), interfaces)
    first_disk = parse_disk_counters(read_text("/proc/diskstats"), devices)
    started = time.monotonic()
    time.sleep(CPU_SAMPLE_WINDOW)
    elapsed = max(CPU_SAMPLE_WINDOW, time.monotonic() - started)
    second_cpu = parse_cpu_times(read_text("/proc/stat"))
    second_network = parse_network_counters(read_text("/proc/net/dev"), interfaces)
    second_disk = parse_disk_counters(read_text("/proc/diskstats"), devices)

    cpu_percent = cpu_percent_between(first_cpu, second_cpu)
    cpu_source = "proc_stat"
    if cpu_percent is None:
        cpu_percent = max(0, min(100, round((float(load1) / cores) * 100)))
        cpu_source = "load_average_fallback"

    network = counter_rates(first_network, second_network, elapsed, "rx", "tx")
    disk = counter_rates(first_disk, second_disk, elapsed, "read", "write")
    return {
        "cpuPercent": cpu_percent,
        "cpuSource": cpu_source,
        "network": {
            "interface": ", ".join(interfaces) if interfaces else "可用网卡",
            "rxBps": network["rx"],
            "txBps": network["tx"],
        },
        "diskIo": {
            "devices": devices,
            "readBps": disk["read"],
            "writeBps": disk["write"],
        },
    }


def smart_attribute(payload, attribute_id):
    table = payload.get("ata_smart_attributes", {}).get("table", [])
    for attribute in table:
        if attribute.get("id") == attribute_id:
            return attribute.get("raw", {}).get("value", 0)
    return 0


def load_drives():
    global drive_cache, drive_cache_at
    now = time.monotonic()
    with drive_cache_lock:
        if drive_cache and now - drive_cache_at < DRIVE_HEALTH_TTL:
            return [dict(drive) for drive in drive_cache]
    raw = run(["lsblk", "-b", "-J", "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,ROTA"], timeout=5)
    try:
        block_devices = json.loads(raw).get("blockdevices", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    drives = []
    for block in block_devices:
        name = str(block.get("name", ""))
        if block.get("type") != "disk" or not re.fullmatch(r"sd[a-z]+|nvme\d+n\d+", name):
            continue
        smart_raw = run(
            [SMARTCTL_BIN, "-n", "standby,0", "-H", "-A", "-i", "-j", f"/dev/{name}"],
            timeout=8,
        )
        try:
            smart = json.loads(smart_raw)
        except json.JSONDecodeError:
            smart = {}
        capacity = smart.get("user_capacity", {}).get("bytes") or block.get("size") or 0
        drives.append(
            {
                "device": f"/dev/{name}",
                "model": smart.get("model_name") or str(block.get("model") or "").strip() or "未知型号",
                "serial": smart.get("serial_number") or str(block.get("serial") or "").strip(),
                "capacityBytes": int(capacity),
                "temperature": smart.get("temperature", {}).get("current"),
                "smartPassed": smart.get("smart_status", {}).get("passed"),
                "powerOnHours": smart.get("power_on_time", {}).get("hours"),
                "reallocatedSectors": smart_attribute(smart, 5),
                "pendingSectors": smart_attribute(smart, 197),
                "offlineUncorrectable": smart_attribute(smart, 198),
                "crcErrors": smart_attribute(smart, 199),
                "rotationRate": smart.get("rotation_rate"),
            }
        )
    with drive_cache_lock:
        drive_cache = [dict(drive) for drive in drives]
        drive_cache_at = now
    return drives


def load_history():
    try:
        payload = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)][-MAX_HISTORY_POINTS:]
    except Exception:
        pass
    return []


def save_history():
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = HISTORY_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        temporary.replace(HISTORY_FILE)
    except Exception:
        pass


def record_sample(status):
    sample = {
        "at": int(time.time() * 1000),
        "cpu": status["metrics"]["cpu"]["percent"],
        "memory": status["metrics"]["memory"]["percent"],
        "storage": status["metrics"]["storage"]["percent"],
        "temperature": status["metrics"]["temperature"]["celsius"],
        "network": status["metrics"]["network"]["rxBps"] + status["metrics"]["network"]["txBps"],
        "diskIo": status["metrics"]["diskIo"]["readBps"] + status["metrics"]["diskIo"]["writeBps"],
    }
    with history_lock:
        if history and sample["at"] - int(history[-1].get("at", 0)) < SAMPLE_INTERVAL * 900:
            history[-1] = sample
        else:
            history.append(sample)
        del history[:-MAX_HISTORY_POINTS]
        save_history()


def sampling_loop():
    while True:
        try:
            record_sample(load_live_metrics())
        except Exception:
            pass
        time.sleep(SAMPLE_INTERVAL)


def load_live_metrics():
    load_parts = (read_text("/proc/loadavg") or "0 0 0").split()
    load1, load5, load15 = (load_parts + ["0", "0", "0"])[:3]
    cores = os.cpu_count() or 4
    activity = measure_system_activity(load1, cores)

    mem_total = 0
    mem_available = 0
    for line in read_text("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "MemTotal:":
            mem_total = int(parts[1])
        elif parts[0] == "MemAvailable:":
            mem_available = int(parts[1])
    mem_used = max(0, mem_total - mem_available)
    mem_percent = round((mem_used / mem_total) * 100) if mem_total else 0

    df = run(["df", "-Pk", POOL_MOUNT]).splitlines()
    storage = {"percent": 0, "used": "0G", "total": "0G", "mount": POOL_MOUNT}
    if len(df) >= 2:
        parts = df[1].split()
        if len(parts) >= 6:
            storage = {
                "percent": int(parts[4].rstrip("%") or 0),
                "used": bytes_label(parts[2]),
                "total": bytes_label(parts[1]),
                "mount": parts[5],
            }

    temperatures = []
    for temp_file in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        raw = read_text(temp_file)
        if raw.isdigit():
            value = int(raw)
            temperatures.append(value / 1000 if value > 1000 else value)
    celsius = round(max(temperatures), 1) if temperatures else None

    updated_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
    return {
        "ok": True,
        "source": "127.0.0.1",
        "probeMode": "local",
        "sampledAt": int(time.time() * 1000),
        "updatedAt": updated_at,
        "metrics": {
            "cpu": {
                "percent": activity["cpuPercent"],
                "source": activity["cpuSource"],
                "load": f"{float(load1):.2f} / {float(load5):.2f} / {float(load15):.2f}",
                "cores": cores,
            },
            "memory": {
                "percent": mem_percent,
                "usedMb": round(mem_used / 1024),
                "totalMb": round(mem_total / 1024),
            },
            "storage": storage,
            "temperature": {"celsius": celsius},
            "network": activity["network"],
            "diskIo": activity["diskIo"],
        },
    }


def load_docker_stats():
    global docker_stats_cache, docker_stats_cache_at
    now = time.monotonic()
    with docker_stats_cache_lock:
        if docker_stats_cache and now - docker_stats_cache_at < DOCKER_STATS_TTL:
            return {name: dict(stats) for name, stats in docker_stats_cache.items()}

    stats_by_name = {}
    stats_output = run([DOCKER_BIN, "stats", "--no-stream", "--format", "{{json .}}"], timeout=8)
    for line in stats_output.splitlines():
        try:
            stats = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = stats.get("Name", "")
        if name:
            stats_by_name[name] = stats
    with docker_stats_cache_lock:
        docker_stats_cache = {name: dict(stats) for name, stats in stats_by_name.items()}
        docker_stats_cache_at = now
    return stats_by_name


def load_status():
    live = load_live_metrics()
    hostname = socket.gethostname()
    docker_active = run(["systemctl", "is-active", "docker.service"]) == "active"
    docker_version = run([DOCKER_BIN, "version", "--format", "{{.Server.Version}}"])
    containers = []
    ps_output = run([DOCKER_BIN, "ps", "--format", "{{json .}}"], timeout=5)
    for line in ps_output.splitlines():
        try:
            container = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = container.get("Names", "")
        if name:
            containers.append(
                {
                    "name": name,
                    "image": container.get("Image", ""),
                    "status": container.get("Status", ""),
                    "id": container.get("ID", ""),
                    "state": container.get("State", ""),
                    "createdAt": container.get("CreatedAt", ""),
                    "runningFor": container.get("RunningFor", ""),
                    "ports": container.get("Ports", ""),
                    "networks": container.get("Networks", ""),
                    "mounts": container.get("Mounts", ""),
                }
            )

    stats_by_name = load_docker_stats()
    for container in containers:
        stats = stats_by_name.get(container["name"], {})
        container.update(
            {
                "cpuPercent": stats.get("CPUPerc", ""),
                "memoryPercent": stats.get("MemPerc", ""),
                "memoryUsage": stats.get("MemUsage", ""),
                "networkIo": stats.get("NetIO", ""),
                "blockIo": stats.get("BlockIO", ""),
                "pids": stats.get("PIDs", ""),
            }
        )

    return {
        "ok": True,
        "source": "127.0.0.1",
        "probeMode": "local",
        "hostname": hostname,
        "updatedAt": live["updatedAt"],
        "metrics": live["metrics"],
        "drives": load_drives(),
        "services": {
            "docker": {
                "active": docker_active,
                "version": docker_version,
                "running": len(containers),
            },
            "containers": containers,
        },
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        cache = "no-store" if self.path.startswith("/api/") else "public, max-age=60"
        self.send_header("Cache-Control", cache)
        super().end_headers()

    def send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        request_path = urlparse(self.path).path
        if request_path == "/healthz" or request_path.endswith("/healthz"):
            self.send_json(200, {"ok": True})
            return
        if request_path == "/api/live-metrics" or request_path.endswith("/api/live-metrics"):
            try:
                self.send_json(200, load_live_metrics())
            except Exception as exc:
                payload = {"ok": False, "source": "127.0.0.1", "probeMode": "local", "error": str(exc)}
                self.send_json(500, payload)
            return
        if request_path == "/api/nas-status" or request_path.endswith("/api/nas-status"):
            try:
                status = load_status()
                with history_lock:
                    status["history"] = list(history)
                self.send_json(200, status)
            except Exception as exc:
                payload = {"ok": False, "source": "127.0.0.1", "probeMode": "local", "error": str(exc)}
                self.send_json(500, payload)
            return

        candidate = STATIC_DIR / self.path.lstrip("/")
        if self.path == "/" or candidate.exists():
            super().do_GET()
            return

        self.path = "/index.html"
        super().do_GET()


if __name__ == "__main__":
    os.chdir(STATIC_DIR)
    with history_lock:
        history.extend(load_history())
    threading.Thread(target=sampling_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Xiaomi Device Manager listening on http://{HOST}:{PORT}")
    print(f"Static files: {STATIC_DIR}")
    server.serve_forever()
