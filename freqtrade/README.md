# Freqtrade signal and backtest component

This directory contains the preserved `IctSmcStrategy` signal formula and the
supported offline data, backtest, lookahead-analysis, and recursive-analysis
helpers. The root Compose deployment also runs this strategy against closed
candles to publish authenticated signal envelopes.

Freqtrade is deliberately **not** an order owner in this architecture:

- `confirm_trade_entry()` permits `BACKTEST` and `HYPEROPT` so official
  Freqtrade analysis produces real trade results.
- It denies `LIVE` and `DRY_RUN`; in simulation, Testnet, and live deployments,
  only `services/execution_sidecar` may submit Binance Spot orders.
- The root `docker-compose.yml`, root `.env.example`, `deploy/`, and
  `docs/GITHUB_ORACLE_DEPLOYMENT.md` form the supported deployment path.
- `freqtrade/docker-compose.yml` is an offline-analysis profile, not a second
  production stack.

Never copy private exchange or Telegram credentials into this directory.

## Offline analysis helpers

Download historical candles first, then run the official diagnostics for the
same exact pair and timerange used by the strategy review:

```bash
bash scripts/download_data.sh 20240101-20260101 BTC/USDT
bash scripts/lookahead.sh 20240101-20260101 BTC/USDT
bash scripts/recursive.sh 20240101-20260101 BTC/USDT
```

Both diagnostic scripts canonicalize the requested BTC/stable pair, replace the
three-pair base whitelist with that one pair, force dry-run/no-auth configuration,
and disable Telegram and the API server. They run only the Freqtrade analysis
commands; they do not run `trade` and receive no Binance key or secret. Complete
plain-text output and the lookahead CSV are retained under
`user_data/backtest_results/`.

An analysis that has too few triggered signals is inconclusive. The lookahead
helper intentionally does not enable limit orders because Freqtrade warns that
doing so can create false positives. Use at least 5,000 1m candles for the
recursive benchmark and review indicator variance rather than treating the command
exit status alone as approval for live trading.

Command semantics and result interpretation follow the official
[lookahead-analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/) and
[recursive-analysis](https://www.freqtrade.io/en/stable/recursive-analysis/)
documentation.
