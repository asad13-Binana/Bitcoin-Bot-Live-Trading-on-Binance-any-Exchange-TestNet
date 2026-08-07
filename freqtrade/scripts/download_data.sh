#!/usr/bin/env bash
# Download every timeframe consumed by the strategy/money-flow context.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(cd .. && pwd)"
TIMERANGE="${1:-20250101-}"
PAIR="${2:-${ACTIVE_PAIR:-}}"
if [[ -z "$PAIR" ]]; then
  PAIR="$(python3 -c "import json; print(json.load(open('../shared/pair/active_pair.json', encoding='utf-8'))['pair'])")"
fi
PAIR="$(PYTHONPATH=.. python3 -c 'import sys; from services.common.market_policy import canonical_pair; print(canonical_pair(sys.argv[1]))' "$PAIR")"
QUOTE="${PAIR#BTC/}"
COMPOSE=(docker compose --profile offline-audit)
"${COMPOSE[@]}" run --rm \
  -v "$REPO_ROOT/shared:/freqtrade/shared:ro" \
  -e FREQTRADE__PAIRLISTS='[{"method": "StaticPairList"}]' \
  -e "FREQTRADE__STAKE_CURRENCY=$QUOTE" \
  freqtrade download-data \
  --config /freqtrade/user_data/config.json \
  --timeframes 1m 5m 15m 1h 2h 4h 1d --timerange "$TIMERANGE" --pairs "$PAIR"
