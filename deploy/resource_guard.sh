#!/usr/bin/env bash
# Root-only disk/inode guard for the canonical Bitcoin Compose project.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo 'ERROR: resource_guard.sh must run as root' >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
PROJECT=$COMPOSE_PROJECT_NAME
STATUS_FILE=$PERSIST/runtime/resource_guard.json
CONTAINER_STATUS_FILE=$PERSIST/runtime/container_status.json
DISK_WARN_PERCENT=${DISK_WARN_PERCENT:-85}
DISK_CRITICAL_PERCENT=${DISK_CRITICAL_PERCENT:-95}
INODE_WARN_PERCENT=${INODE_WARN_PERCENT:-85}
INODE_CRITICAL_PERCENT=${INODE_CRITICAL_PERCENT:-95}
MIN_FREE_MIB=${MIN_FREE_MIB:-1024}
EXPECTED_SERVICES=(moneyflow freqtrade execution-sidecar telegram-broker)

fail(){ echo "ERROR: $*" >&2; exit 1; }
for value in \
  DISK_WARN_PERCENT DISK_CRITICAL_PERCENT INODE_WARN_PERCENT \
  INODE_CRITICAL_PERCENT MIN_FREE_MIB; do
  [[ ${!value} =~ ^[0-9]+$ ]] || fail "$value must be an integer"
done
(( DISK_WARN_PERCENT < DISK_CRITICAL_PERCENT && DISK_CRITICAL_PERCENT <= 100 )) || \
  fail 'disk thresholds are invalid'
(( INODE_WARN_PERCENT < INODE_CRITICAL_PERCENT && INODE_CRITICAL_PERCENT <= 100 )) || \
  fail 'inode thresholds are invalid'

read -r _ _ free_kib _ used_text _ < <(df -Pk "$PERSIST" | awk 'NR==2{print $1,$2,$4,$3,$5,$6}')
read -r _ _ _ _ inode_text _ < <(df -Pi "$PERSIST" | awk 'NR==2{print $1,$2,$4,$3,$5,$6}')
disk_used=${used_text%%%}
inode_used=${inode_text%%%}
free_mib=$((free_kib / 1024))
state=OK
reason=within-thresholds
if (( disk_used >= DISK_CRITICAL_PERCENT || inode_used >= INODE_CRITICAL_PERCENT || free_mib < MIN_FREE_MIB )); then
  state=CRITICAL
  reason=storage-pressure-stop
elif (( disk_used >= DISK_WARN_PERCENT || inode_used >= INODE_WARN_PERCENT )); then
  state=WARNING
  reason=storage-pressure-warning
fi

stopped=()
if [[ "$state" == CRITICAL ]] && command -v docker >/dev/null 2>&1; then
  while IFS= read -r container; do
    [[ -n "$container" ]] || continue
    service=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$container" 2>/dev/null || true)
    allowed=false
    for expected in "${EXPECTED_SERVICES[@]}"; do
      [[ "$service" == "$expected" ]] && allowed=true
    done
    if [[ "$allowed" == true ]]; then
      docker stop --time 120 "$container" >/dev/null
      stopped+=("$service")
    else
      echo "WARNING: refusing to control unidentified project container $container" >&2
    fi
  done < <(docker ps -q --filter "label=com.docker.compose.project=$PROJECT")
fi

mkdir -p "$PERSIST/runtime"

# Docker access belongs exclusively to this root-owned safety guard.  Emit the
# narrow, sanitized status document consumed by the unprivileged monitor; the
# monitor itself never receives the Docker socket or Docker CLI.
CONTAINER_STATUS_TMP=$(mktemp "$PERSIST/runtime/.container_status.XXXXXX")
trap 'rm -f -- "$CONTAINER_STATUS_TMP"' EXIT
python3 - "$CONTAINER_STATUS_TMP" "$PROJECT" <<'PY'
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

path, project = sys.argv[1:]
payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "containers": [],
}
docker = "/usr/bin/docker"
if not os.path.isfile(docker):
    payload["error"] = "docker_missing"
else:
    listed = subprocess.run(
        [docker, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if listed.returncode != 0:
        payload["error"] = "docker_query_failed"
    else:
        identifiers = listed.stdout.split()
        if identifiers:
            inspected = subprocess.run(
                [docker, "inspect", *identifiers],
                capture_output=True,
                check=False,
                text=True,
                timeout=15,
            )
            if inspected.returncode != 0:
                payload["error"] = "docker_inspect_failed"
            else:
                try:
                    raw = json.loads(inspected.stdout)
                except json.JSONDecodeError:
                    raw = []
                    payload["error"] = "docker_inspect_invalid_json"
                for item in raw:
                    state = item.get("State") or {}
                    labels = (item.get("Config") or {}).get("Labels") or {}
                    if labels.get("com.docker.compose.project") != project:
                        continue
                    payload["containers"].append({
                        "service": labels.get("com.docker.compose.service"),
                        "name": str(item.get("Name") or "").lstrip("/"),
                        "status": state.get("Status") or "unknown",
                        "health": (state.get("Health") or {}).get("Status") or "none",
                        "restart_count": int(item.get("RestartCount") or 0),
                        "started_at": state.get("StartedAt"),
                    })
                payload["containers"].sort(key=lambda value: str(value.get("service")))
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(path, 0o644)
PY
mv -fT "$CONTAINER_STATUS_TMP" "$CONTAINER_STATUS_FILE"
trap - EXIT

STATUS_TMP=$(mktemp "$PERSIST/runtime/.resource_guard.XXXXXX")
trap 'rm -f -- "$STATUS_TMP"' EXIT
python3 - "$STATUS_TMP" "$state" "$reason" "$disk_used" "$inode_used" "$free_mib" "${stopped[*]:-}" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path = sys.argv[1]
payload = {
    "state": sys.argv[2],
    "reason": sys.argv[3],
    "disk_used_percent": int(sys.argv[4]),
    "inode_used_percent": int(sys.argv[5]),
    "free_mib": int(sys.argv[6]),
    "stopped_services": sorted(value for value in sys.argv[7].split() if value),
    "at": datetime.now(timezone.utc).isoformat(),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(path, 0o640)
PY
owner=$(stat -c '%u:%g' "$PERSIST/runtime")
chown "$owner" "$STATUS_TMP"
mv -fT "$STATUS_TMP" "$STATUS_FILE"
trap - EXIT

printf 'bitcoin-bot resource state=%s disk=%s%% inode=%s%% free=%sMiB stopped=%s\n' \
  "$state" "$disk_used" "$inode_used" "$free_mib" "${stopped[*]:-none}"
[[ "$state" != CRITICAL ]]
