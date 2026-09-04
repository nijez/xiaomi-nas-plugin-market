#!/usr/bin/env bash
# One-time bootstrap for the community store on an authorized Xiaomi NAS.

set -euo pipefail

NAS_IP="${NAS_IP:-}"
NAS_SSH_KEY="${NAS_SSH_KEY:-}"
if [[ -z "${NAS_IP}" && -t 0 ]]; then
  printf '请输入小米 NAS 当前局域网 IP：' >&2
  read -r NAS_IP
fi
if [[ ! "${NAS_IP}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  printf '请重新运行并输入有效 NAS IP，或设置 NAS_IP。\n' >&2
  exit 2
fi
if [[ -z "${NAS_SSH_KEY}" ]]; then
  for candidate in \
    "${HOME}/.xiaomi-nas-root/nas-root-key" \
    "${HOME}/.ssh/xiaomi-nas-root" \
    "${HOME}/.ssh/nas-root-key"
  do
    if [[ -f "${candidate}" ]]; then
      NAS_SSH_KEY="${candidate}"
      break
    fi
  done
fi
if [[ -z "${NAS_SSH_KEY}" && -t 0 ]]; then
  printf '未自动找到 SSH 密钥，请输入 root 私钥路径：' >&2
  read -r NAS_SSH_KEY
  NAS_SSH_KEY="${NAS_SSH_KEY/#\~/${HOME}}"
fi
if [[ ! -f "${NAS_SSH_KEY}" ]]; then
  printf '未找到 root 脚本生成的 SSH 密钥。可设置 NAS_SSH_KEY 后重试。\n' >&2
  exit 2
fi
NAS_USER_ID="${NAS_USER_ID:-}"
PLUGIN_ID="${PLUGIN_ID:-11002}"
if [[ -n "${NAS_USER_ID}" && ! "${NAS_USER_ID}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  printf 'NAS_USER_ID contains unsupported characters.\n' >&2
  exit 2
fi
if [[ ! "${PLUGIN_ID}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PLUGIN_ID must be a positive integer.\n' >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REMOTE="root@${NAS_IP}"
RELEASE_ID="0.1.1-$(date +%Y%m%d%H%M%S)"
REMOTE_RELEASE="/data/plugin/community-store/releases/${RELEASE_ID}"
TMP_SERVICE="$(mktemp -t xiaomi-community-store-service.XXXXXX)"
trap 'rm -f "${TMP_SERVICE}"' EXIT

for required in server.py storelib.py web/index.html web/assets/community-store-v4.png catalog/catalog.json catalog/catalog.json.sig catalog/repository-public.pem; do
  if [[ ! -f "${PROJECT_DIR}/${required}" ]]; then
    printf 'Release payload is incomplete: %s\n' "${required}" >&2
    exit 2
  fi
done

SSH=(
  -i "${NAS_SSH_KEY}"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o PreferredAuthentications=publickey
  -o PubkeyAuthentication=yes
  -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no
  -o StrictHostKeyChecking=accept-new
)
SCP=("${SSH[@]}")
SSH_VERSION="$(ssh -V 2>&1)"
if [[ "${SSH_VERSION}" =~ OpenSSH_([0-9]+) ]] && (( BASH_REMATCH[1] >= 9 )); then
  SCP+=(-O)
fi

ssh "${SSH[@]}" "${REMOTE}" 'command -v python3 >/dev/null && command -v openssl >/dev/null && command -v nginx >/dev/null && command -v systemctl >/dev/null'
if [[ -z "${NAS_USER_ID}" ]]; then
  REGISTRY_USERS="$(ssh "${SSH[@]}" "${REMOTE}" 'for path in /data/plugin/u*.list; do [ -f "$path" ] || continue; name=${path##*/}; printf "%s\n" "${name%.list}"; done')"
  REGISTRY_COUNT="$(printf '%s\n' "${REGISTRY_USERS}" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [[ "${REGISTRY_COUNT}" == "1" ]]; then
    NAS_USER_ID="$(printf '%s\n' "${REGISTRY_USERS}" | sed '/^$/d')"
  else
    CERT_DIR="/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
    CERT_MATCHES=""
    if [[ -d "${CERT_DIR}" ]]; then
      for certificate in "${CERT_DIR}"/*_cert.pem; do
        [[ -f "${certificate}" ]] || continue
        filename="${certificate##*/}"
        candidate="u${filename%%_*}"
        if printf '%s\n' "${REGISTRY_USERS}" | grep -Fqx "${candidate}"; then
          CERT_MATCHES="${CERT_MATCHES}${candidate}"$'\n'
        fi
      done
    fi
    CERT_MATCHES="$(printf '%s' "${CERT_MATCHES}" | sed '/^$/d' | sort -u)"
    MATCH_COUNT="$(printf '%s\n' "${CERT_MATCHES}" | sed '/^$/d' | wc -l | tr -d ' ')"
    if [[ "${MATCH_COUNT}" == "1" ]]; then
      NAS_USER_ID="${CERT_MATCHES}"
    elif [[ -t 0 ]]; then
      printf '检测到多个小米账号，请输入当前账号对应的用户 ID：\n%s\n用户 ID：' "${REGISTRY_USERS}" >&2
      read -r NAS_USER_ID
    else
      printf '无法自动确定小米账号。请设置 NAS_USER_ID 后重试。候选值：\n%s\n' "${REGISTRY_USERS}" >&2
      exit 2
    fi
  fi
fi
if [[ ! "${NAS_USER_ID}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  printf '无法使用小米用户 ID：%s\n' "${NAS_USER_ID}" >&2
  exit 2
fi
printf '使用小米用户：%s\n' "${NAS_USER_ID}"
URL_USER_ID="${NAS_USER_ID#u}"
sed \
  -e "s|__NAS_USER_ID__|${NAS_USER_ID}|g" \
  -e "s|__URL_USER_ID__|${URL_USER_ID}|g" \
  "${SCRIPT_DIR}/xiaomi-community-store.service" > "${TMP_SERVICE}"

ssh "${SSH[@]}" "${REMOTE}" "mkdir -p '${REMOTE_RELEASE}' /data/plugin/community-store/state /data/plugin/community-store/staging /data/plugin/community-store/backups /data/plugin/www/icon"
tar -C "${PROJECT_DIR}" -czf - server.py storelib.py web catalog | ssh "${SSH[@]}" "${REMOTE}" "tar -xzf - -C '${REMOTE_RELEASE}'"
scp "${SCP[@]}" "${TMP_SERVICE}" "${REMOTE}:/tmp/xiaomi-community-store.service"
scp "${SCP[@]}" "${SCRIPT_DIR}/xiaomi-community-store.nginx.conf" "${REMOTE}:/tmp/xiaomi-community-store.conf"
scp "${SCP[@]}" "${SCRIPT_DIR}/register_plugin.py" "${REMOTE}:/tmp/register-community-store.py"
scp "${SCP[@]}" "${PROJECT_DIR}/web/assets/community-store-v4.png" "${REMOTE}:/data/plugin/www/icon/community-store-v4.icon"
SESSION_SECRET="$(ssh "${SSH[@]}" "${REMOTE}" 'set -eu; token=/data/plugin/community-store/admin-token; if [ ! -s "$token" ]; then umask 077; openssl rand -hex 16 > "$token"; fi; chmod 600 "$token"; cat "$token"')"
if [[ ! "${SESSION_SECRET}" =~ ^[0-9a-f]{32}$ ]]; then
  printf 'The NAS did not return a valid community-store session secret.\n' >&2
  exit 1
fi

ssh "${SSH[@]}" "${REMOTE}" "openssl dgst -sha256 -verify '${REMOTE_RELEASE}/catalog/repository-public.pem' -signature '${REMOTE_RELEASE}/catalog/catalog.json.sig' '${REMOTE_RELEASE}/catalog/catalog.json' >/dev/null"
ssh "${SSH[@]}" "${REMOTE}" "sh -s" <<REMOTE_APPLY
set -eu
release='${REMOTE_RELEASE}'
service=/etc/systemd/system/xiaomi-community-store.service
nginx_conf=/etc/nginx/conf.d/luci/xiaomi-community-store.conf
stamp=\$(date +%s)
[ ! -f "\${service}" ] || cp -p "\${service}" "\${service}.before-\${stamp}.bak"
[ ! -f "\${nginx_conf}" ] || cp -p "\${nginx_conf}" "\${nginx_conf}.before-\${stamp}.bak"
install -m 0644 /tmp/xiaomi-community-store.service "\${service}"
install -m 0644 /tmp/xiaomi-community-store.conf "\${nginx_conf}"
ln -sfn "\${release}" /data/plugin/community-store/current
if ! nginx -t; then
  printf 'Nginx validation failed; inspect timestamped backups.\n' >&2
  exit 1
fi
systemctl daemon-reload
systemctl enable xiaomi-community-store.service
systemctl restart xiaomi-community-store.service
systemctl reload nginx
REMOTE_APPLY
ssh "${SSH[@]}" "${REMOTE}" "python3 /tmp/register-community-store.py --user-id '${NAS_USER_ID}' --plugin-id '${PLUGIN_ID}'"
ssh "${SSH[@]}" "${REMOTE}" "systemctl is-active --quiet xiaomi-community-store.service && python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:18119/healthz', timeout=8)\""

printf '\n插件市场已安装。请完全退出并重新打开小米智能存储客户端。\n'
printf '插件市场会使用小米客户端入口自动授权，不需要管理码。\n'
