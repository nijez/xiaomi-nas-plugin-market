import http from "node:http";
import { spawn } from "node:child_process";

const NAS_IP = process.env.NAS_IP || "127.0.0.1";
const PROBE_MODE = process.env.PROBE_MODE || (NAS_IP === "127.0.0.1" ? "local" : "ssh");
const KEY_PATH = process.env.KEY_PATH || "";
const PORT = Number(process.env.PORT || 5188);

const remoteScript = String.raw`
set -u
printf 'HOSTNAME=%s\n' "$(hostname 2>/dev/null || true)"
printf 'NPROC=%s\n' "$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 4)"
printf 'LOADAVG=%s\n' "$(cat /proc/loadavg 2>/dev/null || true)"
awk '/MemTotal:/ {print "MEM_TOTAL_KB="$2} /MemAvailable:/ {print "MEM_AVAILABLE_KB="$2}' /proc/meminfo 2>/dev/null || true
df -Pk /nas/pool0 2>/dev/null | awk 'NR==2 {print "DF_TOTAL_KB="$2; print "DF_USED_KB="$3; print "DF_AVAIL_KB="$4; print "DF_PERCENT="$5; print "DF_MOUNT="$6}'
max_temp=""
for f in /sys/class/thermal/thermal_zone*/temp; do
  [ -r "$f" ] || continue
  v=$(cat "$f" 2>/dev/null || true)
  case "$v" in ''|*[!0-9]*) continue ;; esac
  if [ -z "$max_temp" ] || [ "$v" -gt "$max_temp" ]; then
    max_temp="$v"
  fi
done
[ -n "$max_temp" ] && printf 'TEMP_RAW=%s\n' "$max_temp"
printf 'DOCKER_ACTIVE=%s\n' "$(systemctl is-active docker.service 2>/dev/null || true)"
printf 'DOCKER_VERSION=%s\n' "$(/data/docker/docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
/data/docker/docker ps --format 'CONTAINER={{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null || true
printf 'DROPBEAR_ACTIVE=%s\n' "$(systemctl is-active dropbear.socket 2>/dev/null || systemctl is-active dropbear.service 2>/dev/null || true)"
printf 'DROPBEAR_ENABLED=%s\n' "$(systemctl is-enabled dropbear.socket 2>/dev/null || systemctl is-enabled dropbear.service 2>/dev/null || true)"
`;

function runProcess(command, args, input) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["pipe", "pipe", "pipe"] });

    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error("NAS status probe timed out"));
    }, 12000);

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) {
        resolve(stdout);
        return;
      }
      reject(new Error(stderr.trim() || `${command} exited with code ${code}`));
    });

    child.stdin.end(input);
  });
}

function runSsh() {
  if (!KEY_PATH) {
    return Promise.reject(new Error("Set KEY_PATH to the root SSH private-key path."));
  }
  return runProcess(
    "ssh",
    [
      "-T",
      "-i",
      KEY_PATH,
      "-o",
      "BatchMode=yes",
      "-o",
      "StrictHostKeyChecking=accept-new",
      "-o",
      "ConnectTimeout=8",
      `root@${NAS_IP}`,
      "sh -s",
    ],
    remoteScript,
  );
}

function runLocal() {
  return runProcess("sh", ["-s"], remoteScript);
}

function runProbe() {
  return PROBE_MODE === "local" ? runLocal() : runSsh();
}

function parseKeyValues(output) {
  const values = new Map();
  const containers = [];

  for (const line of output.split(/\r?\n/)) {
    if (!line.trim()) continue;
    if (line.startsWith("CONTAINER=")) {
      const [name = "", image = "", status = ""] = line.slice("CONTAINER=".length).split("|");
      containers.push({ name, image, status });
      continue;
    }
    const index = line.indexOf("=");
    if (index > 0) {
      values.set(line.slice(0, index), line.slice(index + 1));
    }
  }

  return { values, containers };
}

function toNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function bytesLabelFromKb(kb) {
  if (!Number.isFinite(kb) || kb <= 0) return "0G";
  const gb = kb / 1024 / 1024;
  if (gb >= 1024) {
    const tb = gb / 1024;
    return `${tb.toFixed(tb >= 10 ? 0 : 1)}T`;
  }
  return `${Math.round(gb)}G`;
}

function buildStatus(output) {
  const { values, containers } = parseKeyValues(output);
  const loadavg = values.get("LOADAVG") || "0 0 0";
  const [load1 = "0", load5 = "0", load15 = "0"] = loadavg.split(/\s+/);
  const cores = Math.max(1, toNumber(values.get("NPROC"), 4));
  const load1Number = toNumber(load1);
  const cpuPercent = Math.max(1, Math.min(100, Math.round((load1Number / cores) * 100)));

  const memTotalKb = toNumber(values.get("MEM_TOTAL_KB"));
  const memAvailableKb = toNumber(values.get("MEM_AVAILABLE_KB"));
  const memUsedKb = Math.max(0, memTotalKb - memAvailableKb);
  const memPercent = memTotalKb > 0 ? Math.round((memUsedKb / memTotalKb) * 100) : 0;

  const storageTotalKb = toNumber(values.get("DF_TOTAL_KB"));
  const storageUsedKb = toNumber(values.get("DF_USED_KB"));
  const storagePercent = Number.parseInt(values.get("DF_PERCENT") || "", 10);

  const tempRaw = toNumber(values.get("TEMP_RAW"), NaN);
  const celsius = Number.isFinite(tempRaw)
    ? Number((tempRaw > 1000 ? tempRaw / 1000 : tempRaw).toFixed(1))
    : null;

  const now = new Date();
  const updatedAt = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  }).format(now);

  return {
    ok: true,
    source: PROBE_MODE === "local" ? "127.0.0.1" : NAS_IP,
    probeMode: PROBE_MODE,
    hostname: values.get("HOSTNAME") || "",
    updatedAt,
    metrics: {
      cpu: {
        percent: cpuPercent,
        load: `${Number(load1).toFixed(2)} / ${Number(load5).toFixed(2)} / ${Number(load15).toFixed(2)}`,
        cores,
      },
      memory: {
        percent: memPercent,
        usedMb: Math.round(memUsedKb / 1024),
        totalMb: Math.round(memTotalKb / 1024),
      },
      storage: {
        percent: Number.isFinite(storagePercent) ? storagePercent : 0,
        used: bytesLabelFromKb(storageUsedKb),
        total: bytesLabelFromKb(storageTotalKb),
        mount: values.get("DF_MOUNT") || "/nas/pool0",
      },
      temperature: {
        celsius,
      },
    },
    services: {
      docker: {
        active: values.get("DOCKER_ACTIVE") === "active",
        version: values.get("DOCKER_VERSION") || "",
        running: containers.length,
      },
      containers,
      dropbear: {
        active: values.get("DROPBEAR_ACTIVE") === "active",
        enabled: values.get("DROPBEAR_ENABLED") === "enabled",
      },
    },
  };
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  if (req.method === "OPTIONS") {
    sendJson(res, 204, {});
    return;
  }

  if (req.method !== "GET" || req.url !== "/api/nas-status") {
    sendJson(res, 404, { ok: false, error: "not found" });
    return;
  }

  try {
    const output = await runProbe();
    sendJson(res, 200, buildStatus(output));
  } catch (error) {
    sendJson(res, 502, {
      ok: false,
      source: PROBE_MODE === "local" ? "127.0.0.1" : NAS_IP,
      probeMode: PROBE_MODE,
      error: error instanceof Error ? error.message : "unknown error",
    });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`NAS status API listening on http://127.0.0.1:${PORT}/api/nas-status`);
  if (PROBE_MODE === "local") {
    console.log("Reading status from this machine without SSH");
  } else {
    console.log(`Reading ${NAS_IP} over SSH with key ${KEY_PATH}`);
  }
});
