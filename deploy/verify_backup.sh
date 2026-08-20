#!/usr/bin/env bash
# Validate a Bitcoin Bot backup without restoring or printing its contents.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo 'ERROR: verify_backup.sh must run as root' >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
BACKUP=${1:?usage: verify_backup.sh $BACKUP_ROOT/YYYYMMDDTHHMMSSZ}
[[ -d "$BACKUP" && ! -L "$BACKUP" ]] || {
  echo 'ERROR: backup is missing or a symlink' >&2; exit 1; }
resolved=$(readlink -f "$BACKUP")
[[ $(dirname "$resolved") == "$BACKUP_ROOT" \
   && $(basename "$resolved") =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || {
  echo 'ERROR: backup must be a direct timestamp child of the canonical backup root' >&2
  exit 1
}
[[ -f "$resolved/SHA256SUMS" && ! -L "$resolved/SHA256SUMS" ]] || {
  echo 'ERROR: backup checksum inventory is missing' >&2; exit 1; }
(
  cd "$resolved"
  sha256sum -c SHA256SUMS >/dev/null
)
for database in "$resolved"/sqlite/*; do
  [[ -e "$database" ]] || continue
  [[ -f "$database" && ! -L "$database" ]] || {
    echo 'ERROR: unsafe SQLite backup member' >&2; exit 1; }
  [[ $(sqlite3 "$database" 'PRAGMA quick_check;') == ok ]] || {
    echo 'ERROR: SQLite backup quick_check failed' >&2; exit 1; }
done
for archive in "$resolved"/*.tar.gz; do
  [[ -f "$archive" && ! -L "$archive" ]] || {
    echo 'ERROR: unsafe backup archive member' >&2; exit 1; }
  tar -tzf "$archive" | python3 -c '
import pathlib, sys
for raw in sys.stdin:
    name = raw.rstrip("\n")
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise SystemExit("unsafe archive path")
'
done
printf 'Bitcoin Bot backup integrity verified: %s\n' "$resolved"
printf 'Restore remains an explicit maintenance-window operation; no data was changed.\n'
