#!/usr/bin/env bash
# Root-only, locally retained backup with online SQLite copies and checksums.
set -Eeuo pipefail
umask 077

[[ $EUID -eq 0 ]] || { echo 'ERROR: backup_state.sh must run as root' >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
SHARED=$PERSIST
LOCK_FILE=$BACKUP_LOCK
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=$BACKUP_ROOT/$STAMP

[[ -d "$BACKUP_ROOT" && ! -L "$BACKUP_ROOT" ]] || {
  echo 'ERROR: canonical backup root is missing or a symlink' >&2; exit 1; }
[[ $(readlink -f "$BACKUP_ROOT") == "$BACKUP_ROOT" ]] || {
  echo 'ERROR: backup root is not canonical' >&2; exit 1; }
TMP=$(mktemp -d "$BACKUP_ROOT/.${STAMP}.XXXXXX")
cleanup(){ rm -rf --one-file-system "$TMP"; }
trap cleanup EXIT
python3 -I "$SCRIPT_DIR/prepare_runtime_locks.py"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo 'ERROR: another Bitcoin backup is running' >&2; exit 1; }
[[ ! -e "$DEST" ]] || { echo 'ERROR: timestamp backup already exists' >&2; exit 1; }

mkdir -p "$TMP/sqlite"
databases=(
  "$SHARED/runtime/sidecar/execution_state.sqlite"
  "$SHARED/runtime/sidecar/signal_state.sqlite"
  "$SHARED/runtime/moneyflow/external_market_quota.sqlite3"
  "$SHARED/runtime/telegram/telegram_updates.sqlite3"
  "$SHARED/freqtrade/tradesv3.signal-only.sqlite"
)
for database in "${databases[@]}"; do
  [[ -e "$database" ]] || continue
  [[ -f "$database" && ! -L "$database" ]] || {
    echo "ERROR: database is not a regular file: $database" >&2; exit 1; }
  name=$(basename "$database")
  target=$TMP/sqlite/$name
  sqlite3 "$database" ".timeout 30000" ".backup '$target'"
  # An online backup of a WAL-mode database must become a standalone copy.
  [[ $(sqlite3 "$target" 'PRAGMA journal_mode=DELETE;') == delete ]] || {
    echo "ERROR: cannot finalise standalone SQLite backup: $name" >&2; exit 1; }
  [[ $(sqlite3 "$target" 'PRAGMA quick_check;') == ok ]] || {
    echo "ERROR: SQLite backup validation failed: $name" >&2; exit 1; }
  chmod 0600 "$target"
done

# These archives can contain operational configuration and therefore remain
# root-only. External copies must be encrypted by the operator before leaving
# the Oracle host; no archive content is printed by this tool.
for source in "$CONFIG_ROOT" "$SHARED/audit" "$SHARED/runtime"; do
  [[ -d "$source" && ! -L "$source" ]] || continue
  if [[ -n $(find "$source" -xdev -type l -print -quit) ]]; then
    echo "ERROR: refusing symlinked backup content under $source" >&2
    exit 1
  fi
done
tar --numeric-owner --one-file-system -C "$PERSIST_PARENT" -czf "$TMP/config-snapshots.tar.gz" \
  config-snapshots
tar --numeric-owner --one-file-system -C "$SHARED" -czf "$TMP/audit-evidence.tar.gz" audit
# Raw WAL/SHM/live database copies are not safe restore inputs. The online
# standalone copies above are authoritative; this archive is metadata only.
tar --numeric-owner --one-file-system --exclude='*.sqlite*' \
  -C "$SHARED" -czf "$TMP/deployment-metadata.tar.gz" runtime
chmod 0600 "$TMP"/*.tar.gz

(
  cd "$TMP"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)
chmod 0600 "$TMP/SHA256SUMS"
mv -T "$TMP" "$DEST"
trap - EXIT
printf 'Bitcoin Bot backup created and verified: %s\n' "$DEST"
printf 'Keep it root-only; encrypt before any off-host transfer.\n'
