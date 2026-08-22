# Bitcoin Spot Bot — mode-separated Oracle/GitHub release

For credential setup and the safe GET-only Oracle API preflight, see
[`docs/API_READINESS_RUNBOOK.md`](docs/API_READINESS_RUNBOOK.md). Run TestNet
first. The preflight never places an order or sends a Telegram message and does
not certify LIVE trading.

This repository is a Bitcoin-only Binance Spot trading system. It supports one
operator-selected pair at a time. The owner menu is built from current Binance
`exchangeInfo` rows whose base asset is exactly `BTC`, status is `TRADING`, and
Spot trading is explicitly allowed. `BTC_QUOTE_ALLOWLIST` is an optional operator
cap; leaving it empty does not hardcode or rank quote assets.

Both packages default to `simulation` and start with entries **off**. The immutable
`RELEASE_MODE` file separates them: the testnet package permits only simulation or
testnet, while the live package permits only simulation or live. The live package
is **not production-certified**; live execution additionally requires exact release
binding, an offline Ed25519-signed promotion record, an official Freqtrade result
ZIP that passes the configured metrics gate, and every external gate in
`LAUNCH_GITHUB_AND_ORACLE.txt` section 4.

The exact canonical 2025-01-01 through 2026-07-29 backtest has already failed
that gate: 2,349 trades, -49.10% total profit, 0.3019 profit factor and 49.11%
maximum drawdown. Real-money promotion is therefore prohibited until the
strategy is redesigned, frozen and revalidated. Packaging and exchange-safety
hardening do not establish profitability.

## Runtime design

There are exactly four application services:

1. `moneyflow` reads public Spot REST data plus credential-free `aggTrade` and
   `bookTicker` market streams. It publishes bounded 15/30/60-second taker-flow
   windows, order-book imbalance, spread/liquidity, and closed-candle context for
   1m, 5m, 15m, 1h, 2h, 4h, and 1d. It has no credentials or order methods and
   makes no futures request. Optional CoinGecko Demo and CoinMarketCap Basic
   clients fetch only Bitcoin ID (`bitcoin` / `1`) in USD. They cannot select a
   pair, size an order, or move money. When explicitly enabled, fresh fixed-BTC
   provider agreement can participate in the existing MoneyFlow confirmation;
   otherwise it remains advisory. Their default caps are 4% below
   the conservative documented free quotas: CoinGecko 96/minute and 9,600/month;
   CoinMarketCap 28/minute and 9,600/month, with a five-minute minimum cadence,
   durable pre-request reservations and fail-closed state-loss handling.
2. `freqtrade` runs `IctSmcStrategy` as the 1m/5m signal and offline-backtest engine.
   Runtime `LIVE` and `DRY_RUN` order callbacks are denied; only `BACKTEST` and
   `HYPEROPT` may simulate Freqtrade trades.
3. `execution-sidecar` is the only holder of Binance Spot keys and the only order
   owner. It uses durable operation intents, unique client IDs, exchange-filter
   and current execution-rule preflight, synchronized signed requests,
   reconciliation, user-data events, OTOCO/OCO/trailing protection, and no blind
   retry after an ambiguous exchange outcome.
4. `telegram-broker` is an owner-only control plane with one-use confirmations for
   money-affecting commands. Its `/audit` command is a read-only cross-service,
   release, safe-state and freshness check that cannot repair or resume the bot.
   It never receives Binance credentials.

The system does not rank, scan, or rotate through altcoins. Higher timeframes and
Spot microstructure are confirmation context; the inherited entry formula remains
the original 1m entry with a 5m hard trend filter.

## Safe first launch

The current Oracle A1 target uses Ubuntu 24.04 LTS ARM64. Verify the
immutable source before creating any runtime environment file:

```bash
python3 scripts/verify_manifest.py
python3 -m venv /tmp/bitcoin-bot-verify-venv
/tmp/bitcoin-bot-verify-venv/bin/python -m pip install -r requirements-dev.txt
PYTHON=/tmp/bitcoin-bot-verify-venv/bin/python bash deploy/verify_release.sh
```

The first command is standard-library-only and must pass before installing anything.
The full verifier needs the project test dependencies; keep its temporary environment
outside the immutable source tree so it cannot invalidate the exact manifest.

Then use the verified installer documented below. It stores runtime configuration
at `/etc/bitcoin-bot/.env`, outside the immutable release tree. Leave
`EXECUTION_MODE=simulation` and entries off for the first launch.

Do not place Binance or Telegram credentials in Git, the ZIP, an issue, a workflow
log, or a chat. Binance keys should be Spot-trade-only, IP restricted where possible,
and have withdrawals disabled.

Use a dedicated Spot subaccount for this bot. It must begin with no BTC balance and
no pre-existing BTC orders or order lists; otherwise authenticated reconciliation
fails closed. This ownership boundary prevents the emergency-exit path from treating
unrelated, manually held BTC as bot inventory.

In `testnet` mode, authenticated order placement and the execution-sidecar's filter
preflight use Binance Spot Testnet. The read-only signal stack intentionally consumes
production Spot public market data so dry/Testnet decisions see the real market.
Testnet fills therefore validate lifecycle safety, not realistic
liquidity, slippage, or profitability.

For the beginner-safe Oracle host and installer flow, read
[`docs/ORACLE_SETUP_GUIDE.md`](docs/ORACLE_SETUP_GUIDE.md). For the optional
GitHub Actions simulation flow, read
[`docs/GITHUB_ORACLE_DEPLOYMENT.md`](docs/GITHUB_ORACLE_DEPLOYMENT.md). For testnet
and live gates, read [`docs/LIVE_PROMOTION_CHECKLIST.md`](docs/LIVE_PROMOTION_CHECKLIST.md).
The pinned six-repository Binance review and adopted/rejected decisions are in
[`docs/BINANCE_OFFICIAL_SOURCE_REVIEW_2026-07-29.md`](docs/BINANCE_OFFICIAL_SOURCE_REVIEW_2026-07-29.md).
The current Oracle Free Tier sizing, ARM64 image verification, and decisions on
the supplied OCI instance-creation and Oracle Database action repositories are
in [`docs/ORACLE_GITHUB_SOURCE_REVIEW_2026-07-30.md`](docs/ORACLE_GITHUB_SOURCE_REVIEW_2026-07-30.md).

## Pair changes

Use `/pairs` or `/switchpair BTC/USDC` in Telegram. Pair changes follow this
owner-controlled sequence:

1. The sidecar disarms entries, refreshes current Binance metadata, and verifies
   that durable and exchange order state is flat.
2. It records `WAITING_MANUAL_SWAP` without changing the active pair. Balances are
   displayed only as information and never used to infer a market.
3. After any required quote-asset swap is complete, the owner confirms `/swapdone`.
   The sidecar rechecks flatness, quote funding, current filters and the exact
   protection capabilities required by the selected mode.
4. It publishes the one-pair Freqtrade projection. `/verifypair` requires a fresh
   heartbeat with the exact pair generation and configuration hash.
5. Entries remain off until a separate `/start` confirmation.

In live mode, applying a different pair also latches a restart requirement: fresh
evidence bound to the new pair generation must be signed and verified before entries
can be resumed.

The MoneyFlow network client is Spot-only. No USD-M hostname, open-interest,
funding-rate, mark-price, or futures taker/depth input participates in collection or
classification.

## Important limits

- OTOCO pending protection activates only after the entry fully fills. Partial fill,
  cancel, disconnect, and restart paths require reconciliation and may pause entries.
- A stop-limit can remain unfilled through a violent price gap. Emergency exit is a
  structured, explicit owner action and still depends on exchange/network availability.
- Money-flow and order-book pressure are noisy telemetry, not proof of future price.
- A passing backtest is necessary but not sufficient. Freqtrade documents material
  differences between backtests and real fills; complete Testnet and Oracle soak first.
- No software package can make real-money trading risk-free.

## Primary references

- [Freqtrade stable documentation](https://www.freqtrade.io/en/stable/)
- [Freqtrade official repository](https://github.com/freqtrade/freqtrade)
- [Binance official Spot API documentation](https://github.com/binance/binance-spot-api-docs)
- [Oracle Cloud Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)
- [GitHub Actions deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
