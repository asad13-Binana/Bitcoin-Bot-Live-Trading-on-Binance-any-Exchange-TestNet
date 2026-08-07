#!/usr/bin/env bash
# Strict verification — exits NONZERO unless everything passes:
#  1) image pulls, 2) config validates & strategy loads, 3) strategy is listed.
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
echo "VERIFY: ALL CHECKS PASSED"
