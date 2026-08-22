# Spot-only MoneyFlow and external confluence

This addendum records the current source boundary without rewriting the historical
audit reports.

## Network and execution boundary

- MoneyFlow uses credential-free Binance Spot REST and market-data WebSocket endpoints.
- The WebSocket subscribes only to the active BTC symbol's `aggTrade` and `bookTicker`
  streams on the market-data-only host.
- No USD-M/futures client, hostname, endpoint, open interest, mark price, funding rate,
  or futures taker/depth signal is used.
- The service has no account, order, transfer, withdrawal, or signed-request method.
- The protected Freqtrade strategy formula is unchanged.

## Rolling flow

The stream calculates receive-time 15, 30, and 60-second windows. Duplicate and
out-of-order aggregate IDs are ignored. A sequence gap, reconnect, or symbol change
clears the sample and requires a complete 60-second warm-up. During warm-up the
existing bounded Spot REST aggregate-trade calculation is labelled as a fallback.

## CoinGecko and CoinMarketCap

Both fixed-Bitcoin clients remain disabled until private API keys are installed on the
Oracle host. `REQUIRE_EXTERNAL_CONFLUENCE=true` may be used only with
`REQUIRE_FLOW_CONTEXT=true`. When required, the enabled providers must supply enough
fresh BTC/USD observations, agree with the configured 24-hour direction floor, and
remain within the configured deviation from Binance Spot. Missing, stale,
contradictory, malformed, or excessively divergent provider evidence fails closed.

This source readiness does not prove profitability, authenticated TestNet behaviour,
Oracle stability, provider availability, or LIVE-money suitability.
