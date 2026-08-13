#!/usr/bin/env bash
# Redacted Oracle A1 deployment diagnostic. It never prints secret values.
set -uo pipefail

APP_ROOT=/opt/bitcoin-bot
CURRENT=$APP_ROOT/current
ENV_FILE=/etc/bitcoin-bot/.env
PERSIST=/var/lib/bitcoin-bot/shared
TIMING_TARGET=https://testnet.binance.vision/api/v3/time
TIMING_SAMPLES=${TIMING_SAMPLES:-10}
COMPOSE_PROJECT_NAME=bitcoin-bot

section(){ printf '\n== %s ==\n' "$1"; }
line(){ printf '%-30s %s\n' "$1" "$2"; }
command_value(){
  local label=$1
  shift
  local value
  value=$("$@" 2>/dev/null) || value=unavailable
  line "$label" "$value"
}
http_status(){
  local label=$1 url=$2 code
  code=$(curl -sS -o /dev/null --connect-timeout 5 --max-time 15 \
    -w '%{http_code}' "$url" 2>/dev/null) || code=unreachable
  line "$label" "$code"
}

section 'BITCOIN BOT identity'
if [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]]; then
  python3 - "$ENV_FILE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
allowed = {
    "BOT_PRODUCT": "BITCOIN-BOT",
    "BOT_ENVIRONMENT": "TESTNET",
    "BOT_INSTANCE_ID": "BITCOIN-TN-TYO-01",
    "EXECUTION_MODE": "simulation",
    "MONITOR_BIND_HOST": "127.0.0.1",
    "MONITOR_PORT": "8091",
}
values = {}
for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
    if not match:
        continue
    key, value = match.groups()
    if key in allowed and key not in values:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
for key, default in allowed.items():
    print(f"{key:<30} {values.get(key, default)}")
PY
else
  line 'private environment' 'missing-or-symlink'
fi
command_value hostname hostname

section 'Host'
if [[ -r /etc/os-release ]]; then
  command_value OS sh -c '. /etc/os-release; printf "%s %s" "$NAME" "$VERSION_ID"'
fi
command_value architecture dpkg --print-architecture
command_value kernel uname -r
command_value CPU-count nproc
command_value CPU-model sh -c "lscpu | awk -F: '/Model name/{gsub(/^[[:space:]]+/,\"\",\$2); print \$2; exit}'"
command_value RAM sh -c "free -h | awk '/^Mem:/{print \$2 \" total, \" \$3 \" used, \" \$7 \" available\"}'"
command_value swap sh -c "free -h | awk '/^Swap:/{print \$2 \" total, \" \$3 \" used\"}'"
command_value root-disk sh -c "df -hP / | awk 'NR==2{print \$2 \" total, \" \$3 \" used, \" \$4 \" free, \" \$5 \" used\"}'"

section 'Docker and Compose'
command_value Docker docker version --format '{{.Server.Version}}'
command_value Compose docker compose version --short
command_value Docker-root-dir docker info --format '{{.DockerRootDir}}'
command_value Docker-security docker info --format '{{json .SecurityOptions}}'
if command -v docker >/dev/null 2>&1; then
  docker ps --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --format 'container={{.Names}} status={{.Status}} image={{.Image}}' 2>/dev/null || true
  while IFS= read -r container; do
    [[ -n "$container" ]] || continue
    docker inspect --format \
      'container={{.Name}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} memory={{.HostConfig.Memory}} nano_cpus={{.HostConfig.NanoCpus}} pids={{.HostConfig.PidsLimit}} read_only={{.HostConfig.ReadonlyRootfs}}' \
      "$container" 2>/dev/null || true
  done < <(docker ps -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" 2>/dev/null)
fi

section 'Time'
command_value chrony-active systemctl is-active chrony
command_value chrony-sync sh -c "chronyc tracking | awk -F: '/Leap status|System time|Last offset/{gsub(/^[[:space:]]+/,\"\",\$2); printf \"%s=%s; \",\$1,\$2}'"
command_value UTC-date date -u +%Y-%m-%dT%H:%M:%SZ

section 'Services'
for unit in \
  docker.service chrony.service unattended-upgrades.service \
  bitcoin-bot-resource-guard.timer bitcoin-bot-monitor-testnet.service \
  bitcoin-bot-monitor-snapshot.timer bitcoin-bot-monitor-report-testnet.timer; do
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  line "$unit" "${state:-not-installed}"
done

section 'Release'
if [[ -L "$CURRENT" ]]; then
  command_value current-release readlink -f "$CURRENT"
  [[ -f "$CURRENT/RELEASE_SHA256.txt" ]] && \
    command_value release-hash awk 'NF{print $1;exit}' "$CURRENT/RELEASE_SHA256.txt"
  [[ -f "$CURRENT/.git-commit" ]] && command_value Git-SHA head -n 1 "$CURRENT/.git-commit"
  [[ -f "$CURRENT/RELEASE_MODE" ]] && command_value package-mode head -n 1 "$CURRENT/RELEASE_MODE"
else
  line current-release not-installed
fi
for state_file in \
  "$PERSIST/runtime/deployment_status.json" \
  "$PERSIST/runtime/release_validation.json"; do
  if [[ -f "$state_file" && ! -L "$state_file" ]]; then
    python3 - "$state_file" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
safe = {key: data.get(key) for key in (
    "ok", "status", "release_hash", "execution_mode", "at"
) if key in data}
print(path.name + "=" + json.dumps(safe, sort_keys=True))
PY
  fi
done

section 'Listening TCP ports'
ss -H -ltn 2>/dev/null | awk '{print $4}' | sort -u || true

section 'OCI metadata compatibility'
imds_v2=$(curl -sS -o /dev/null --connect-timeout 2 --max-time 5 \
  -H 'Authorization: Bearer Oracle' -w '%{http_code}' \
  http://169.254.169.254/opc/v2/instance/ 2>/dev/null || true)
imds_v1=$(curl -sS -o /dev/null --connect-timeout 2 --max-time 5 \
  -w '%{http_code}' http://169.254.169.254/opc/v1/instance/ 2>/dev/null || true)
line IMDSv2 "${imds_v2:-unreachable}"
line IMDSv1 "${imds_v1:-unreachable} (404 expected when v2-only is enforced)"

section 'Public HTTPS reachability (no credentials)'
http_status Binance-Testnet-time "$TIMING_TARGET"
http_status Telegram-API https://api.telegram.org
http_status CoinGecko https://api.coingecko.com/api/v3/ping
http_status CoinMarketCap https://pro-api.coinmarketcap.com/v1/key/info

section "Binance Testnet HTTPS timings ($TIMING_SAMPLES samples, seconds)"
[[ "$TIMING_SAMPLES" =~ ^[0-9]+$ && "$TIMING_SAMPLES" -ge 10 && "$TIMING_SAMPLES" -le 100 ]] || {
  echo 'TIMING_SAMPLES must be an integer from 10 through 100' >&2
  exit 1
}
TIMING_FILE=$(mktemp)
trap 'rm -f -- "$TIMING_FILE"' EXIT
for sample in $(seq 1 "$TIMING_SAMPLES"); do
  if ! curl -sS -o /dev/null --connect-timeout 5 --max-time 15 \
    -w '%{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer} %{time_total}\n' \
    "$TIMING_TARGET" >> "$TIMING_FILE"; then
    printf 'nan nan nan nan nan\n' >> "$TIMING_FILE"
  fi
done
python3 - "$TIMING_FILE" <<'PY'
import math
import statistics
import sys
from pathlib import Path

names = ["time_namelookup", "time_connect", "time_appconnect", "time_starttransfer", "time_total"]
columns = [[] for _ in names]
failed = 0
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    try:
        values = [float(value) for value in raw.split()]
    except ValueError:
        failed += 1
        continue
    if len(values) != len(names) or not all(math.isfinite(value) for value in values):
        failed += 1
        continue
    for column, value in zip(columns, values):
        column.append(value)
print(f"successful_samples={len(columns[0])} failed_samples={failed}")
for name, values in zip(names, columns):
    if not values:
        print(f"{name}: unavailable")
        continue
    ordered = sorted(values)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    print(
        f"{name}: min={ordered[0]:.6f} median={statistics.median(ordered):.6f} "
        f"p95={p95:.6f} max={ordered[-1]:.6f}"
    )
PY

section 'Result'
line diagnostic 'completed; Oracle acceptance still requires operator review of every unavailable/non-active item'
