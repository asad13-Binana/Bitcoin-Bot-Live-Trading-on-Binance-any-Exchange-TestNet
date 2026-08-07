# Live promotion checklist

Live is intentionally blocked until every item is supported by retained evidence.
A passing backtest is not a guarantee of profit.

## Exact-strategy analysis

- Verify the release manifest and strategy fingerprints.
- Download the chosen BTC/stable pair data for 1m, 5m, 15m, 1h, 2h, 4h, and 1d:

  ```bash
  bash freqtrade/scripts/download_data.sh 20240101- BTC/USDT
  ```

- Run multiple in-sample and untouched out-of-sample periods through the enforced
  backtest gate:

  ```bash
  bash freqtrade/scripts/backtest.sh 20240101-20250101 BTC/USDT
  bash freqtrade/scripts/backtest.sh 20250101-20260101 BTC/USDT
  ```

  The official result ZIP must contain exactly one `IctSmcStrategy` result, the full
  strategy source and resolved config. Gates are at least 100 trades, profit factor
  above 1.15, positive total profit, account drawdown below 20%, protections enabled,
  fee at least 0.001 per side, Spot mode, 1m, one exact pair, and one open trade.

- Run official Freqtrade `lookahead-analysis` and `recursive-analysis` on the exact
  strategy/pair and retain their complete outputs. Treat insufficient triggered
  signals as inconclusive, not a pass. Do not use `--allow-limit-orders` to hide
  results. Use a long timerange (Freqtrade recommends at least 5000 candles for the
  recursive benchmark).

  ```bash
  bash freqtrade/scripts/lookahead.sh 20240101-20260101 BTC/USDT
  bash freqtrade/scripts/recursive.sh 20240101-20260101 BTC/USDT
  ```

  The helpers use an exact one-pair, Spot, dry-run configuration with empty exchange
  credentials and retain logs (plus the lookahead CSV) beneath
  `freqtrade/user_data/backtest_results/`. Review and archive those artifacts; a
  successful process exit is not itself a promotion verdict.

## Testnet and Oracle evidence

- Complete Binance Spot Testnet entry, full fill, partial fill, OTOCO/OCO activation,
  trailing conversion, break-even, profit lock, cancel, reject, timeout ambiguity,
  user-stream disconnect/reconnect, restart, and pair-switch drills.
- Prove no duplicate order follows an ambiguous submission/cancel and that entries
  remain paused until reconciliation.
- Complete three clean release/rollback passes.
- Run a fourteen-day Oracle simulation/Testnet soak with clock sync, disk, memory,
  Telegram, monitoring, log redaction, and restart evidence retained.
- Verify the API key has Spot trading only, withdrawals disabled, and an Oracle IP
  restriction. Confirm there are no margin/futures/transfer permissions.

## Offline signature

Generate an Ed25519 keypair on an offline machine; never copy the private key to
Oracle, GitHub, a release archive, or a container:

```bash
python scripts/certify_live_evidence.py keygen \
  --private-key offline/live-ed25519.pem --public-env offline/public.env
python scripts/certify_live_evidence.py template \
  --output offline/assertions.json
```

Set an assertion to true only when its retained evidence exists. Export the exact
Oracle `active_pair.json`, exact official Freqtrade result ZIP, and non-secret risk
fields. Then certify into a staging directory:

```bash
python scripts/certify_live_evidence.py certify \
  --private-key offline/live-ed25519.pem \
  --release-root . \
  --active-pair evidence/active_pair.json \
  --backtest evidence/freqtrade-result.zip \
  --assertions offline/assertions.json \
  --risk-env evidence/oracle-risk.env \
  --output-dir evidence/staged-sidecar \
  --valid-days 30
```

Copy only the public key line, `LIVE_EVIDENCE.<release-hash>.json`, and its
content-addressed backtest into the corresponding Oracle persistent paths. The
installer rechecks exact release, strategy bytes/fingerprints, pair generation, risk
policy, backtest bytes/config/metrics, assertions, signature, expiry, and a fixed
validity margin. Only after every gate closes may a separately reviewed protected
`oracle-production` workflow be created; no such live dispatch ships in this release.
Keep `AUTO_CONFIRM=false`, and entries must still start off.
