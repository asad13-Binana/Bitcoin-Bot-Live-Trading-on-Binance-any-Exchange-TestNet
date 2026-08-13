# External validation runbook

These checks require real infrastructure and were not represented as local passes.

Before lifecycle testing, run the credential-gated read-only checks in
`docs/API_READINESS_RUNBOOK.md`. A successful preflight proves API identity and
authentication only; it does not replace any drill below.

## Binance Spot Testnet

Use a dedicated test account and the chosen BTC pair. Record timestamps, request IDs,
client order IDs, exchange order/list IDs, balances, and sidecar audit events for:

1. rejected filters and insufficient balance before submission;
2. full and partial entry fills with protective sell coverage;
3. cancel-and-replace success, definite rejection, timeout, and 5xx ambiguity;
4. OCO + trailing, trailing-only, fee-adjusted break-even, and profit lock;
5. restart with open order, open list, BTC balance, unresolved intent, and disconnected
   user-data stream;
6. pair switch only after both old/new symbols are exchange-verified flat;
7. Telegram unauthorized chat/user, expired callback, replayed callback, and owner
   confirmation.

Any unknown exchange outcome must pause entries and produce no blind duplicate POST.

## Oracle soak

Run simulation/Testnet for fourteen continuous days. Daily, retain:

- container identity/health, release validation and deployment status;
- chrony sync, memory, swap, disk and inode headroom;
- public Spot/futures availability and API latency/errors;
- signal/command rejection counts, unresolved intents and reconciliation result;
- monitor authentication/rate-limit/audit events and redaction samples;
- Telegram delivery without token leakage.

Exercise three upgrades and forced rollback drills. A rollback counts only if the old
release/config/image identity is proven healthy and exchange state reconciles.

## Production observation

This section is blocked until the strategy is redesigned, frozen, passes untouched
out-of-sample analysis, and every Testnet/Oracle/promotion gate is closed.
Start with the smallest exchange-valid quote amount and entries off. Reconcile before
the first owner resume. Monitor every initial order manually. Stop promotion if
observed fill/slippage, partial-fill handling, fees, latency, or protection behavior
differs materially from retained evidence.
