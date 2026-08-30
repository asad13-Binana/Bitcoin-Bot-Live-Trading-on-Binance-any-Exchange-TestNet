#!/usr/bin/env bash
# Strict verification — exits NONZERO unless everything passes:
#  1) image pulls, 2) config validates & strategy loads, 3) strategy is listed,
#  4) the actual pinned RemotePairList loads and refreshes the mounted pair file.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(cd .. && pwd)"
COMPOSE=(docker compose --profile offline-audit)
"${COMPOSE[@]}" pull
echo ">>> Validating config + strategy load (show-config)..."
"${COMPOSE[@]}" run --rm \
  -v "$REPO_ROOT/shared:/freqtrade/shared:ro" \
  -v "$REPO_ROOT/services:/freqtrade/services:ro" \
  freqtrade show-config \
  --config /freqtrade/user_data/config.json > /dev/null
echo ">>> Checking IctSmcStrategy is discoverable..."
"${COMPOSE[@]}" run --rm \
  -v "$REPO_ROOT/shared:/freqtrade/shared:ro" \
  -v "$REPO_ROOT/services:/freqtrade/services:ro" \
  freqtrade list-strategies \
  --config /freqtrade/user_data/config.json | grep "IctSmcStrategy" | grep -q " OK "
echo ">>> Exercising real RemotePairList startup/refresh without network..."
mapfile -t IMAGES < <("${COMPOSE[@]}" config --images)
[[ ${#IMAGES[@]} == 1 && "${IMAGES[0]}" == freqtradeorg/freqtrade:*@sha256:* ]] || {
  echo 'expected exactly one digest-pinned Freqtrade image' >&2
  exit 1
}
# No application services, persistent state, credentials or network are used.
# show-config/list-strategies alone never exercise RemotePairList.gen_pairlist.
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -v "$REPO_ROOT/freqtrade/user_data/config.json:/freqtrade/user_data/config.json:ro" \
  -v "$REPO_ROOT/shared/pair:/freqtrade/shared/pair:ro" \
  -v "$REPO_ROOT/freqtrade/tests/remote_pairlist_probe.py:/freqtrade/remote_pairlist_probe.py:ro" \
  --entrypoint python "${IMAGES[0]}" /freqtrade/remote_pairlist_probe.py
echo "VERIFY: ALL CHECKS PASSED"
