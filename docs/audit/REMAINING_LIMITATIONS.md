# Remaining limitations and blocked evidence

Audit date: 2026-07-30  
Readiness: offline release candidate only; real-money promotion is blocked

The source implements a fail-closed live gate, but implementation is not operational
certification. The default is `EXECUTION_MODE=simulation`, `LIVE_TRADING_ENABLED=false`,
`AUTO_CONFIRM=false`, and entries off. No document in this package should be read as a
profitability or safety guarantee.

## Mandatory external gates — BLOCKED on the audit host

| Gate | Current status | Evidence required to close it |
|---|---|---|
| Root Docker image build and Compose runtime | BLOCKED | Build the custom services image; pull the pinned Freqtrade image; validate the exact four-service stack, health checks, volumes, permissions, resource limits and restart behavior on Linux. |
| systemd runtime | BLOCKED | Run `systemd-analyze verify`, install the monitoring units, start every simulation/testnet/live API and report service/timer pair, and retain journal evidence. Static pairing is not a runtime test. |
| GitHub repository and Actions | BLOCKED | Initialize/publish a user-owned private repository, run the full matrix and artifact job, register the restricted `oracle-sim` runner, prove the one-use root-approved digest flow and retain workflow/artifact evidence. The current source directory has no `.git` repository, and private-plan reviewer features must not be assumed. |
| Oracle Free/Always Free host | BLOCKED | Deploy the immutable artifact to a supported Ubuntu A1 Flex host using the current 2 OCPU/12 GB Always Free allocation; prove ARM64 image pulls/build, clock sync, IMDSv2 posture, controlled SSH routing, firewall, disk/memory/swap headroom, container health, backup, three release/rollback cycles, restart, network-loss, OOM and disk-full handling. Free capacity is not guaranteed. |
| Binance Spot Testnet lifecycle | BLOCKED | Prove exact selected-symbol filters and entry/full-fill/partial-fill/stale-entry/OTOCO/OCO/trailing/break-even/profit-lock/cancel/reject/accepted-timeout/reconnect/restart/pair-switch behavior with retained exchange IDs and redacted logs. |
| Real Telegram delivery | BLOCKED | The read-only `/audit` command and owner gate pass offline tests. Prove actual owner-only private-chat delivery, health reporting, one-use confirmation/cancel, status/reconcile, pair switch, protection and emergency-exit workflows with a dedicated test token. |
| Fourteen-day soak | BLOCKED | Retain continuous Oracle simulation/Testnet evidence for service health, reconnects, disk/log retention, monitoring, redaction, clocks, latency and resource use. |
| Canonical Freqtrade profitability gate | FAILED / LIVE BLOCKER | The exact embedded strategy and source matched, but the 2025-01-01 through 2026-07-29 result produced 2,349 trades, -49.10% total profit, 0.3019 profit factor and 49.11% maximum drawdown. Redesign and freeze the strategy before any promotion attempt. |
| Lookahead and recursive analysis | DEFERRED AFTER FAILED PROFITABILITY GATE | Run the official commands over an adequate timerange only after a redesigned frozen strategy first passes profitability/drawdown gates; retain complete output/CSV and review actual bias/variance. A zero exit code or too few signals is not a pass. |
| Live promotion signature | BLOCKED | After every preceding gate, sign fresh exact evidence offline with Ed25519, deploy only the public key and signed/content-addressed evidence, verify validity margin, and obtain protected manual approval. |
| Live orders | NOT AUTHORIZED / NOT RUN | Begin only after all evidence gates close; start with the smallest permitted amount and entries still manually controlled. |

## Inherent trading and exchange limitations

- A strategy can lose money after passing tests and backtests. Backtests cannot reproduce
  all latency, queue position, spread, partial-fill, fee, slippage and outage behavior.
- A stop-limit can trigger yet remain unfilled through a fast gap. Emergency exit still
  depends on Binance and network availability.
- Cancel-and-replace is not atomic at the exchange. The code persists an intent, verifies
  cancellation and fails closed on ambiguity, but exposure can remain temporarily
  unprotected while an exchange-confirmed replacement is being established.
- OTOCO pending legs do not protect a partially filled working entry. The sidecar's
  partial-fill path cancels the working entry, requires terminal confirmation and then
  protects the executed amount; this requires real Testnet race testing.
- Public order-book and taker-flow signals are noisy and can be manipulated or disappear.
  Higher-timeframe and futures observations are context, not proof of future price.
- A matching same-symbol USD-M perpetual may not exist. With
  `REQUIRE_MATCHING_FUTURES=true` the absence fails entries closed; otherwise futures
  context is advisory.
- Quote-market availability, trading status, filters, order-list capability and Binance
  API behavior can change. Pair metadata must be fetched from the same execution
  environment before a money-moving request.
- Binance execution-time price ranges and their reference prices can change continuously.
  The bot fetches both before a replacement, but a preflight cannot guarantee that a
  resting order will remain executable later; terminal expiry still requires
  reconciliation.
- CoinGecko and CoinMarketCap free-plan limits and terms can change. The shipped
  conservative caps, five-minute cadence, durable reservation ledger and backoff reduce
  abuse risk but do not guarantee provider access. Both clients are disabled by default
  and their output is advisory only.
- In testnet mode, public signal/moneyflow telemetry intentionally uses production market
  data for realistic observations, while authenticated execution and filter preflight use
  Spot Testnet. This split must be understood during evidence review; Testnet liquidity
  and fills do not reproduce production.
- Host clock skew can invalidate envelopes and exchange requests. NTP/chrony health is an
  operational prerequisite. Startup server-time synchronization reduces this risk but
  does not replace host clock monitoring.
- An API key must be Spot-trade-only, withdrawals disabled and IP restricted where
  available. Source code cannot enforce Binance account permissions.
- Use a dedicated, otherwise-flat Spot account or subaccount. The emergency path is
  deliberately bounded to durable bot-owned exposure and should not be expected to manage
  unrelated manual BTC holdings.
- SQLite, audit files and snapshots are local durability mechanisms, not a substitute for
  host backups. Disk corruption or total host loss requires external recovery evidence.
- Monitoring is read-only and can report a fault; it cannot restore exchange connectivity,
  fill an order or guarantee notification delivery.

## Final release-assembly boundary

The pre-freeze parent status and stale hashes were replaced during final assembly. The
Bitcoin-only `VALIDATION_STATUS.json` now records the exact offline results; the manifest
was rebuilt only after the code, tests and audit documents were frozen. The deterministic
ZIP and adjacent checksum were then built from that manifest, checked for path traversal,
duplicate names, links, CRC and executable modes, freshly extracted, and retested.

Those checks establish source-package integrity only. They do not close any external gate
listed above. Any later file change invalidates the manifest and requires the complete
manifest, ZIP, checksum and fresh-extract verification sequence to be repeated.
