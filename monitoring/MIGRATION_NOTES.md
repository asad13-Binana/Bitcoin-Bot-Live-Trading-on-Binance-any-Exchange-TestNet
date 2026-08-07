# Bitcoin monitoring migration

The hardened Bearer API, MCP bridge and Telegram reporter remain the only
monitoring implementations. The Bitcoin release changes the deployment prefix
to `bitcoin-bot`, expects exactly `moneyflow`, `freqtrade`,
`execution-sidecar`, and `telegram-broker`, and replaces broad-market status
with active-pair and money-flow views.

The security contract is unchanged: recursive redaction, constant-time auth,
authenticated-client rate limiting, durable request audit, strict request
bounds, `/api/v1` versioning, docs-off default, read-only databases, loopback
MCP, isolated monitor credentials, and hardened per-mode systemd pairs.
