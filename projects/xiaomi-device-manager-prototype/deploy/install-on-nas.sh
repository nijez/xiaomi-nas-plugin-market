#!/usr/bin/env bash
# Install or update only the community Device Manager on an authorized Xiaomi NAS.

set -euo pipefail

if [[ -z "${NAS_IP:-}" || -z "${NAS_SSH_KEY:-}" ]]; then
  printf 'Set NAS_IP and NAS_SSH_KEY before running this installer.\n' >&2
  exit 2
fi

NAS_USER_ID="${NAS_USER_ID:-}"
PLUGIN_ID="${PLUGIN_ID:-11001}"
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
RELEASE_ID="release-$(date +%Y%m%d%H%M%S)"
REMOTE_RELEASE="/data/plugin/xiaomi-device-manager/releases/${RELEASE_ID}"
REMOTE_TARGET="root@${NAS_IP}"
TEMP_NGINX="$(mktemp -t xiaomi-device-manager-nginx.XXXXXX)"

cleanup() {
  rm -f "${TEMP_NGINX}"
}
trap cleanup EXIT

for required in \
  "${PROJECT_DIR}/dist/client/index.html" \
  "${PROJECT_DIR}/scripts/nas_status_server.py" \
  "${PROJECT_DIR}/assets/xiaomi-device-manager-v2.png"
do
  if [[ ! -f "${required}" ]]; then
    printf 'Missing release payload: %s\n' "${required}" >&2
    exit 2
  fi
done

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

REMOTE_UI="/home/${NAS_USER_ID}/plugin/devicemanager/src/ui"
sed "s|__NAS_USER_ID__|${NAS_USER_ID}|g" "${SCRIPT_DIR}/xiaomi-device-manager.nginx.conf" > "${TEMP_NGINX}"

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "mkdir -p '${REMOTE_RELEASE}/public' '${REMOTE_UI}' /data/plugin/www/icon"
tar -C "${PROJECT_DIR}/dist/client" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_RELEASE}/public'"
tar -C "${PROJECT_DIR}/dist/client" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_UI}'"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/scripts/nas_status_server.py" "${REMOTE_TARGET}:${REMOTE_RELEASE}/server.py"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/assets/xiaomi-device-manager-v2.png" "${REMOTE_TARGET}:/data/plugin/www/icon/xiaomi-device-manager-v2.icon"
scp "${SSH_OPTIONS[@]}" "${SCRIPT_DIR}/xiaomi-device-manager.service" "${REMOTE_TARGET}:/tmp/xiaomi-device-manager.service"
scp "${SSH_OPTIONS[@]}" "${TEMP_NGINX}" "${REMOTE_TARGET}:/tmp/xiaomi-device-manager.conf"

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "ln -sfn '${REMOTE_RELEASE}' /data/plugin/xiaomi-device-manager/current && install -m 0644 /tmp/xiaomi-device-manager.service /etc/systemd/system/xiaomi-device-manager.service"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'sh -s' <<'REMOTE_NGINX'
set -eu
target=/etc/nginx/conf.d/luci/xiaomi-device-manager.conf
backup="${target}.before-$(date +%s).bak"
if [ -f "${target}" ]; then
  cp -p "${target}" "${backup}"
fi
install -m 0644 /tmp/xiaomi-device-manager.conf "${target}"
if ! nginx -t; then
  if [ -f "${backup}" ]; then
    mv "${backup}" "${target}"
  fi
  nginx -t
  exit 1
fi
systemctl daemon-reload
systemctl enable xiaomi-device-manager.service
systemctl restart xiaomi-device-manager.service
systemctl reload nginx
REMOTE_NGINX

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "python3 - --user-id '${NAS_USER_ID}' --plugin-id '${PLUGIN_ID}'" < "${SCRIPT_DIR}/register_plugin.py"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "systemctl is-active --quiet xiaomi-device-manager.service && curl -fsS --max-time 8 http://127.0.0.1:18080/api/nas-status >/dev/null"

printf '\n设备管家已安装到 %s；请重新打开小米智能存储客户端。\n' "${NAS_IP}"

