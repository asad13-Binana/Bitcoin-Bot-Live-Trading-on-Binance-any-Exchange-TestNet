#!/usr/bin/env bash
# Offline-only lookahead-bias analysis for the exact configured BTC/stable pair.
# This one-off container receives no exchange credentials and cannot own orders.
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
PAIR_TAG="${PAIR//\//-}"
RUN_ID="$(date -u +'%Y%m%dT%H%M%SZ')-$$"
OUTPUT_DIR="$PWD/user_data/backtest_results"
OUTPUT_LOG="$OUTPUT_DIR/lookahead-analysis-${PAIR_TAG}-${RUN_ID}.log"
OUTPUT_CSV="/freqtrade/user_data/backtest_results/lookahead-analysis-${PAIR_TAG}-${RUN_ID}.csv"
COMPOSE=(docker compose --profile offline-audit)
OFFLINE_SIGNAL_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
RELEASE_HASH="$(awk 'NF{print $1;exit}' ../RELEASE_SHA256.txt)"
OFFLINE_SHARED="$(mktemp -d)"
trap 'rm -rf -- "$OFFLINE_SHARED"' EXIT
mkdir -p "$OFFLINE_SHARED/pair" "$OUTPUT_DIR"

PYTHONPATH=.. python3 - "$PAIR" "$OFFLINE_SHARED/pair" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from services.common.market_policy import (
    canonical_pair,
    pair_config_hash,
    pair_state_hash,
)

pair = canonical_pair(sys.argv[1])
root = Path(sys.argv[2])
state = {
    "schema_version": 1,
    "pair": pair,
    "symbol": pair.replace("/", ""),
    "base": "BTC",
    "quote": pair.split("/", 1)[1],
    "generation": 1,
    "source": "offline-lookahead-analysis",
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "pair_config_hash": pair_config_hash(pair),
}
state["state_hash"] = pair_state_hash(state)
(root / "active_pair.json").write_text(
    json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(root / "analysis-config.json").write_text(
    json.dumps(
        {
            "strategy": "IctSmcStrategy",
            "timeframe": "1m",
            "dry_run": True,
            "trading_mode": "spot",
            "max_open_trades": 1,
            "stake_currency": pair.split("/", 1)[1],
            "exchange": {
                "name": "binance",
                "key": "",
                "secret": "",
                "ccxt_config": {},
                "ccxt_async_config": {},
                "pair_whitelist": [pair],
                "pair_blacklist": [],
            },
            "pairlists": [{"method": "StaticPairList"}],
            "telegram": {"enabled": False},
            "api_server": {"enabled": False},
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

# Freqtrade deliberately forces market orders, protections off, no cache, and a
# large dry-run wallet for this diagnostic. Do not add --allow-limit-orders.
"${COMPOSE[@]}" run --rm --no-deps --cap-drop ALL \
  -v "$OFFLINE_SHARED:/freqtrade/shared:ro" \
  -v "$REPO_ROOT/services:/freqtrade/services:ro" \
  -e FREQTRADE__EXCHANGE__KEY= \
  -e FREQTRADE__EXCHANGE__SECRET= \
  -e FREQTRADE__TELEGRAM__ENABLED=false \
  -e FREQTRADE__API_SERVER__ENABLED=false \
  -e FREQTRADE__DRY_RUN=true \
  -e 'FREQTRADE__PAIRLISTS=[{"method": "StaticPairList"}]' \
  -e "FREQTRADE__STAKE_CURRENCY=$QUOTE" \
  -e ACTIVE_PAIR_FILE=/freqtrade/shared/pair/active_pair.json \
  -e SIGNAL_INBOX=/tmp/offline-signals \
  -e SIGNAL_PROCESSED=/tmp/offline-processed \
  -e SIGNAL_REJECTED=/tmp/offline-rejected \
  -e SIGNAL_HEARTBEAT=/tmp/offline-heartbeat.json \
  -e "SIGNAL_HMAC_KEY=$OFFLINE_SIGNAL_KEY" \
  -e "ENVELOPE_RELEASE_HASH=$RELEASE_HASH" \
  freqtrade lookahead-analysis \
  --no-color \
  --config /freqtrade/user_data/config.json \
  --config /freqtrade/shared/pair/analysis-config.json \
  --strategy IctSmcStrategy \
  --timeframe 1m \
  --timerange "$TIMERANGE" \
  --fee 0.001 \
  --pairs "$PAIR" \
  --minimum-trade-amount 20 \
  --targeted-trade-amount 20 \
  --export none \
  --lookahead-analysis-exportfilename "$OUTPUT_CSV" 2>&1 | tee "$OUTPUT_LOG"

echo "Lookahead artifacts retained in: $OUTPUT_DIR"
