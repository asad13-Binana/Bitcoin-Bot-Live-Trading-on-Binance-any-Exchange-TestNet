# Release, rollback, and recovery guide

## Release identity

Each CI artifact contains one `bitcoin-bot/` root. Its manifest hash becomes the
release identity and image tag. Oracle snapshots `/etc/bitcoin-bot/.env` to a
timestamp-matched mode-0600 file. A successful generation receives a one-line marker
containing both the release hash and configuration SHA-256.

`/opt/bitcoin-bot/current` is switched atomically. Do not edit this link, a config
snapshot, success marker, manifest, evidence document, or retained backtest.

## Automatic rollback

Before cutover the installer proves that the old project is the exact healthy
release/config/image generation and, for live, that its signed evidence remains valid
for at least one hour. It pauses entries, reconciles, stops the old owner, verifies
that the fixed Compose project is empty, and only then starts the new owner.

If new startup or monitoring fails, the installer removes the attempted project and
independently proves that no project container remains. It then revalidates the old
config and live evidence, starts the old generation, and verifies exact identity and
health. If any container remains unidentified, deletion and symlink switching stop;
the failed release/config/image are deliberately preserved.

Read `runtime/deployment_status.json` and `runtime/release_validation.json`. Statuses
ending in `_CRITICAL` require manual investigation with entries off.

## Operator rollback

Do not run ad-hoc `docker compose up` against a retained directory and do not point
`current` backward manually. To deliberately restore old code, download the older
CI artifact and verify its checksum. For the supported simulation path, approve that
exact tarball SHA-256 out of band and use the restricted root wrapper; do not invoke
the root-managed installer directly. It will create a new timestamp/config identity
and use the same reconciliation and health gates. Testnet/live rollback requires a
separately reviewed mode-specific deployment path that is not included here.

## Retention

The installer retains the active release and the configured number of successful
rollback generations. It prunes only canonical timestamp-named direct children, uses
the two-hash marker, and cannot cross a nested mount. Signed live evidence and
content-addressed backtests are never automatically deleted because they are audit
records. Archive those records offline before any manual space cleanup.

Do not use broad Docker prune commands on the trading host. A failed unique image is
removed only when no container references it; uncertainty is preserved for diagnosis.

## Recovery priorities

1. Keep entries off and preserve exchange-native protective sells.
2. Determine all project containers with
   `docker ps -a --filter label=com.docker.compose.project=bitcoin-bot`.
3. Compare `current`, the two-hash marker, config snapshot, and deployment status.
4. Reconcile Binance Spot open orders, order lists, and BTC balance before restart.
5. Use a verified CI artifact through the normal installer. Never retry an ambiguous
   order placement or cancel-and-replace operation by hand without reconciliation.
