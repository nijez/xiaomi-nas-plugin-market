#!/usr/bin/env bash
# Friendly entry point for the signed Xiaomi community store installer.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/deploy/install-on-nas.sh"
