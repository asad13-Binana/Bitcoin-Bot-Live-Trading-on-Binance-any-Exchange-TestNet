# AWS TestNet incident review — 30 August 2026

## Scope and evidence status

This review records the supplied AWS TestNet audit evidence and the repository
controls that were reproduced against baseline commit
`b136893b4810e034c0922a1204c80ede4c00900b`. It is not evidence from an Oracle
host and does not certify LIVE trading.

## Confirmed observations

- Signal generation, Freqtrade, MoneyFlow, authenticated read-only API checks
  and the owner-only Telegram broker were reported healthy on AWS.
- The first TestNet order lifecycle left `0.00001000 BTC` after the protective
  exit. No active order, order list or intent remained.
- Startup reconciliation treated that balance as unowned and failed closed.
- The execution sidecar then restarted repeatedly under the former unbounded
  restart policy.
- The manifest failure in the AWS working copy was caused by an ignored runtime
  `.env` placed in the repository root. The verifier correctly rejected that
  unexpected file.
- Historical AWS out-of-memory records did not prove which bot instance owned
  each recorded container/cgroup. The four-bot resource contract was therefore
  not changed from ambiguous evidence.

## Non-core corrections in this change

- The execution sidecar uses five bounded retries.
- Telegram starts independently of sidecar health once Freqtrade is healthy,
  preserving owner visibility while entries stay fail-closed.
- Health checking uses the immutable `bitcoin-testnet` identity and its private
  config snapshot; repository-root `.env` fallback is removed.
- Unknown or stale `mode_*` Telegram callbacks fall back safely instead of
  raising a routing exception.
- Every static Telegram menu button has an owner-response regression test.
- Active Oracle guidance now uses the `bitcoin-testnet` paths, runner, wrapper,
  validator, monitoring port and rendered systemd unit names.

## Deliberately unresolved protected-core issue

The ownership meaning of a post-exit residual balance belongs to execution and
risk core. This change does not label the balance as harmless dust, alter durable
trade ownership, clear reconciliation, place an order or modify the strategy.
Until the account and durable state reconcile, TestNet entries must remain off.
A code correction requires a separately authorised, narrow TestNet execution-
safety review and authenticated lifecycle evidence before any LIVE parity work.

## Readiness classification

- GitHub/source: ready for pull-request validation after generated integrity
  records and CI pass.
- AWS incident: restart storm contained; the protected reconciliation gate is
  intentionally preserved.
- Oracle: pending installation and real-host validation.
- LIVE money: prohibited by the separate failed promotion gate.
