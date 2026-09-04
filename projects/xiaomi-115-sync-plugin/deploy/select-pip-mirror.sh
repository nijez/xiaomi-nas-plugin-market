#!/bin/sh
# Select a reachable Python package index from the NAS itself. Keep the
# probes small so installation startup is fast and does not fetch packages.

set -eu

fallback='https://pypi.org/simple'

if ! command -v curl >/dev/null 2>&1; then
  printf '%s\n' "${fallback}"
  exit 0
fi

best_url=''
best_time=''

for index_url in \
  'https://pypi.org/simple' \
  'https://pypi.tuna.tsinghua.edu.cn/simple' \
  'https://mirrors.aliyun.com/pypi/simple' \
  'https://pypi.mirrors.ustc.edu.cn/simple' \
  'https://repo.huaweicloud.com/repository/pypi/simple'
do
  result="$(curl --location --silent --show-error --output /dev/null \
    --connect-timeout 3 --max-time 8 --retry 0 \
    --write-out '%{http_code} %{time_total}' "${index_url}/oss2/" 2>/dev/null || true)"
  status="${result%% *}"
  elapsed="${result#* }"

  case "${status}" in
    2??)
      printf '可用 Python 源：%s（%ss）\n' "${index_url}" "${elapsed}" >&2
      if [ -z "${best_url}" ] || awk -v candidate="${elapsed}" -v best="${best_time}" 'BEGIN { exit !(candidate < best) }'; then
        best_url="${index_url}"
        best_time="${elapsed}"
      fi
      ;;
    *)
      printf '跳过不可用 Python 源：%s\n' "${index_url}" >&2
      ;;
  esac
done

printf '%s\n' "${best_url:-${fallback}}"
