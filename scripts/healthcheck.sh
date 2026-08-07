#!/usr/bin/env bash
# Require the canonical Bitcoin stack to be present, running and healthy.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

EXPECTED=(moneyflow freqtrade execution-sidecar telegram-broker)
COMPOSE_PROJECT_NAME=bitcoin-bot
CONFIG_ROOT=/var/lib/bitcoin-bot/config-snapshots
CONFIG_FILE=${BITCOIN_BOT_ENV_FILE:-$CONFIG_ROOT/$(basename "$ROOT").env}
if [[ ! -f "$CONFIG_FILE" ]]; then
  [[ -f "$ROOT/.env" ]] || {
    echo "private runtime config not found: $CONFIG_FILE" >&2
    exit 1
  }
  CONFIG_FILE="$ROOT/.env"
fi
RELEASE_HASH=$(awk 'NF{print $1;exit}' RELEASE_SHA256.txt)
[[ "$RELEASE_HASH" =~ ^[0-9a-f]{64}$ ]] || { echo 'invalid release hash' >&2; exit 1; }
RELEASE_TAG="bitcoin-${RELEASE_HASH:0:16}"
CLEAN_ENV=(env -i "PATH=$PATH" "HOME=${HOME:-/tmp}" \
  "COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME" "RELEASE_TAG=$RELEASE_TAG" \
  "SIDECAR_RELEASE_HASH=$RELEASE_HASH" "ENVELOPE_RELEASE_HASH=$RELEASE_HASH")
for passthrough in DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG XDG_RUNTIME_DIR; do
  [[ -n "${!passthrough:-}" ]] && CLEAN_ENV+=("$passthrough=${!passthrough}")
done
COMPOSE=("${CLEAN_ENV[@]}" docker compose --env-file "$CONFIG_FILE")

fail=0
mapfile -t running < <("${COMPOSE[@]}" ps --status running --services 2>/dev/null | sort)
for service in "${EXPECTED[@]}"; do
  if ! printf '%s\n' "${running[@]}" | grep -qx "$service"; then
    echo "NOT RUNNING: $service" >&2
    fail=1
    continue
  fi
  cid="$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    echo "NO CONTAINER: $service" >&2
    fail=1
    continue
  fi
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)"
  if [[ "$status" != healthy ]]; then
    echo "UNHEALTHY: $service ($status)" >&2
    fail=1
  fi
done

if [[ "${#running[@]}" -ne "${#EXPECTED[@]}" ]]; then
  echo "UNEXPECTED SERVICE COUNT: running=${#running[@]} expected=${#EXPECTED[@]}" >&2
  fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  "${COMPOSE[@]}" ps || true
  exit 1
fi
echo "all four Bitcoin Bot services are present, running and healthy"
