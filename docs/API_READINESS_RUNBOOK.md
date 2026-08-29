# API readiness runbook

This is a credential-gated, **read-only** preflight for the exact installed
release. It checks Binance authentication/time/symbol/open-order visibility,
Telegram bot identity and owner-chat visibility, and enabled Bitcoin-only
CoinGecko/CoinMarketCap providers. It cannot place or cancel an order and does
not send a Telegram message.

## Private configuration

Create `/etc/bitcoin-testnet/.env` from `.env.example`, populate it outside Git,
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
sudo /opt/bitcoin-testnet/current/deploy/api_preflight.sh \
  | sudo tee /var/log/bitcoin-testnet/api-readiness-testnet.json >/dev/null
```

The command exits non-zero if any required check fails. A pass proves API
identity and read-only authentication only. It does **not** prove order
lifecycle, Telegram message delivery, crash recovery, Oracle soak, or trading
profitability. Perform the TestNet lifecycle drills in
`docs/EXTERNAL_VALIDATION_RUNBOOK.md` next.

## LIVE boundary

Do not put production credentials in this TestNet instance. Production
read-only authentication belongs to the separately reviewed LIVE repository
and cannot promote this TestNet package to LIVE trading.
