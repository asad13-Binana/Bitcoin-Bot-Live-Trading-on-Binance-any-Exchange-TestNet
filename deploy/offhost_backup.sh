#!/usr/bin/env bash
# Encrypt and copy the newest validated local backup to private OCI Object Storage.
# The VM receives only an age public recipient and OCI instance-principal access.
set -Eeuo pipefail
umask 077

CONFIG=/etc/bitcoin-bot/offhost-backup.env
BACKUP_ROOT=/var/backups/bitcoin-bot
VALIDATOR=/usr/local/libexec/bitcoin-bot/verify_backup.sh
STATUS=/var/lib/bitcoin-bot/shared/runtime/offhost_backup_status.json
LOCK=/var/lock/bitcoin-bot.offhost-backup.lock
OCI_IMAGE_ARM64='ghcr.io/oracle/oci-cli:sha-45aa4a4@sha256:efaeca93e2adc0411151bcde39a9c945bc6245cbf8d3117fa7c526653492eb19'
OCI_IMAGE_AMD64='ghcr.io/oracle/oci-cli:sha-45aa4a4@sha256:e781329f06b345e1322260a5594d365a088dac77cef2b0bb394a5acf40804cea'

fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'offhost_backup.sh must run as root'
[[ $# -le 1 ]] || fail 'usage: offhost_backup.sh [--preflight]'
mode=${1:---upload}
[[ "$mode" == --upload || "$mode" == --preflight ]] || fail 'unknown option'
stage=
on_exit(){
  code=$?
  [[ -z "$stage" || ! -d "$stage" ]] || rm -rf --one-file-system -- "$stage"
  if (( code != 0 )) && [[ -d $(dirname "$STATUS") && ! -L $(dirname "$STATUS") ]]; then
    python3 - "$STATUS" "$code" <<'PY' >/dev/null 2>&1 || true
import json, os, pathlib, sys, tempfile
from datetime import datetime, timezone
path = pathlib.Path(sys.argv[1])
payload = {
    "ok": False,
    "failed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "exit_code": int(sys.argv[2]),
    "authentication": "instance_principal",
}
fd, temporary = tempfile.mkstemp(prefix=".offhost-status.", dir=path.parent)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chown(temporary, 0, path.parent.stat().st_gid)
os.chmod(temporary, 0o640)
os.replace(temporary, path)
PY
  fi
  exit "$code"
}
trap on_exit EXIT

for command in age docker flock python3 sha256sum tar; do
  command -v "$command" >/dev/null 2>&1 || fail "required command is missing: $command"
done
[[ -f "$CONFIG" && ! -L "$CONFIG" ]] || fail 'off-host backup configuration is missing or a symlink'
[[ $(stat -c '%U:%G:%a' "$CONFIG") == root:root:600 ]] || fail 'off-host backup configuration must be root:root mode 0600'
[[ -d "$BACKUP_ROOT" && ! -L "$BACKUP_ROOT" ]] || fail 'canonical local backup root is unavailable'
[[ -f "$VALIDATOR" && ! -L "$VALIDATOR" ]] || fail 'local backup validator is unavailable'

declare -A cfg=()
while IFS= read -r raw || [[ -n "$raw" ]]; do
  [[ "$raw" != *$'\r'* ]] || fail 'configuration contains carriage returns'
  [[ -z "$raw" || "$raw" == \#* ]] && continue
  [[ "$raw" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]] || fail 'configuration contains an invalid line'
  key=${BASH_REMATCH[1]}
  value=${BASH_REMATCH[2]}
  case "$key" in
    OFFHOST_BACKUP_ENABLED|OCI_NAMESPACE|OCI_BUCKET|OCI_OBJECT_PREFIX|AGE_RECIPIENT) ;;
    *) fail "unsupported configuration key: $key" ;;
  esac
  [[ ! -v "cfg[$key]" ]] || fail "duplicate configuration key: $key"
  cfg[$key]=$value
done <"$CONFIG"

[[ ${cfg[OFFHOST_BACKUP_ENABLED]:-} == true ]] || fail 'off-host backup is not explicitly enabled'
namespace=${cfg[OCI_NAMESPACE]:-}
bucket=${cfg[OCI_BUCKET]:-}
prefix=${cfg[OCI_OBJECT_PREFIX]:-}
recipient=${cfg[AGE_RECIPIENT]:-}
[[ "$namespace" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || fail 'OCI_NAMESPACE is invalid'
[[ "$bucket" =~ ^[A-Za-z0-9._-]{1,256}$ ]] || fail 'OCI_BUCKET is invalid'
[[ "$prefix" =~ ^[A-Za-z0-9._/-]{1,128}$ && "$prefix" != /* && "$prefix" != */ && "$prefix" != *..* ]] || fail 'OCI_OBJECT_PREFIX is invalid'
[[ "$recipient" =~ ^age1[0-9a-z]{20,100}$ ]] || fail 'AGE_RECIPIENT is not an age public recipient'

case $(dpkg --print-architecture) in
  arm64) oci_image=$OCI_IMAGE_ARM64 ;;
  amd64) oci_image=$OCI_IMAGE_AMD64 ;;
  *) fail 'OCI CLI image is pinned only for arm64 and amd64' ;;
esac
[[ "$oci_image" == *@sha256:* ]] || fail 'OCI CLI image is not digest pinned'
docker image inspect "$oci_image" >/dev/null 2>&1 || fail 'pinned OCI CLI image is not installed; run configure_offhost_backup.sh'

run_oci(){
  docker run --rm --pull never --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 100 --memory 512m --cpus 1 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    -e HOME=/tmp -e OCI_CLI_AUTH=instance_principal \
    "$oci_image" "$@"
}

exec 9>"$LOCK"
flock -n 9 || fail 'another off-host backup is running'

# Prove the instance principal can see exactly the configured private bucket.
run_oci os bucket get --auth instance_principal \
  --namespace-name "$namespace" --name "$bucket" >/dev/null
if [[ "$mode" == --preflight ]]; then
  echo 'offhost_backup_preflight=PASS; no backup was uploaded'
  exit 0
fi

latest=$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
  -name '20????????T??????Z' -printf '%f\n' | sort -r | sed -n '1p')
[[ "$latest" =~ ^20[0-9]{6}T[0-9]{6}Z$ ]] || fail 'no timestamped local backup is available'
source_backup=$BACKUP_ROOT/$latest
"$VALIDATOR" "$source_backup" >/dev/null

stage=$(mktemp -d "$BACKUP_ROOT/.offhost.XXXXXX")
encrypted=$stage/$latest.tar.age
remote_object=$prefix/$latest.tar.age
tar --numeric-owner --one-file-system -C "$BACKUP_ROOT" -cf - "$latest" | \
  age --encrypt --recipient "$recipient" --output "$encrypted"
chmod 0600 "$encrypted"

read -r sha_hex sha_b64 < <(python3 - "$encrypted" <<'PY'
import base64
import hashlib
import pathlib
import sys

digest = hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).digest()
print(digest.hex(), base64.b64encode(digest).decode("ascii"))
PY
)
[[ "$sha_hex" =~ ^[0-9a-f]{64}$ && -n "$sha_b64" ]] || fail 'could not compute encrypted backup SHA-256'
printf '%s  %s\n' "$sha_hex" "$(basename "$encrypted")" >"$stage/$latest.tar.age.sha256"
chmod 0600 "$stage/$latest.tar.age.sha256"

docker run --rm --pull never --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 100 --memory 512m --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e OCI_CLI_AUTH=instance_principal \
  --mount "type=bind,src=$stage,dst=/backup" \
  "$oci_image" os object put --auth instance_principal \
  --namespace-name "$namespace" --bucket-name "$bucket" \
  --name "$remote_object" --file "/backup/$(basename "$encrypted")" \
  --no-overwrite --no-multipart --verify-checksum --opc-checksum-algorithm SHA256 \
  --opc-content-sha256 "$sha_b64" --content-type application/octet-stream >/dev/null

# A successful PUT is not enough: download the encrypted object and compare its
# exact SHA-256 before recording the backup as durable.
docker run --rm --pull never --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 100 --memory 512m --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e OCI_CLI_AUTH=instance_principal \
  --mount "type=bind,src=$stage,dst=/backup" \
  "$oci_image" os object get --auth instance_principal \
  --namespace-name "$namespace" --bucket-name "$bucket" \
  --name "$remote_object" --file /backup/verified-download.age >/dev/null
[[ $(sha256sum "$stage/verified-download.age" | awk '{print $1}') == "$sha_hex" ]] || \
  fail 'downloaded Object Storage copy does not match the encrypted local backup'

sidecar=$stage/$latest.tar.age.sha256
sidecar_sha_b64=$(python3 - "$sidecar" <<'PY'
import base64, hashlib, pathlib, sys
print(base64.b64encode(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).digest()).decode("ascii"))
PY
)
docker run --rm --pull never --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 100 --memory 512m --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  -e HOME=/tmp -e OCI_CLI_AUTH=instance_principal \
  --mount "type=bind,src=$stage,dst=/backup,readonly" \
  "$oci_image" os object put --auth instance_principal \
  --namespace-name "$namespace" --bucket-name "$bucket" \
  --name "$remote_object.sha256" --file "/backup/$(basename "$sidecar")" \
  --no-overwrite --no-multipart --verify-checksum --opc-checksum-algorithm SHA256 \
  --opc-content-sha256 "$sidecar_sha_b64" --content-type text/plain >/dev/null

status_dir=$(dirname "$STATUS")
[[ -d "$status_dir" && ! -L "$status_dir" ]] || fail 'status directory is unavailable or a symlink'
python3 - "$STATUS" "$latest" "$remote_object" "$sha_hex" <<'PY'
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

path = pathlib.Path(sys.argv[1])
payload = {
    "ok": True,
    "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "source_backup": sys.argv[2],
    "object_name": sys.argv[3],
    "encrypted_sha256": sys.argv[4],
    "authentication": "instance_principal",
}
fd, temporary = tempfile.mkstemp(prefix=".offhost-status.", dir=path.parent)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chown(temporary, 0, path.parent.stat().st_gid)
os.chmod(temporary, 0o640)
os.replace(temporary, path)
PY

echo "offhost_backup=PASS object=$remote_object sha256=$sha_hex"
