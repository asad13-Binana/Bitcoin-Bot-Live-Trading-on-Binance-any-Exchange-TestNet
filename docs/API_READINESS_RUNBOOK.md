# API readiness runbook

This is a credential-gated, **read-only** preflight for the exact installed
release. It checks Binance authentication/time/symbol/open-order visibility,
Telegram bot identity and owner-chat visibility, and enabled Bitcoin-only
CoinGecko/CoinMarketCap providers. It cannot place or cancel an order and does
not send a Telegram message.

## Private configuration

Create `/etc/bitcoin-bot/.env` from `.env.example`, populate it outside Git,
and keep it a regular `root:root` file with mode `0600`. Never paste a key or
token into a GitHub issue, workflow variable, command line, log, or audit
report. For Binance, use a dedicated Spot key, disable withdrawals, and apply
the Oracle instance's stable egress IP restriction.

Before the Telegram check, open the bot in Telegram and send `/start`. Put the
numeric owner chat ID in `TELEGRAM_OWNER_CHAT_ID`. CoinGecko and
CoinMarketCap remain optional; leave their `*_CONTEXT_ENABLED` values `false`
unless dedicated keys are installed.

## TestNet first

Set `EXECUTION_MODE=testnet`, `BOT_ENVIRONMENT=TESTNET`,
`LIVE_TRADING_ENABLED=false`, and use Binance Spot Testnet credentials. Then:

```bash
sudo /opt/bitcoin-bot/current/deploy/api_preflight.sh \
  | sudo tee /var/log/bitcoin-bot/api-readiness-testnet.json >/dev/null
```

The command exits non-zero if any required check fails. A pass proves API
identity and read-only authentication only. It does **not** prove order
lifecycle, Telegram message delivery, crash recovery, Oracle soak, or trading
profitability. Perform the TestNet lifecycle drills in
`docs/EXTERNAL_VALIDATION_RUNBOOK.md` next.

## LIVE package: authentication only, still disabled

Do this only after TestNet and Oracle gates are complete. Keep
`EXECUTION_MODE=simulation`, `BOT_ENVIRONMENT=LIVE`, and
`LIVE_TRADING_ENABLED=false`; use a dedicated production key with withdrawals
disabled and IP restriction enabled. The explicit phrase prevents accidental
production authentication:

```bash
sudo /opt/bitcoin-bot/current/deploy/api_preflight.sh \
  --confirm-live-read-only LIVE_READ_ONLY_NO_ORDERS \
  | sudo tee /var/log/bitcoin-bot/api-readiness-live-read-only.json >/dev/null
```

This does not enable LIVE trading. The current protected strategy failed its
profitability/drawdown gate, so real-money promotion remains prohibited.
