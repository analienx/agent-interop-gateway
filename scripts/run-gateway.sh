#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${AIGW_TOKEN:-}" ]]; then
  echo "warning: AIGW_TOKEN is not set; local requests are unauthenticated" >&2
fi
exec aigw serve "$@"
