#!/usr/bin/env bash
# Pull the digest-pinned OCI CLI image, prove IAM/bucket access, then enable the timer.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'ERROR: configure_offhost_backup.sh must run as root' >&2; exit 1; }
SCRIPT=/usr/local/libexec/bitcoin-bot/offhost_backup.sh
[[ -f "$SCRIPT" && ! -L "$SCRIPT" ]] || { echo 'ERROR: installed off-host backup script is unavailable' >&2; exit 1; }
case $(dpkg --print-architecture) in
  arm64) image='ghcr.io/oracle/oci-cli:sha-45aa4a4@sha256:efaeca93e2adc0411151bcde39a9c945bc6245cbf8d3117fa7c526653492eb19' ;;
  amd64) image='ghcr.io/oracle/oci-cli:sha-45aa4a4@sha256:e781329f06b345e1322260a5594d365a088dac77cef2b0bb394a5acf40804cea' ;;
  *) echo 'ERROR: unsupported architecture' >&2; exit 1 ;;
esac
docker pull "$image"
docker image inspect "$image" >/dev/null
"$SCRIPT" --preflight
systemctl daemon-reload
systemctl enable --now bitcoin-bot-offhost-backup.timer
echo 'off-host backup enabled; run the service once now and verify its status before relying on it'


