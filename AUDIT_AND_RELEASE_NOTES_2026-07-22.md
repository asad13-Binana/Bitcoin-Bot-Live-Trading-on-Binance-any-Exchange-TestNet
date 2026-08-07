# Audit and release notes — 2026-07-22

This release is a clean Bitcoin-only consolidation, not an in-place rename of the
supplied multi-asset bot and not a direct copy of the 11-file Bitcoin upgrade donor.

## Evidence verdict

- The supplied parent ZIP checksum was valid and its extracted content matched the
  previously reviewed hardened live package.
- The Bitcoin donor ZIP was incomplete as a deployable bot. Its stated 20-test result
  reproduced as 19 passes and one failure: client IDs collided under a fixed clock on
  Windows. Concepts were reviewed individually; unsafe donor modules were not copied.
- The inherited all-in-one runtime combined unrelated asset-selection responsibilities
  and a structurally fixed quote assumption. It was excluded from this release.
- The parent Freqtrade entry formula was preserved as a 1m/5m formula. Requested higher
  timeframes were added to read-only telemetry, not smuggled into entry conditions.

## Material fixes in this build

- Exact BTC/stable pair policy and generation-hashed one-pair state.
- Read-only Spot/USD-M money-flow service with seven requested timeframes.
- Clean Spot-only adapter; no margin, transfer, withdrawal, or futures-order methods.
- Intent-before-submit lifecycle, deterministic lookup, and no blind ambiguous retry.
- Collision-resistant client IDs tested concurrently under a fixed clock.
- OTOCO entry plus OCO/trailing/break-even/profit-lock replacement with filter preflight.
- Safe pair switching and mandatory fresh evidence/restart after a live switch.
- Owner-only Telegram menu with one-use confirmations and token-safe diagnostics.
- Separate monitoring environments and complete simulation/testnet/live systemd pairs.
- Ed25519 live certifier: private key offline, public key runtime-only.
- Official Freqtrade ZIP parsing, embedded full-strategy/config verification, and policy
  binding instead of trusting operator-typed backtest claims.
- Multi-architecture Docker image indexes pinned by digest; runtime Python locks include
  hashes and are installed with `--require-hashes`.

Final counts and environment-dependent blockers are recorded in
`VALIDATION_STATUS.json`; the finding and per-file evidence are in
`docs/audit/ISSUE_LEDGER.csv` and `docs/audit/FILE_REVIEW_LEDGER.csv`. The adjacent
`.zip.sha256` is the authoritative artifact checksum after fresh extraction and retest.
