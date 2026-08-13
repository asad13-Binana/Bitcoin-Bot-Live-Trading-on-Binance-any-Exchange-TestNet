#!/usr/bin/env bash
set -Eeuo pipefail

# Run the read-only API readiness probe with the same trusted environment-file
# parser used by the privileged installer.  The environment file is parsed as
# literal data; it is never sourced or evaluated as shell code.

[[ ${EUID:-$(id -u)} -eq 0 ]] || {
  echo 'ERROR: api_preflight.sh must run as root' >&2
  exit 1
}

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
ENV_FILE=${BITCOIN_BOT_ENV_FILE:-/etc/bitcoin-bot/.env}
# shellcheck source=deploy/lib/envfile.sh
source "$ROOT/deploy/lib/envfile.sh"
env_file_require_trusted "$ENV_FILE"
env_file_load "$ENV_FILE"

export PYTHONPATH="$ROOT"
exec /usr/bin/python3 "$ROOT/scripts/api_readiness.py" "$@"
