#!/usr/bin/env bash
# Install or update only the Jingsu ERP link on the authorized Xiaomi NAS.

set -euo pipefail

if [[ -z "${NAS_IP:-}" || -z "${NAS_SSH_KEY:-}" ]]; then
  printf 'Set NAS_IP and NAS_SSH_KEY before running this installer.\n' >&2
  exit 2
fi

NAS_USER_ID="${NAS_USER_ID:?Set NAS_USER_ID}"
PLUGIN_ID="${PLUGIN_ID:-1003}"
ERP_UPSTREAM_HOST="${ERP_UPSTREAM_HOST:?Set ERP_UPSTREAM_HOST}"
ERP_UPSTREAM_PORT="${ERP_UPSTREAM_PORT:-8080}"

if [[ ! "${NAS_USER_ID}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  printf 'NAS_USER_ID contains unsupported characters.\n' >&2
  exit 2
fi
if [[ ! "${PLUGIN_ID}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PLUGIN_ID must be a positive integer.\n' >&2
  exit 2
fi
if [[ ! "${ERP_UPSTREAM_HOST}" =~ ^[A-Za-z0-9.-]+$ ]]; then
  printf 'ERP_UPSTREAM_HOST must be an IPv4 address or DNS hostname.\n' >&2
  exit 2
fi
if [[ ! "${ERP_UPSTREAM_PORT}" =~ ^[1-9][0-9]{0,4}$ ]] || (( ERP_UPSTREAM_PORT > 65535 )); then
  printf 'ERP_UPSTREAM_PORT must be between 1 and 65535.\n' >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RELEASE_ID="release-$(date +%Y%m%d%H%M%S)"
REMOTE_RELEASE="/data/plugin/erp-link/releases/${RELEASE_ID}"
REMOTE_TARGET="root@${NAS_IP}"
TEMP_SERVICE="$(mktemp -t xiaomi-erp-link-service.XXXXXX)"

cleanup() {
  rm -f "${TEMP_SERVICE}"
}
trap cleanup EXIT

sed \
  -e "s|__ERP_UPSTREAM_HOST__|${ERP_UPSTREAM_HOST}|g" \
  -e "s|__ERP_UPSTREAM_PORT__|${ERP_UPSTREAM_PORT}|g" \
  "${SCRIPT_DIR}/xiaomi-erp-link.service" > "${TEMP_SERVICE}"

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
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "mkdir -p '${REMOTE_RELEASE}' /data/plugin/www/icon"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/proxy_server.py" "${REMOTE_TARGET}:${REMOTE_RELEASE}/proxy_server.py"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/web/assets/jingsu-erp-icon-v4.png" "${REMOTE_TARGET}:/data/plugin/www/icon/jingsu-erp.icon"
scp "${SSH_OPTIONS[@]}" "${TEMP_SERVICE}" "${REMOTE_TARGET}:/tmp/xiaomi-erp-link.service"
scp "${SSH_OPTIONS[@]}" "${SCRIPT_DIR}/xiaomi-erp-link.nginx.conf" "${REMOTE_TARGET}:/tmp/xiaomi-erp-link.nginx.conf"

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "ln -sfn '${REMOTE_RELEASE}' /data/plugin/erp-link/current && install -m 0644 /tmp/xiaomi-erp-link.service /etc/systemd/system/xiaomi-erp-link.service && systemctl daemon-reload && systemctl enable xiaomi-erp-link.service && systemctl restart xiaomi-erp-link.service"

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'sh -s' <<'REMOTE_NGINX'
set -eu
target=/etc/nginx/conf.d/luci/xiaomi-erp-link.conf
backup="${target}.before-$(date +%s).bak"
if [ -f "${target}" ]; then
  cp -p "${target}" "${backup}"
fi
install -m 0644 /tmp/xiaomi-erp-link.nginx.conf "${target}"
if ! nginx -t; then
  if [ -f "${backup}" ]; then
    mv "${backup}" "${target}"
  else
    rm -f "${target}"
  fi
  nginx -t
  exit 1
fi
systemctl reload nginx
REMOTE_NGINX

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "python3 - --user-id '${NAS_USER_ID}' --plugin-id '${PLUGIN_ID}'" < "${SCRIPT_DIR}/register_plugin.py"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "systemctl is-active --quiet xiaomi-erp-link.service && curl -fsS --max-time 10 http://127.0.0.1:18118/__health && curl -fsS --max-time 10 -H 'X-ERP-Plugin-Base: /plugin/${NAS_USER_ID}/erplink' http://127.0.0.1:18118/ >/dev/null"

printf '\n精速 ERP 已注册到 %s，代理上游为 %s:%s。\n' "${NAS_IP}" "${ERP_UPSTREAM_HOST}" "${ERP_UPSTREAM_PORT}"
