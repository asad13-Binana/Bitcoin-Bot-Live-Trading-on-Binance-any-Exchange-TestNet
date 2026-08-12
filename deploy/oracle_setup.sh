#!/usr/bin/env bash
# Prepare an Ubuntu Oracle Cloud VM for the immutable Bitcoin Bot artifact.
set -euo pipefail

APP_ROOT=/opt/bitcoin-bot
PERSIST_PARENT=/var/lib/bitcoin-bot
PERSIST=/var/lib/bitcoin-bot/shared
INCOMING=/var/lib/bitcoin-bot/incoming
ROOT_INCOMING=/var/lib/bitcoin-bot/root-incoming
PRIVATE=/etc/bitcoin-bot
APPROVED_DIGEST=$PRIVATE/approved-artifact.sha256
ROOT_LIBEXEC=/usr/local/libexec/bitcoin-bot
ROOT_WRAPPER=/usr/local/sbin/bitcoin-bot-deploy
ACTIONS_RUNNER_USER=gha-runner
MONITOR_LOG_PARENT=/var/log/bitcoin-bot
MONITOR_LOG_DIR=/var/log/bitcoin-bot/monitor
CONFIG_ROOT=/var/lib/bitcoin-bot/config-snapshots
SWAP_FILE=/swapfile-bitcoin-bot
SWAP_MIN_MIB=${SWAP_MIN_MIB:-3800}
REQUIRED_UBUNTU_VERSION=${REQUIRED_UBUNTU_VERSION:-24.04}
REQUIRE_ARM64=${REQUIRE_ARM64:-true}
ENABLE_GITHUB_RUNNER=${ENABLE_GITHUB_RUNNER:-false}
DOCKER_VERSION=${DOCKER_VERSION:-}
CHRONY_MAX_OFFSET_SECONDS=${CHRONY_MAX_OFFSET_SECONDS:-0.5}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

fail(){ echo "ERROR: $*" >&2; exit 1; }
as_root(){
  if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

DEPLOY_USER=${DEPLOY_USER:-${SUDO_USER:-${USER:-}}}
[[ -n "$DEPLOY_USER" && "$DEPLOY_USER" != root ]] || \
  fail 'run as the deployment user with sudo, or set DEPLOY_USER to that non-root account'
id "$DEPLOY_USER" >/dev/null 2>&1 || fail "deployment user does not exist: $DEPLOY_USER"
DEPLOY_GROUP=$(id -gn "$DEPLOY_USER")

# This installer intentionally targets the supported Oracle Ubuntu image.
# It uses Docker's official apt repository and does not guess commands for a
# different distribution.
source /etc/os-release
[[ "${ID:-}" == ubuntu ]] || fail 'oracle_setup.sh supports Ubuntu images only'
[[ "${VERSION_ID:-}" == "$REQUIRED_UBUNTU_VERSION" ]] || \
  fail "expected Ubuntu $REQUIRED_UBUNTU_VERSION, found ${VERSION_ID:-unknown}"
CODENAME=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
[[ -n "$CODENAME" ]] || fail 'cannot determine Ubuntu codename'
HOST_ARCH=$(dpkg --print-architecture)
[[ "$HOST_ARCH" == amd64 || "$HOST_ARCH" == arm64 ]] || \
  fail "unsupported Oracle host architecture: $HOST_ARCH (expected amd64 or arm64)"
[[ "$REQUIRE_ARM64" == true || "$REQUIRE_ARM64" == false ]] || \
  fail 'REQUIRE_ARM64 must be true or false'
[[ "$ENABLE_GITHUB_RUNNER" == true || "$ENABLE_GITHUB_RUNNER" == false ]] || \
  fail 'ENABLE_GITHUB_RUNNER must be true or false'
if [[ "$REQUIRE_ARM64" == true && "$HOST_ARCH" != arm64 ]]; then
  fail "Oracle A1 target requires arm64, found $HOST_ARCH"
fi
PHYSICAL_MEMORY_MIB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
(( PHYSICAL_MEMORY_MIB >= 1400 )) || \
  fail "physical memory ${PHYSICAL_MEMORY_MIB} MiB is below the required 1400 MiB; use Ampere A1 Flex rather than E2.1.Micro"

as_root apt-get update
as_root apt-get install -y \
  ca-certificates curl git gnupg jq chrony iproute2 logrotate openssh-server \
  python3 python3-venv python3-pycryptodome sqlite3 unattended-upgrades
python3 -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)' || \
  fail 'Oracle host Python must be a supported CPython 3.10 through 3.13 (target Ubuntu 24.04)'
# Follow Docker's current official Ubuntu repository format. Conflicting
# distribution packages are removed only before the first Docker CE install;
# existing Docker CE hosts are never destructively reinitialised.
if ! dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -Fqx 'install ok installed'; then
  conflicting=()
  for package in \
    docker.io docker-compose docker-compose-v2 docker-doc docker-buildx \
    podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -Fqx 'install ok installed'; then
      conflicting+=("$package")
    fi
  done
  (( ${#conflicting[@]} == 0 )) || as_root apt-get remove -y "${conflicting[@]}"
fi

as_root install -m 0755 -d /etc/apt/keyrings
DOCKER_KEY_TMP=$(mktemp)
DOCKER_SOURCE_TMP=$(mktemp)
cleanup_docker_repo(){ rm -f -- "$DOCKER_KEY_TMP" "$DOCKER_SOURCE_TMP"; }
trap cleanup_docker_repo EXIT
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$DOCKER_KEY_TMP"
[[ -s "$DOCKER_KEY_TMP" ]] || fail 'Docker repository signing key download is empty'
as_root install -m 0644 -o root -g root "$DOCKER_KEY_TMP" /etc/apt/keyrings/docker.asc
cat > "$DOCKER_SOURCE_TMP" <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $CODENAME
Components: stable
Architectures: $HOST_ARCH
Signed-By: /etc/apt/keyrings/docker.asc
EOF
as_root install -m 0644 -o root -g root "$DOCKER_SOURCE_TMP" \
  /etc/apt/sources.list.d/docker.sources
as_root rm -f /etc/apt/sources.list.d/docker.list
cleanup_docker_repo
trap - EXIT
as_root apt-get update
if [[ -n "$DOCKER_VERSION" ]]; then
  apt-cache madison docker-ce | awk '{print $3}' | grep -Fxq "$DOCKER_VERSION" || \
    fail "requested Docker version is unavailable for $CODENAME/$HOST_ARCH: $DOCKER_VERSION"
  as_root apt-get install -y \
    "docker-ce=$DOCKER_VERSION" "docker-ce-cli=$DOCKER_VERSION" \
    containerd.io docker-buildx-plugin docker-compose-plugin
elif ! dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -Fqx 'install ok installed'; then
  as_root apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo 'Docker CE is already installed; no unreviewed Docker upgrade was performed.'
  echo 'Set DOCKER_VERSION to one reviewed apt-cache madison version for a controlled upgrade.'
fi
as_root systemctl enable --now docker chrony
# The root-owned deployment wrapper is the only Docker privilege boundary.
# The normal deployment account must not retain root-equivalent Docker access.
if id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -Fxq docker; then
  as_root gpasswd -d "$DEPLOY_USER" docker >/dev/null
fi
for forbidden_group in docker lxd disk root; do
  if id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -Fxq "$forbidden_group"; then
    fail "$DEPLOY_USER unexpectedly belongs to privileged group: $forbidden_group"
  fi
done

[[ "$DEPLOY_USER" != "$ACTIONS_RUNNER_USER" ]] || \
  fail "deployment user must be separate from $ACTIONS_RUNNER_USER"
# The isolated account is always retained as an unprivileged artifact-staging
# identity. Installing the GitHub runner application and granting its three
# exact sudo commands remain explicitly opt-in.
if ! id "$ACTIONS_RUNNER_USER" >/dev/null 2>&1; then
  as_root useradd --create-home --shell /bin/bash \
    --comment 'Restricted artifact staging account' "$ACTIONS_RUNNER_USER"
fi
as_root chmod 0700 "/home/$ACTIONS_RUNNER_USER"
for forbidden_group in docker sudo adm lxd disk root; do
  if id -nG "$ACTIONS_RUNNER_USER" | tr ' ' '\n' | grep -Fxq "$forbidden_group"; then
    fail "$ACTIONS_RUNNER_USER unexpectedly belongs to privileged group: $forbidden_group"
  fi
done

# Oracle free-tier hosts benefit from explicit swap headroom during image
# builds/upgrades. Existing swap is respected; a dedicated 4 GiB swap file is
# created only when the host has less than the required total.
swap_mib=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)
if (( swap_mib < SWAP_MIN_MIB )); then
  if [[ -e "$SWAP_FILE" ]]; then
    swap_type=$(as_root blkid -p -s TYPE -o value "$SWAP_FILE" 2>/dev/null || true)
    [[ "$swap_type" == swap ]] || fail "$SWAP_FILE exists but is not a swap filesystem"
    swap_bytes=$(as_root stat -c '%s' "$SWAP_FILE")
    (( swap_bytes >= 4 * 1024 * 1024 * 1024 )) || \
      fail "$SWAP_FILE is smaller than the required 4 GiB"
  else
    as_root fallocate -l 4G "$SWAP_FILE"
    as_root mkswap "$SWAP_FILE" >/dev/null
  fi
  as_root chown root:root "$SWAP_FILE"
  as_root chmod 0600 "$SWAP_FILE"
  if ! awk '{print $1}' /proc/swaps | grep -Fxq "$SWAP_FILE"; then
    as_root swapon "$SWAP_FILE"
  fi
  if ! grep -Eq "^[[:space:]]*${SWAP_FILE//\//\\/}[[:space:]]" /etc/fstab; then
    printf '%s none swap sw 0 0\n' "$SWAP_FILE" | as_root tee -a /etc/fstab >/dev/null
  fi
fi
as_root tee /etc/sysctl.d/99-bitcoin-bot.conf >/dev/null <<'EOF'
vm.swappiness=10
EOF
as_root sysctl --system >/dev/null

swap_mib=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)
(( swap_mib >= SWAP_MIN_MIB )) || fail "swap setup incomplete: ${swap_mib} MiB"
systemctl is-active --quiet chrony || fail 'chrony is not active'
chronyc tracking >/dev/null || fail 'chrony cannot report clock status'
chronyc waitsync 30 "$CHRONY_MAX_OFFSET_SECONDS" >/dev/null || \
  fail "chrony did not synchronise within ${CHRONY_MAX_OFFSET_SECONDS}s"
docker compose version >/dev/null || fail 'Docker Compose plugin is unavailable'
as_root docker version >/dev/null || fail 'Docker Engine is unavailable'
as_root docker info >/dev/null || fail 'Docker daemon information is unavailable'

# Ubuntu security updates remain automatic, but an operating bot is never
# rebooted without an explicit maintenance decision. Docker's third-party
# repository is not added to unattended-upgrades.
as_root tee /etc/apt/apt.conf.d/52bitcoin-bot-unattended-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
as_root systemctl enable --now unattended-upgrades.service >/dev/null

# Bound host-side bot logs without deleting current audit or reconciliation
# files. SQLite and JSONL evidence are protected by their own retention and
# backup mechanisms and are intentionally not listed here.
as_root tee /etc/logrotate.d/bitcoin-bot >/dev/null <<'EOF'
/var/lib/bitcoin-bot/shared/freqtrade/logs/*.log /var/log/bitcoin-bot/monitor/*.log {
    size 10M
    rotate 7
    daily
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
EOF
as_root logrotate --debug /etc/logrotate.d/bitcoin-bot >/dev/null 2>&1 || \
  fail 'bitcoin-bot logrotate policy is invalid'

if ! id botmon >/dev/null 2>&1; then
  as_root useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin botmon
fi
as_root usermod -aG "$DEPLOY_GROUP" botmon
for forbidden_group in docker sudo adm lxd disk root; do
  if id -nG botmon | tr ' ' '\n' | grep -Fxq "$forbidden_group"; then
    fail "botmon unexpectedly belongs to privileged group: $forbidden_group"
  fi
done

PROTECTED_PATHS=(
  "$APP_ROOT"
  "$PERSIST_PARENT"
  "$INCOMING"
  "$ROOT_INCOMING"
  "$PERSIST"
  "$CONFIG_ROOT"
  "$PRIVATE"
  "$MONITOR_LOG_PARENT"
  "$MONITOR_LOG_DIR"
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
for protected in "${PROTECTED_PATHS[@]}"; do
  [[ ! -L "$protected" ]] || fail "refusing symlinked deployment path: $protected"
done

as_root mkdir -p \
  "$APP_ROOT/releases" \
  "$CONFIG_ROOT" \
  "$INCOMING" \
  "$ROOT_INCOMING" \
  "$PERSIST/audit" \
  "$PERSIST/command_results" \
  "$PERSIST/commands/inbox" \
  "$PERSIST/commands/processed" \
  "$PERSIST/freqtrade/logs" \
  "$PERSIST/moneyflow/history" \
  "$PERSIST/pair" \
  "$PERSIST/runtime" \
  "$PERSIST/runtime/moneyflow" \
  "$PERSIST/runtime/sidecar/backtests" \
  "$PERSIST/runtime/telegram" \
  "$PERSIST/signals/inbox" \
  "$PERSIST/signals/processed" \
  "$PERSIST/signals/rejected" \
  "$PRIVATE" "$MONITOR_LOG_DIR"
for protected in "${PROTECTED_PATHS[@]}"; do
  [[ $(readlink -f "$protected" 2>/dev/null || true) == "$protected" ]] || \
    fail "deployment path is not canonical: $protected"
done
as_root chown root:root "$APP_ROOT"
as_root chown root:root "$PERSIST_PARENT"
as_root chown root:"$DEPLOY_GROUP" "$PRIVATE"
as_root chown root:root "$MONITOR_LOG_PARENT"
as_root chmod 0755 "$APP_ROOT" "$PERSIST_PARENT" "$MONITOR_LOG_PARENT"
as_root chown "$ACTIONS_RUNNER_USER:$ACTIONS_RUNNER_USER" "$INCOMING"
as_root chmod 0700 "$INCOMING"
as_root chown root:root "$ROOT_INCOMING"
as_root chmod 0700 "$ROOT_INCOMING"
as_root chmod 0750 "$PRIVATE"
as_root chown -R --one-file-system "$DEPLOY_USER:$DEPLOY_GROUP" \
  "$APP_ROOT/releases" "$PERSIST" "$CONFIG_ROOT"
as_root chmod 0755 "$APP_ROOT/releases"
as_root chmod 0700 "$CONFIG_ROOT"
# Monitoring interpreters are root-managed. Re-running host setup must never
# make them writable by the deployment account.
if [[ -d "$APP_ROOT/monitoring-venvs" ]]; then
  as_root chown -R --one-file-system root:root "$APP_ROOT/monitoring-venvs"
fi
if [[ -L "$APP_ROOT/monitoring-current" ]]; then
  as_root chown -h root:root "$APP_ROOT/monitoring-current"
fi
as_root chmod 0750 \
  "$PERSIST" "$PERSIST/audit" "$PERSIST/command_results" "$PERSIST/commands" \
  "$PERSIST/commands/inbox" "$PERSIST/commands/processed" \
  "$PERSIST/freqtrade" "$PERSIST/freqtrade/logs" \
  "$PERSIST/moneyflow" "$PERSIST/moneyflow/history" \
  "$PERSIST/pair" "$PERSIST/runtime" "$PERSIST/runtime/moneyflow" \
  "$PERSIST/runtime/sidecar" "$PERSIST/runtime/sidecar/backtests" \
  "$PERSIST/runtime/telegram" "$PERSIST/signals" \
  "$PERSIST/signals/inbox" "$PERSIST/signals/processed" "$PERSIST/signals/rejected"
as_root chown botmon:botmon "$MONITOR_LOG_DIR"
as_root chmod 0750 "$MONITOR_LOG_DIR"

if [[ ! -f "$PRIVATE/.env" ]]; then
  as_root install -m 0600 -o root -g root /dev/null "$PRIVATE/.env"
  echo "Created $PRIVATE/.env. Populate it with sudoedit from the release .env.example before deployment."
fi
as_root chown root:root "$PRIVATE/.env"
as_root chmod 0600 "$PRIVATE/.env"

if [[ ! -e "$APPROVED_DIGEST" ]]; then
  as_root install -m 0600 -o root -g root /dev/null "$APPROVED_DIGEST"
fi
[[ ! -L "$APPROVED_DIGEST" ]] || fail "refusing symlinked approval file: $APPROVED_DIGEST"
as_root chown root:root "$APPROVED_DIGEST"
as_root chmod 0600 "$APPROVED_DIGEST"

[[ -f "$SCRIPT_DIR/bitcoin-bot-deploy" && ! -L "$SCRIPT_DIR/bitcoin-bot-deploy" ]] || \
  fail 'root deployment wrapper is missing or a symlink'
[[ -f "$SCRIPT_DIR/install_artifact.sh" && ! -L "$SCRIPT_DIR/install_artifact.sh" ]] || \
  fail 'artifact installer is missing or a symlink'
[[ -f "$SCRIPT_DIR/oracle_validate.sh" && ! -L "$SCRIPT_DIR/oracle_validate.sh" ]] || \
  fail 'Oracle validation diagnostic is missing or a symlink'
[[ -f "$SCRIPT_DIR/resource_guard.sh" && ! -L "$SCRIPT_DIR/resource_guard.sh" ]] || \
  fail 'resource guard is missing or a symlink'
[[ -f "$SCRIPT_DIR/backup_state.sh" && ! -L "$SCRIPT_DIR/backup_state.sh" \
   && -f "$SCRIPT_DIR/verify_backup.sh" && ! -L "$SCRIPT_DIR/verify_backup.sh" ]] || \
  fail 'backup or restore-validation tool is missing or a symlink'
[[ -f "$SCRIPT_DIR/systemd/bitcoin-bot-resource-guard.service" \
   && -f "$SCRIPT_DIR/systemd/bitcoin-bot-resource-guard.timer" ]] || \
  fail 'resource guard systemd units are missing'
as_root install -m 0755 -o root -g root -d "$ROOT_LIBEXEC"
as_root install -m 0755 -o root -g root \
  "$SCRIPT_DIR/install_artifact.sh" "$ROOT_LIBEXEC/install_artifact.sh"
as_root install -m 0755 -o root -g root \
  "$SCRIPT_DIR/resource_guard.sh" "$ROOT_LIBEXEC/resource_guard.sh"
as_root install -m 0755 -o root -g root \
  "$SCRIPT_DIR/backup_state.sh" "$ROOT_LIBEXEC/backup_state.sh"
as_root install -m 0755 -o root -g root \
  "$SCRIPT_DIR/verify_backup.sh" "$ROOT_LIBEXEC/verify_backup.sh"
as_root install -m 0755 -o root -g root \
  "$SCRIPT_DIR/bitcoin-bot-deploy" "$ROOT_WRAPPER"
as_root install -m 0755 -o root -g root \
  "$SCRIPT_DIR/oracle_validate.sh" /usr/local/sbin/bitcoin-bot-oracle-validate
as_root install -m 0644 -o root -g root \
  "$SCRIPT_DIR/systemd/bitcoin-bot-resource-guard.service" \
  "$SCRIPT_DIR/systemd/bitcoin-bot-resource-guard.timer" \
  /etc/systemd/system/
as_root systemctl daemon-reload
as_root systemctl enable --now bitcoin-bot-resource-guard.timer >/dev/null
as_root install -m 0700 -o root -g root -d /var/backups/bitcoin-bot
BACKUP_LOCK_FILE=/var/lock/bitcoin-bot.backup.lock
as_root touch "$BACKUP_LOCK_FILE"
as_root chown root:root "$BACKUP_LOCK_FILE"
as_root chmod 0600 "$BACKUP_LOCK_FILE"

SUDOERS_TMP=$(mktemp)
cleanup_sudoers(){ rm -f -- "$SUDOERS_TMP"; }
trap cleanup_sudoers EXIT
if [[ "$ENABLE_GITHUB_RUNNER" == true ]]; then
cat > "$SUDOERS_TMP" <<'EOF'
gha-runner ALL=(root) NOPASSWD: /usr/local/sbin/bitcoin-bot-deploy preflight
gha-runner ALL=(root) NOPASSWD: /usr/local/sbin/bitcoin-bot-deploy simulation
gha-runner ALL=(root) NOPASSWD: /usr/local/sbin/bitcoin-bot-deploy verify
EOF
  chmod 0600 "$SUDOERS_TMP"
  as_root visudo -cf "$SUDOERS_TMP"
  as_root install -m 0440 -o root -g root "$SUDOERS_TMP" \
    /etc/sudoers.d/bitcoin-bot-actions
else
  as_root rm -f /etc/sudoers.d/bitcoin-bot-actions
fi
cleanup_sudoers
trap - EXIT

LOCK_FILE=/var/lock/bitcoin-bot.install.lock
as_root touch "$LOCK_FILE"
as_root chown root:root "$LOCK_FILE"
as_root chmod 0600 "$LOCK_FILE"

ACTIONS_LOCK_FILE=/var/lock/bitcoin-bot.actions-deploy.lock
as_root touch "$ACTIONS_LOCK_FILE"
as_root chown root:root "$ACTIONS_LOCK_FILE"
as_root chmod 0600 "$ACTIONS_LOCK_FILE"

echo "Oracle host prepared for $DEPLOY_USER without Docker-group membership."
if [[ "$ENABLE_GITHUB_RUNNER" == true ]]; then
  echo "Optional runner enabled: register $ACTIONS_RUNNER_USER with label oracle-sim."
  echo "$ACTIONS_RUNNER_USER has no Docker group and only three exact root-wrapper commands."
else
  echo 'GitHub self-hosted runner disabled by default; use manual immutable-artifact transfer.'
fi
echo 'Keep execution mode at simulation first. Binance keys must have Spot permission only, withdrawals disabled, and Oracle-IP restriction enabled.'
echo 'Monitoring receives no trading credentials and no Docker-socket access.'
