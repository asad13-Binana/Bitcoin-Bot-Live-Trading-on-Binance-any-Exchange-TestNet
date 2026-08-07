#!/usr/bin/env bash
# THE ENFORCED GATE. Runs the backtest with protections + fees, then PARSES the
# result and FAILS (nonzero exit) unless minimum acceptance criteria are met:
#   profit_factor > 1.15  AND  trades >= 100  AND  profit_total > 0
#   AND max_drawdown_account < 20% (missing drawdown = fail-closed)
# Passing here is NECESSARY, not sufficient: re-run on a later out-of-sample
# timerange you did not tune on, then complete the simulation/Testnet/Oracle soak.
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
OFFLINE_SIGNAL_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
RELEASE_HASH="$(awk 'NF{print $1;exit}' ../RELEASE_SHA256.txt)"
OFFLINE_SHARED="$(mktemp -d)"
trap 'rm -rf -- "$OFFLINE_SHARED"' EXIT
mkdir -p "$OFFLINE_SHARED/pair"
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
    'schema_version': 1,
    'pair': pair,
    'symbol': pair.replace('/', ''),
    'base': 'BTC',
    'quote': pair.split('/', 1)[1],
    'generation': 1,
    'source': 'offline-backtest',
    'updated_at': datetime.now(timezone.utc).isoformat(),
    'pair_config_hash': pair_config_hash(pair),
}
state['state_hash'] = pair_state_hash(state)
(root / 'active_pair.json').write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
(root / 'backtest-config.json').write_text(json.dumps({
    'strategy': 'IctSmcStrategy',
    'timeframe': '1m',
    'trading_mode': 'spot',
    'max_open_trades': 1,
    'enable_protections': True,
    'fee': 0.001,
    'stake_currency': pair.split('/', 1)[1],
    'exchange': {'pair_whitelist': [pair], 'pair_blacklist': []},
    'pairlists': [{'method': 'StaticPairList'}],
}, indent=2, sort_keys=True) + '\n', encoding='utf-8')
PY
"${COMPOSE[@]}" run --rm \
  -v "$OFFLINE_SHARED:/freqtrade/shared:ro" \
  -v "$REPO_ROOT/services:/freqtrade/services:ro" \
  -e FREQTRADE__PAIRLISTS='[{"method": "StaticPairList"}]' \
  -e "FREQTRADE__STAKE_CURRENCY=$QUOTE" \
  -e ACTIVE_PAIR_FILE=/freqtrade/shared/pair/active_pair.json \
  -e SIGNAL_INBOX=/tmp/offline-signals \
  -e SIGNAL_PROCESSED=/tmp/offline-processed \
  -e SIGNAL_REJECTED=/tmp/offline-rejected \
  -e SIGNAL_HEARTBEAT=/tmp/offline-heartbeat.json \
  -e "SIGNAL_HMAC_KEY=$OFFLINE_SIGNAL_KEY" \
  -e "ENVELOPE_RELEASE_HASH=$RELEASE_HASH" \
  freqtrade backtesting \
  --config /freqtrade/user_data/config.json \
  --config /freqtrade/shared/pair/backtest-config.json \
  --strategy IctSmcStrategy \
  --timeframe 1m --timerange "$TIMERANGE" --fee 0.001 --pairs "$PAIR" \
  --enable-protections --export trades

python3 - << 'PYGATE'
import json, sys, zipfile, pathlib
res_dir = pathlib.Path("user_data/backtest_results")
try:
    latest = json.loads((res_dir / ".last_result.json").read_text())["latest_backtest"]
    p = res_dir / latest
    if p.suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            candidates = []
            for name in z.namelist():
                if not name.endswith(".json") or name.endswith("_config.json"):
                    continue
                try:
                    value = json.loads(z.read(name))
                except Exception:
                    continue
                if isinstance(value, dict) and "IctSmcStrategy" in (value.get("strategy") or {}):
                    candidates.append(value)
            if len(candidates) != 1:
                raise ValueError(f"expected one IctSmcStrategy result, found {len(candidates)}")
            data = candidates[0]
    else:
        data = json.loads(p.read_text())
    stats = data["strategy"]["IctSmcStrategy"]
    pf = stats.get("profit_factor")
    trades = stats.get("total_trades", 0)
    profit = stats.get("profit_total", 0)
    dd = stats.get("max_drawdown_account", None)
    print("\n================ BACKTEST GATE ================")
    print(f"  trades         : {trades}")
    print(f"  profit_total   : {profit:.4%}" if isinstance(profit,(int,float)) else f"  profit_total   : {profit}")
    print(f"  profit_factor  : {pf}")
    print(f"  max_dd_account : {dd}")
    dd_ok = isinstance(dd,(int,float)) and dd < 0.20
    if dd is None:
        print("  NOTE: max_drawdown_account MISSING -> fail-closed")
    ok = (pf is not None and pf > 1.15) and trades >= 100 and (profit or 0) > 0 and dd_ok
    print(f"  VERDICT        : {'PASS — proceed to OUT-OF-SAMPLE retest, then dry-run' if ok else 'FAIL — DO NOT GO LIVE'}")
    print("===============================================\n")
    sys.exit(0 if ok else 1)
except SystemExit:
    raise
except Exception as e:
    print(f"\nBACKTEST GATE: could not parse results ({e}) — FAIL-CLOSED.\n")
    sys.exit(1)
PYGATE
rm -rf -- "$OFFLINE_SHARED"
trap - EXIT
