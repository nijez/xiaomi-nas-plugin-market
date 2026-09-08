#!/usr/bin/env bash
# Install or update only the 115 Sync plugin. Run this script explicitly from
# this source directory after reviewing the target NAS IP and user ID.

set -euo pipefail

if [[ -z "${NAS_IP:-}" ]]; then
  printf 'Set NAS_IP to the current LAN address of the Xiaomi NAS.\n' >&2
  exit 2
fi
if [[ -z "${NAS_SSH_KEY:-}" ]]; then
  printf 'Set NAS_SSH_KEY to the root SSH private-key path.\n' >&2
  exit 2
fi

NAS_USER_ID="${NAS_USER_ID:-}"
PLUGIN_ID="${PLUGIN_ID:-1000}"
if [[ -n "${NAS_USER_ID}" && ! "${NAS_USER_ID}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  printf 'NAS_USER_ID may contain only letters, digits, underscores, and hyphens.\n' >&2
  exit 2
fi
if [[ ! "${PLUGIN_ID}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PLUGIN_ID must be a positive integer.\n' >&2
  exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SECURITY_MODULE="${PROJECT_DIR}/plugin_security.py"
[[ -f "${SECURITY_MODULE}" ]] || SECURITY_MODULE="${PROJECT_DIR}/../../shared/plugin_security.py"
[[ -f "${SECURITY_MODULE}" ]] || { printf 'Missing plugin_security.py\n' >&2; exit 2; }
DEPENDENCY_MODULE="$(dirname "${SECURITY_MODULE}")/offline_dependencies.py"
[[ -f "${DEPENDENCY_MODULE}" ]] || { printf 'Missing offline_dependencies.py\n' >&2; exit 2; }
# Fail locally before any SSH call or NAS write if the release lacks locked wheels.
python3 "${DEPENDENCY_MODULE}" verify --requirements "${PROJECT_DIR}/requirements.txt"
RELEASE_ID="release-$(date +%Y%m%d%H%M%S)"
REMOTE_RELEASE="/data/plugin/115-sync/releases/${RELEASE_ID}"
REMOTE_TARGET="root@${NAS_IP}"
TEMP_NGINX="$(mktemp -t xiaomi-115-sync-nginx.XXXXXX)"

cleanup() {
  rm -f "${TEMP_NGINX}"
}
trap cleanup EXIT

SSH_OPTIONS=(
  -i "${NAS_SSH_KEY}"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o PreferredAuthentications=publickey
  -o PubkeyAuthentication=yes
  -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no
  -o StrictHostKeyChecking=accept-new
)

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'true'
if [[ -z "${NAS_USER_ID}" ]]; then
  if ! NAS_USER_ID="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'set -- /data/plugin/u*.list; if [ "$#" -eq 1 ] && [ -f "$1" ]; then name=${1##*/}; printf "%s\n" "${name%.list}"; else exit 1; fi')"; then
    printf 'Could not identify exactly one Xiaomi user registry. Set NAS_USER_ID explicitly.\n' >&2
    exit 2
  fi
  printf '自动识别小米用户：%s\n' "${NAS_USER_ID}"
fi
REMOTE_UI="/home/${NAS_USER_ID}/plugin/115sync/src/ui"
[[ "${NAS_USER_ID}" =~ ^u[0-9]+$ ]] || { printf 'Invalid Xiaomi user ID\n' >&2; exit 2; }
sed "s|__NAS_USER_ID__|${NAS_USER_ID}|g" "${SCRIPT_DIR}/xiaomi-115-sync.nginx.conf" > "${TEMP_NGINX}"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "mkdir -p '${REMOTE_RELEASE}' '${REMOTE_UI}' /data/plugin/115-sync/data /data/plugin/www/icon"

tar -C "${PROJECT_DIR}" -czf - server.py requirements.txt requirements.lock wheelhouse pip-bootstrap | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_RELEASE}'"
scp "${SSH_OPTIONS[@]}" "${SECURITY_MODULE}" "${REMOTE_TARGET}:${REMOTE_RELEASE}/plugin_security.py"
scp "${SSH_OPTIONS[@]}" "${DEPENDENCY_MODULE}" "${REMOTE_TARGET}:${REMOTE_RELEASE}/offline_dependencies.py"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "python3 '${REMOTE_RELEASE}/offline_dependencies.py' install --requirements '${REMOTE_RELEASE}/requirements.txt' --target '${REMOTE_RELEASE}/lib'"
tar -C "${PROJECT_DIR}/web" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_UI}'"

scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/web/assets/115-sync-icon.png" "${REMOTE_TARGET}:/data/plugin/www/icon/115-sync.icon"
scp "${SSH_OPTIONS[@]}" "${SCRIPT_DIR}/xiaomi-115-sync.service" "${REMOTE_TARGET}:/tmp/xiaomi-115-sync.service"
scp "${SSH_OPTIONS[@]}" "${TEMP_NGINX}" "${REMOTE_TARGET}:/tmp/xiaomi-115-sync.nginx.conf"

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "ln -sfn '${REMOTE_RELEASE}' /data/plugin/115-sync/current"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "set -eu; sed -i 's|__NAS_USER_ID__|${NAS_USER_ID}|g' /tmp/xiaomi-115-sync.service; install -m 0644 /tmp/xiaomi-115-sync.service /etc/systemd/system/xiaomi-115-sync.service; python3 '${REMOTE_RELEASE}/plugin_security.py' --template /tmp/xiaomi-115-sync.nginx.conf --target /etc/nginx/conf.d/luci/xiaomi-115-sync.conf --key-file /data/plugin/115-sync/proxy.key; systemctl daemon-reload; nginx -t; systemctl enable xiaomi-115-sync.service; systemctl restart xiaomi-115-sync.service; systemctl reload nginx"

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "python3 - --user-id '${NAS_USER_ID}' --plugin-id '${PLUGIN_ID}'" < "${SCRIPT_DIR}/register_plugin.py"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "systemctl is-active --quiet xiaomi-115-sync.service && python3 -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18115/healthz', timeout=8).read().decode())\""

printf '\n115 云备份已安装到 %s；请在小米智能存储客户端的应用市场中打开。\n' "${NAS_IP}"
