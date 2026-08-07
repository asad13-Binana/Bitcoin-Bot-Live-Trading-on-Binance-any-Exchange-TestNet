# Official Binance Source Review — 29 July 2026

## Verdict

The six user-supplied Binance repositories were reviewed at pinned commits.
Four narrow safety/data-integrity improvements were adopted. The signal
strategy, indicator conditions, position sizing, protection formulae and
Bitcoin-only Spot scope were not changed.

The prior canonical strategy backtest remains a failed release gate:
2,349 trades, -49.10% total profit, 0.3019 profit factor and 49.11% maximum
drawdown. This review does not make the live package eligible for real-money
promotion.

## Reviewed source snapshots

| Official repository | Pinned commit | Decision |
| --- | --- | --- |
| `binance/binance-spot-api-docs` | `6b8372cad7cecbdf5dd88a3372eafff51988c5cf` | Authoritative Spot behavior adopted where applicable |
| `binance/binance-api-postman` | `f3f3558e41939a56c8fb92324c4c3825402b0104` | Endpoint and secret-handling cross-check only |
| `binance/binance-public-data` | `5c7f3197591c0d54d85dc43066226bc4c671d47a` | CHECKSUM and timestamp-unit verification adopted |
| `binance/binance-connector-python` | `ceed38e2953556afb191d1e11a5ba1efcc3e1489` | Migration deferred because it would change the core authenticated execution path |
| `binance/binance-connector-js` | `036d6f26c1cea917a68933aadd558afae423734c` | No dependency added; Python bot only |
| `binance/binance-futures-connector-python` | `a6bfbbf10fe2c1b4eb76fc24ffb82eb94bf9df89` | Rejected: deprecated and Futures-only |

The duplicated `/tree/master` links point to the same repository content and
were not counted as separate sources.

## Claude text cross-check

The pasted Claude description is not a literal map of this release:
`services/universe_service/external_signals/`, `coingecko_client.py`,
`cmc_client.py`, `writer_lock.py` and a top-50 universe scanner do not exist in
this bot. The current implementation is Bitcoin-only and keeps optional
CoinGecko/CoinMarketCap advisory context in `services/moneyflow/`.

Useful principles from that text—stable provider identity, default-off
enrichment, durable pre-request quotas, bounded HTTP, breaker/backoff and
fail-closed state—were independently verified in the actual files and tests.
No market-cap floor, top-50 scanner or external-provider entry veto was added,
because that would create a new universe/decision path and alter the bot's
scope.

## Adopted changes

### 1. Current `PRICE_RANGE` execution-rule preflight

Binance now exposes execution-time rules through
`GET /api/v3/executionRules` and a continually changing reference through
`GET /api/v3/referencePrice`. A taker execution outside the allowed range may
expire with `EXECUTION_RULE_PRICE_RANGE_EXCEEDED`.

The replacement preflight now:

- requests exactly one BTC Spot symbol;
- parses at most one `PRICE_RANGE` rule fail-closed;
- respects Binance's documented optional-multiplier and null-reference
  behavior;
- applies bid multipliers to BUY execution prices and ask multipliers to SELL
  execution prices;
- reuses the existing second preflight immediately before replacement POST;
- records the rule and reference in the credential-free audit summary.

The rule is applied to the order execution price, not the stop trigger price.
It does not alter entry signals or calculated strategy prices.

Read-only Binance plugin snapshot captured during this review:

- symbol: `BTCUSDT`, status `TRADING`, Spot allowed;
- `PRICE_RANGE`: bid/ask up `1.1500`, bid/ask down `0.8500`;
- reference price: `63939.40659165` at Binance timestamp
  `1785341300013`;
- legacy `PERCENT_PRICE_BY_SIDE`: up `2`, down `0.5`.

This snapshot is evidence of the validation gap at review time, not a
hardcoded trading input. Runtime always queries current values.

### 2. `EXPIRED_IN_MATCH` terminal state

The authenticated event store now treats `EXPIRED_IN_MATCH` as terminal for
both BUY and SELL orders. An unfilled BUY is closed as an entry rejection; a
SELL protection expiration requires authoritative reconciliation. This aligns
the persisted lifecycle with Binance's self-trade-prevention order status.

### 3. Signed-request timing and rate-limit observability

The authenticated Spot gateway now:

- sets an explicit bounded HTTP timeout;
- caps `recvWindow` at Binance's recommended 5,000 ms maximum;
- synchronizes its timestamp offset at startup using the server-time
  round-trip midpoint and fails startup when the sample latency is excessive;
- uses that synchronized timestamp for the WebSocket API user-data
  subscription;
- captures `X-MBX-USED-WEIGHT-*`, `X-MBX-ORDER-COUNT-*` and `Retry-After`
  headers without credentials.

Authenticated order POSTs are still never blindly retried. Transport faults,
HTTP 5xx, HTTP 418/429 and Binance timeout/unknown outcomes remain ambiguous
until deterministic client-ID reconciliation proves the result.

### 4. Official public-data archive verifier

`scripts/verify_binance_public_kline_archive.py` now checks an official Spot
kline ZIP and its adjacent `.CHECKSUM` before data is used:

- exact SHA-256 and declared filename;
- safe single-CSV ZIP structure, no traversal, duplicate, encrypted, symlink
  or device entries;
- ZIP CRC by streaming the complete member;
- 12-column kline schema and finite numeric values;
- increasing open timestamps and consistent millisecond/microsecond units.

This is important because Binance documents microsecond timestamps for Spot
archives from 1 January 2025 onward and may replace historical archives after
corrections.

Example:

```powershell
python scripts/verify_binance_public_kline_archive.py `
  .\BTCUSDT-1m-2025-01.zip `
  --checksum .\BTCUSDT-1m-2025-01.zip.CHECKSUM
```

The verifier is read-only. It does not normalize or rewrite the raw archive.

Live proof against the official data host passed for
`BTCUSDT-1m-2025-01-01.zip`: adjacent CHECKSUM matched SHA-256
`10a12909f1b0e3fcc6b7f502e5ea9be5d1ba3455dd8ab16cc61c8650640ba7c0`;
the ZIP contained 1,440 valid rows and was correctly identified as
microseconds.

## Incidental audit-host correction

Repeated all-files tests exposed an unrelated Windows-only hang in parent
directory `fsync` after an atomic JSON replace. Python faulthandler identified
the exact frame. Directory `fsync` is now performed only on POSIX, preserving
the Oracle/Linux durability path; Windows still performs file flush, file
`fsync`, permission update and atomic replace. No trading calculation changed.

## Existing behavior confirmed correct

- HTTP 5xx and `-1007` order outcomes are classified as unknown/ambiguous,
  never assumed rejected and never blindly resent.
- Public HTTP 429/418 responses honor `Retry-After`; 418 establishes an
  in-process ban-until latch.
- The user-data stream uses
  `userDataStream.subscribe.signature`, not the retired listen-key REST
  endpoints, and reconnects after `serverShutdown` or
  `eventStreamTerminated`.
- New-order preflight requires symbol status `TRADING`; `CANCEL_ONLY` cannot
  pass.
- API key guidance remains Spot trading only, withdrawals disabled and IP
  restricted.

## Not adopted

### Official Python connector migration

The modular official connector is a credible future replacement for
`python-binance`, but changing the authenticated order, order-list,
reconciliation and error-mapping path is a core execution migration. It was
therefore deferred under the user's no-core-change instruction. A future
migration needs contract tests and complete Binance Spot Testnet lifecycle
drills before release.

Automatic SDK retries must not be enabled for non-idempotent order POSTs.

### JavaScript connector

No JavaScript runtime or dependency was added. Its timeout, reconnect and
rate-limit patterns were used only as an independent design cross-check.

### Futures connector

The reviewed repository declares itself deprecated and supports `/fapi/*` and
`/dapi/*`. Futures, margin and leverage remain excluded from this Spot-only
bot.

### Postman collection

The collection confirmed current Spot endpoint coverage and secret placeholder
practice. It is an operator/API exploration aid, not production bot code, so
nothing was imported from it.

## Remaining external gates

- No authenticated Binance Spot Testnet order lifecycle was executed.
- Docker Linux image/runtime validation remains unavailable on this host.
- GitHub Actions, Oracle deployment/soak and real Telegram delivery remain
  unverified.
- Real-money launch remains prohibited by the failed canonical strategy
  backtest, independently of these safety fixes.
