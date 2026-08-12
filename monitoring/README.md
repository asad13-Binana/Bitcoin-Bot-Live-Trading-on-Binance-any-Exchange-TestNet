# Bitcoin bot read-only monitoring

This is the single canonical monitor. It exposes 12 authenticated GET routes
under `/api/v1`, 12 read-only MCP tools, an optional Telegram report, and
mode-specific systemd units. It has no Binance credentials and no order,
cancel, buy, sell, transfer, or configuration-write route.

## Data-source contract

- Real order lifecycle/open positions: `shared/runtime/sidecar/execution_state.sqlite`.
- Real realised P/L, once the fill-ledger projection exists: `shared/runtime/sidecar/pnl_ledger.jsonl`.
- Freqtrade: reported separately as **signal-only**, never as real execution.
- Active pair, money-flow, service and WebSocket state: bot-produced JSON files.
- Docker status: the root-owned deployment safety guard writes a sanitized
  snapshot. No monitoring component or `botmon` process receives the Docker
  socket or Docker CLI.
- Deployment/validation: installer-produced status files bound to the exact
  release hash.

Every returned string is recursively secret-redacted. Authentication uses a
constant-time Bearer comparison, loopback source allow-list, per-client rate
limit after authentication, request IDs, and mandatory audit logging.

Routes: `health`, `status`, `performance`, `trades`, `errors`, `crashes`,
`latency`, `system`, `deployment`, `pair`, `moneyflow`, and `report`.

The money-flow response is bounded to the active BTC/quote pair. It reports
Spot pressure, matching USD-M futures context when that exact symbol exists,
and the 1m/5m/15m/1h/2h/4h/1d trend summary. A missing matching futures market
is reported explicitly and is never silently replaced by a different quote.

Simulation, testnet and live monitoring default to loopback port 8091 so this
Bitcoin Bot cannot collide with the separate Binana bot's port 8090. Live is programmatically disabled
until the operator explicitly enables the live monitor after formal promotion.
Simulation probes only Binance's unauthenticated production market-data ping;
it has no account credentials and never submits an exchange order.
See `INSTALL.md`, `SECURITY.md`, and `../docs/OFFICIAL_DEPLOYMENT_REFERENCES.md`.
