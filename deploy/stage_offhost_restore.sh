#!/usr/bin/env bash
# Download, verify, decrypt, and validate one backup without restoring live state.
set -Eeuo pipefail
umask 077

CONFIG=/etc/bitcoin-bot/offhost-backup.env
BACKUP_ROOT=/var/backups/bitcoin-bot
VALIDATOR=/usr/local/libexec/bitcoin-bot/verify_backup.sh
OCI_IMAGE_ARM64='ghcr.io/oracle/oci-cli:sha-45aa4a4@sha256:efaeca93e2adc0411151bcde39a9c945bc6245cbf8d3117fa7c526653492eb19'
OCI_IMAGE_AMD64='ghcr.io/oracle/oci-cli:sha-45aa4a4@sha256:e781329f06b345e1322260a5594d365a088dac77cef2b0bb394a5acf40804cea'

fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'stage_offhost_restore.sh must run as root'
[[ $# -eq 2 ]] || fail 'usage: stage_offhost_restore.sh YYYYMMDDTHHMMSSZ /offline/path/age-identity.txt'
stamp=$1
identity=$2
[[ "$stamp" =~ ^20[0-9]{6}T[0-9]{6}Z$ ]] || fail 'backup timestamp is invalid'
[[ -f "$identity" && ! -L "$identity" ]] || fail 'age identity must be a regular non-symlink file'
[[ $(stat -c '%U:%G:%a' "$identity") == root:root:600 ]] || fail 'age identity must be root:root mode 0600'
for command in age docker python3 sha256sum tar; do
  command -v "$command" >/dev/null 2>&1 || fail "required command is missing: $command"
done
[[ -f "$CONFIG" && ! -L "$CONFIG" ]] || fail 'off-host backup configuration is unavailable'
[[ $(stat -c '%U:%G:%a' "$CONFIG") == root:root:600 ]] || fail 'off-host backup configuration must be root:root mode 0600'
[[ -d "$BACKUP_ROOT" && ! -L "$BACKUP_ROOT" ]] || fail 'canonical backup root is unavailable'
[[ -f "$VALIDATOR" && ! -L "$VALIDATOR" ]] || fail 'backup validator is unavailable'

declare -A cfg=()
while IFS= read -r raw || [[ -n "$raw" ]]; do
  [[ "$raw" != *$'\r'* ]] || fail 'configuration contains carriage returns'
  [[ -z "$raw" || "$raw" == \#* ]] && continue
  [[ "$raw" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]] || fail 'configuration contains an invalid line'
  key=${BASH_REMATCH[1]}; value=${BASH_REMATCH[2]}
  case "$key" in
    OFFHOST_BACKUP_ENABLED|OCI_NAMESPACE|OCI_BUCKET|OCI_OBJECT_PREFIX|AGE_RECIPIENT) ;;
    *) fail "unsupported configuration key: $key" ;;
  esac
  [[ ! -v "cfg[$key]" ]] || fail "duplicate configuration key: $key"
  cfg[$key]=$value
done <"$CONFIG"
namespace=${cfg[OCI_NAMESPACE]:-}; bucket=${cfg[OCI_BUCKET]:-}; prefix=${cfg[OCI_OBJECT_PREFIX]:-}
[[ "$namespace" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || fail 'OCI_NAMESPACE is invalid'
[[ "$bucket" =~ ^[A-Za-z0-9._-]{1,256}$ ]] || fail 'OCI_BUCKET is invalid'
[[ "$prefix" =~ ^[A-Za-z0-9._/-]{1,128}$ && "$prefix" != /* && "$prefix" != */ && "$prefix" != *..* ]] || fail 'OCI_OBJECT_PREFIX is invalid'
case $(dpkg --print-architecture) in
  arm64) oci_image=$OCI_IMAGE_ARM64 ;;
  amd64) oci_image=$OCI_IMAGE_AMD64 ;;
  *) fail 'OCI CLI image is pinned only for arm64 and amd64' ;;
esac
docker image inspect "$oci_image" >/dev/null 2>&1 || fail 'pinned OCI CLI image is not installed'
[[ ! -e "$BACKUP_ROOT/$stamp" ]] || fail 'that timestamp already exists locally; refusing overwrite'

stage=$(mktemp -d "$BACKUP_ROOT/.restore.XXXXXX")
trap 'rm -rf --one-file-system -- "$stage"' EXIT
object=$prefix/$stamp.tar.age
for suffix in '' .sha256; do
  docker run --rm --pull never --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 100 --memory 512m --cpus 1 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    -e HOME=/tmp -e OCI_CLI_AUTH=instance_principal \
    --mount "type=bind,src=$stage,dst=/recovery" \
    "$oci_image" os object get --auth instance_principal \
    --namespace-name "$namespace" --bucket-name "$bucket" \
    --name "$object$suffix" --file "/recovery/$stamp.tar.age$suffix" >/dev/null
done

expected=$(python3 - "$stage/$stamp.tar.age.sha256" "$stamp.tar.age" <<'PY'
import re, pathlib, sys
line = pathlib.Path(sys.argv[1]).read_text(encoding="ascii").strip()
match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
if not match or match.group(2) != sys.argv[2]:
    raise SystemExit("invalid encrypted-backup checksum sidecar")
print(match.group(1))
PY
)
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || fail 'encrypted-backup checksum is invalid'
actual=$(sha256sum "$stage/$stamp.tar.age" | awk '{print $1}')
[[ "$actual" == "$expected" ]] || fail 'encrypted backup SHA-256 mismatch'
age --decrypt --identity "$identity" --output "$stage/$stamp.tar" "$stage/$stamp.tar.age"

python3 - "$stage/$stamp.tar" "$stamp" <<'PY'
import pathlib, sys, tarfile
archive = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
with tarfile.open(archive, "r:") as bundle:
    members = bundle.getmembers()
    if not members:
        raise SystemExit("decrypted backup archive is empty")
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != expected:
            raise SystemExit(f"unsafe archive path: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise SystemExit(f"link or special archive member rejected: {member.name}")
PY
tar --extract --file "$stage/$stamp.tar" --directory "$BACKUP_ROOT" \
  --no-same-owner --no-same-permissions --keep-old-files
chmod -R u=rwX,go= "$BACKUP_ROOT/$stamp"
"$VALIDATOR" "$BACKUP_ROOT/$stamp" >/dev/null
echo "offhost_restore_stage=PASS backup=$BACKUP_ROOT/$stamp; live state was not modified"

