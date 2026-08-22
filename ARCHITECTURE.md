# Architecture and trust boundaries

```text
Binance public Spot REST + market-data WebSocket
          |
          v
   moneyflow (no keys) --------------------------+
                                                  |
Binance Spot candles                              v
          |                               latest flow snapshot
          v                                      |
Freqtrade signal engine -- signed signal --> execution-sidecar -- authenticated Spot orders
      (no trading keys)                    (sole key/order owner)
                                                  ^
                                                  |
Telegram owner -- signed command --> telegram-broker
                         (no Binance keys)
```

## One active pair

`shared/pair/active_pair.json` is authoritative and contains a generation counter and
hash. The sidecar is the only writer. It publishes two projections: a RemotePairList
file and a later Freqtrade config overlay. If publication is interrupted, strategy and
sidecar pair-hash checks reject stale or cross-generation signals.

Every accepted market is validated from current Binance `exchangeInfo`: base `BTC`,
allowlisted quote, Spot enabled, `TRADING`, filters present, and OCO/OTO capability.
No suffix guessing is used for money-moving decisions.

## Signal ownership

The preserved formula is:

- 5m: EMA9 > EMA21 > EMA50, close above 5m EMA50, positive 5m MACD histogram.
- 1m: close above rolling VWAP, recent EMA9/21-zone pullback, close reclaims rising
  EMA9, RSI above 50 and rising, RVOL at least 1.5, ADX above 20, positive volume.

1m MACD is calculated but remains non-gating, matching the supplied parent strategy.
The 15m/1h/2h/4h/1d series are monitoring context and do not silently alter that formula.

Freqtrade emits a release-bound HMAC envelope for a closed candle. The sidecar checks
signature, producer, release, TTL, exact pair generation, freshness, deduplication,
risk state, optional money-flow policy, and entries-armed state before preparing an
entry. Freqtrade itself denies runtime trade creation.

## Execution state machine

Before every network side effect, SQLite stores a deterministic operation intent.
The important outcomes are:

- `CONFIRMED`: exchange identifier was returned or recovered by client-ID lookup.
- `DEFINITE_REJECT`: Binance unambiguously rejected the request; no order exists.
- `AMBIGUOUS`: timeout, transport failure, 5xx/rate-limit ambiguity, or malformed
  success. Entries pause and the request is not blindly resent.

Lifecycle rows remain nonterminal for exposure-bearing or uncertain states. Pair
switches and new entries are blocked while another BTC exposure or unresolved intent
exists. User-data stream reconnects also disarm entries until reconciliation.

## Protection ownership

The first placement is OTOCO where current symbol capabilities permit it: a limit buy
working order plus pending sell take-profit/stop legs. After entry, the owner can use
fixed OCO, trailing-only, OCO-with-trailing, fee-adjusted break-even, or profit lock.
Replacement validates all filters and order/list capacity before cancel. If cancel is
ambiguous, no replacement sell is submitted. Quantity is bounded by verified free BTC
and recorded fills.

## Promotion boundary

Signals and owner commands use different HMAC keys. Live evidence uses Ed25519 instead:
the private key stays offline and Oracle receives only the public key. Evidence expires
and binds the exact manifest hash, full strategy bytes and method fingerprints, pair
generation, stable-quote policy, deployed risk policy, official Freqtrade ZIP bytes,
embedded sanitized config/source, parsed metrics, and explicit external assertions.

The release remains simulation-capable without evidence. Live startup fails closed if
any binding differs.
