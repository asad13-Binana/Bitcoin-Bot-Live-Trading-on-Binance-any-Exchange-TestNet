# Bitcoin Bot Backtest Review — 29 July 2026

## Verdict

**BLOCKER — the exact packaged strategy fails the mandatory profitability and
drawdown gate. It must not be promoted to real-money trading.**

The infrastructure package remains simulation-first. The testnet package may
be used for exchange-lifecycle and deployment drills, but this result does not
approve the strategy for live capital.

## Canonical run

- Engine: Freqtrade 2026.6
- CCXT: 4.5.70
- Market: Binance Spot, BTC/USDT
- Strategy source: `freqtrade/user_data/strategies/IctSmcStrategy.py`
- Main timeframe: 1 minute
- Informative timeframe: 5 minutes
- Period: 1 January 2025 03:30 UTC to 29 July 2026 14:40 UTC
- Usable one-minute candles: 827,230
- Starting balance: 1,000 USDT
- Stake: 100 USDT
- Maximum open trades: 1
- Fee: 0.1% as configured in Freqtrade
- Protections: enabled

| Metric | Result | Required gate | Verdict |
|---|---:|---:|---|
| Total trades | 2,349 | at least 100 | PASS |
| Total profit | -49.10% (-490.964 USDT) | above 0% | FAIL |
| Profit factor | 0.30 | above 1.15 | FAIL |
| Maximum account drawdown | 49.11% | below 20% | FAIL |
| Win rate | 30.9% | informational | — |

The `lost_vwap_5m_bear` exit closed 1,479 trades, all at a loss, for
-701.114 USDT. ROI and trailing exits were profitable in aggregate but did
not offset those losses.

## Narrow diagnostic variant

The same entry formula, fee, stops, ROI, trailing logic, and protections were
tested with ordinary exit signals disabled. This was a diagnostic only and was
not adopted:

- 1,431 trades
- -33.99% total profit
- 0.32 profit factor
- 34.11% maximum drawdown

The variant also failed the mandatory gate. Its 229 hard-stop exits contributed
-501.185 USDT, so removing one exit path does not establish an edge.

## Exact unchanged-strategy repeat — 30 July 2026

The canonical run was repeated after the deployment-only and read-only Telegram
self-audit patch. The protected strategy was not edited.

- New raw Freqtrade result ZIP SHA-256:
  `5c068ab2f614853788c51976c7512d6a4321c5a02936d4a4d3e42472bbd91d49`
- Embedded, live-package and testnet-package strategy SHA-256:
  `023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340`
- Result ZIP CRC: passed
- Canonical trade-list SHA-256:
  `1b57fda8be10717cfddeaf87d2ceeff188bd34bc4e6ad6f5d21d19e03d92d415`
- Trade list compared with the 29 July canonical run: byte-equivalent after
  canonical JSON serialization
- Metrics: exactly unchanged at 2,349 trades, -49.10%, profit factor 0.3019244430,
  maximum account drawdown 49.11386260%, 727 wins and 1,622 losses

This is deterministic evidence that the deployment and Telegram additions did
not change backtest behavior. It also reconfirms the live prohibition.

## Correctness defects fixed during the run

1. The offline backtest, look-ahead, and recursive-analysis helpers omitted the
   required `pair_config_hash` from their generated active-pair state.
2. The runtime signal hook attempted `int(NaN)` on historical no-signal
   candles and rewrote runtime heartbeat state during offline analysis.

All three helpers now generate a complete active-pair state. Backtest,
hyperopt, and edge modes now skip runtime envelope and heartbeat work. The
protected indicator, entry, and exit formula methods were not changed.

## External evidence boundary

- The Binance connector returned current read-only Spot rules for 14 trading
  BTC-base symbols; all advertised OCO, OTO, trailing-stop support and the
  expected price, lot, notional, and trailing-delta filters.
- The connector also returned complete, ordered 1m and 5m BTC/USDT samples.
- No authenticated Binance request or order was made.
- The GitHub connector exposed no installed account or repository, so no remote
  workflow, branch, commit, runner or Actions result was available.
- The local Docker client was present, but its Linux engine was unavailable.
  The backtest used an isolated local Freqtrade 2026.6 runtime instead.

Look-ahead and recursive analyses are not promotion evidence while the primary
profitability gate is already failed. Redesign the strategy, freeze the new
rules before evaluation, then run training, untouched out-of-sample,
look-ahead, recursive, testnet, and Oracle soak gates.
