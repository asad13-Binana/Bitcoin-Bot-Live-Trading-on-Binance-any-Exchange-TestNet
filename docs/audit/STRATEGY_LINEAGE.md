# Strategy lineage and preservation boundary

Audit date: 2026-07-29  
Strategy: `IctSmcStrategy`  
Runtime trading universe: one exact owner-selected Binance BTC-base Spot pair

## Provenance

The Bitcoin build derives its signal formula from the audited parent Binance Bot while
using Freqtrade 2026.6 as the indicator, signal and offline-analysis engine. The source
archives recorded by the manifest builder are:

| Evidence archive | SHA-256 | Use |
|---|---|---|
| `binance-bot-live-trading-CLAUDE.zip` | `4945ba6fa0ae82a6bf84f836f5308d6f9f9473975bc17c579d719e291bc456b4` | Audited safety baseline and parent signal/Telegram lineage. |
| `bitcoin_bot.zip` | `8399b74b62e2e673618b3e21dfab43bebfb661be6f3c05b2a792d2556480a7e3` | Design donor reviewed selectively; not copied as a deployable whole. |

The release notes record that the donor's original 20-test claim reproduced as 19 passes
and one Windows fixed-clock client-ID collision failure. The new client-ID and execution
implementation was rebuilt and regression-tested rather than accepting the donor module
unchanged.

## Preserved entry formula

The entry signal remains the parent 1m formula with its 5m hard trend filter:

1. 5m EMA9 > EMA21 > EMA50.
2. Current close is above the informative 5m EMA50.
3. 5m MACD histogram is positive.
4. 1m close is above the 200-bar rolling VWAP approximation.
5. A low in the latest three 1m bars touched its own EMA9/EMA21 zone.
6. Close reclaims EMA9 and EMA9 is non-decreasing.
7. RSI is above 50 and rising.
8. Relative volume is at least 1.5.
9. ADX is above 20 and volume is positive.

The 1m MACD (5/13/6) remains calculated but non-gating. The exit signal remains the
structural `close < rolling VWAP` plus negative 5m MACD-histogram condition; sidecar-owned
exchange protection governs runtime orders.

## Intentional Bitcoin-only change

The parent `populate_entry_trend()` included a Sharia eligibility tail. The user explicitly
required that Sharia scanning and top-50/altcoin scanning not be included in the Bitcoin
bot. That tail was therefore removed. This is a deliberate scope removal, not a hidden
trading-condition change. Exact market confinement is enforced elsewhere:

- `PairController` owns a generation-hashed active pair with base `BTC`; current
  Binance metadata establishes the quote and an optional operator allowlist can cap it.
- The Freqtrade one-pair projections must match that state exactly.
- `bot_loop_start()` emits only for the authoritative active pair.
- The sidecar verifies the pair hash/generation again before entry.
- `deploy/verify_release.sh` rejects Sharia and universe-service paths.

The requested current fingerprints for `populate_entry_trend()` are:

```text
source_sha256 = 8913eb105ed4e5b7195482e89510cf1b2d0db3f13b7dba3bb213ce0b78c0dc28
token_sha256  = 8316280ffc4b9c72c0a5687df071a5713ac5bdae0d1dec7b4b795351d70356fc
```

These are canonical method fingerprints, not the hash of the complete strategy file.
Whitespace/comments affect the source fingerprint; the logical-token fingerprint removes
that presentation sensitivity. Both are enforced by `scripts/build_manifest.py`.

For completeness, the other protected signal-method fingerprints currently encoded by
the manifest builder are:

| Method | Source SHA-256 | Logical-token SHA-256 |
|---|---|---|
| `populate_indicators_5m` | `3c01ceda9807efbcf63b32297879c04af6cc65744387dd4821a2ed1328025969` | `071a394a70a2370b05700ba8e58bfbbeec6d66fb48ca78995dc8fc5dd98e265b` |
| `populate_indicators` | `11c39597e4c7f535808e36db290f36f8908dc0e3b98578d9d92dd1e8abd93526` | `ab8b017314652ed1d08f9c23813eade8a79d5a67c0831a624095f315ef6ecd59` |
| `populate_entry_trend` | `8913eb105ed4e5b7195482e89510cf1b2d0db3f13b7dba3bb213ce0b78c0dc28` | `8316280ffc4b9c72c0a5687df071a5713ac5bdae0d1dec7b4b795351d70356fc` |
| `populate_exit_trend` | `fdd2c099edf44b4db4408a5aec183f2c493c95db34d66975697dc6a50c50c196` | `dc79eb4e19e4c68db8c3e877c671a115edb11f87f40cf1a9b34fbb0c457ccd7f` |

## What was added without changing the formula

- Higher 15m/1h/2h/4h/1d candles, Spot pressure and same-symbol futures pressure are
  external context. They can be required by explicit risk policy but are not silently
  inserted into `populate_entry_trend()`.
- Freqtrade runtime order callbacks are denied. The signal envelope is HMAC authenticated,
  release-bound, pair-generation-bound, time bounded and replay protected; the execution
  sidecar owns all authenticated Binance requests.
- OTOCO/OCO/trailing/break-even/profit-lock and Telegram controls are execution/risk layers,
  not signal conditions.
- Live evidence binds the complete strategy-file hash in addition to these method
  fingerprints, preventing unreviewed changes elsewhere in the class.

## Verification and promotion boundary

`tests/test_control_and_flow.py::test_freqtrade_signal_formula_has_the_exact_preserved_1m_5m_structure`
checks the condition structure and the absence of the removed operational tail. Manifest
generation independently recomputes all protected fingerprints and fails on mismatch.

This lineage check proves identity of reviewed code, not profitability. An official exact
pair Freqtrade result ZIP, lookahead analysis, recursive analysis, Testnet drills, Oracle
soak and signed live evidence remain mandatory and were blocked on the local audit host.
