# Monitoring security model

- Bind: loopback only; API docs off by default.
- Auth: non-placeholder Bearer token of at least 32 characters, compared with
  `hmac.compare_digest`.
- Network: `MONITOR_ALLOWED_IPS` is enforced before authentication; default is
  loopback IPv4/IPv6 only.
- Rate limit: per authenticated client, so invalid requests cannot consume the
  valid client's quota.
- Audit: authorized, denied, and rate-limited requests carry request IDs. An
  unwritable audit path makes the request fail visibly with 503.
- Redaction: Bearer/Basic auth, quoted secrets, short env tokens, Telegram,
  GitHub/OpenAI keys, database URLs, signed queries, private keys, and nested
  secret fields are removed from every response.
- Privilege: `botmon` has no trading env and no Docker socket. A fixed root-owned
  helper publishes only container name/service/state/health/restart/start time.
- Filesystem: systemd uses `ProtectSystem=strict`, no capabilities, private
  devices/tmp, kernel/control protections, and narrow read/write paths.
- MCP: refuses non-loopback URLs and returns structured failures.
- Telegram: enable flag enforced; API and Telegram tokens never appear in
  success or failure output.
- Simulation: public production market data may be observed without API keys;
  the monitor has no signed-account client and no order-capable route.

The monitor is observability only. It does not certify a strategy, approve live
trading, or replace Oracle testnet lifecycle and soak evidence.
