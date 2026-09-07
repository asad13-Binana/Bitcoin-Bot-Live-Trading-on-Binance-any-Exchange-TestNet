# AWS runtime and adaptive protection repair — 7 September 2026

Fixes prepared with **GPT-6 Astra**. This is repair attribution, not a trading certification or cryptographic signature. The owner reviews and manually merges changes.

## Behaviour and boundaries

The protected IctSmcStrategy file remains byte-identical, SHA-256 `023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340`. Entry indicators and their thresholds are unchanged. The application fixes are shared by the Testnet and Live repositories; RELEASE_MODE, manifests, provenance, deployment identities and Live promotion gates remain separate. The optional stable Telegram panel refuses Live mode. No Live-money test or Oracle deployment is claimed.

The existing Moneyflow bullish classification combines its existing depth, volume/trade-flow and higher-timeframe conditions. No new order-block detector, AI model, indicator or volatility threshold is introduced. OCO remains in place when the existing conditions do not justify conversion. When fresh bullish pressure and sufficient price/fee margin are present, automatic management can replace it once with the existing tight trailing-only order. A bullish transition goes directly to trailing-only rather than cancelling protection twice through an intermediate break-even OCO. Otherwise, the existing break-even threshold can tighten the OCO. An owner OFF overrides the environment default.

Conversion requires a reconciled protected trade, current matching Moneyflow state, no unresolved operation and a connected subscribed stream. The SELL limit must cover the configured fees, slippage allowance and fill buffer. Tick rounding is applied to the fee floor before deriving the stop trigger. Temporary entry suspension does not overwrite a risk pause; automatic entry processing resumes only after authenticated reconciliation and only if it was previously enabled. Restart continues to require owner resume.

## Confirmed defects repaired in source

- Working AWS overlays are incorporated into the source: fixed-base sizing, Testnet manual-entry routing, authenticated emergency-fill recovery, account stream handling, keyless CoinGecko and the operator panel.
- An emergency recovery may not mark a one-step short fill as a complete exit. Order, client ID, side, symbol and exact quantities must agree.
- Ordinary balance messages use a coalesced ownership reconciliation instead of a false disconnected-stream event. Execution callbacks and order management are serialised.
- Adaptive configuration is initialised for Testnet as well as Live, respects an explicit owner OFF and publishes its decision in health/status.
- Break-even stop-limit rounding includes fees, both slippage allowances and the fill buffer. Invalid preflight does not itself pause an otherwise running bot.
- The notifier advances through its durable history beyond the first 500 signals; acknowledgement failures remain pending. Exactly-once delivery across a crash after Telegram accepts but before local commit is not guaranteed by Telegram.
- P&L displays are explicitly gross, fees excluded, use cumulative fills without counting duplicate events twice, include confirmed emergency exits and separate quote currencies. Missing historical fill evidence is not invented.
- Health reporting checks timestamp freshness; log previews read a bounded tail.
- The owner process holds an idle WAL connection, allowing read-only database consumers and checkpoints without an unbounded auxiliary keeper container.
- Source and offline/container verification consistently pin Freqtrade 2026.8 at the published multiarchitecture digest. Freqtrade's required temporary directories are declared, and the sidecar restarts unless explicitly stopped.

## Research and execution limits

[Binance's trailing FAQ](https://developers.binance.com/en/docs/products/spot/faqs/trailing-stop-faq) permits a trailing leg inside OCO. A SELL trails the highest trade after activation; its limit price does not follow the trigger. OCO_TRAILING therefore already has native trailing protection. Removing its take-profit leg is a separate adaptive replacement. A STOP_LOSS SELL stopPrice is a downward activation condition, so an above-entry stopPrice is not a profit-arming switch.

[Binance trading endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade) and the [amend FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/order_amend_keep_priority.md) do not provide an in-place OCO-to-trailing-only amendment. Cancellation and replacement have a protection gap; durable intents and reconciliation address ambiguous responses, not exchange atomicity. The retained stop-limit order can miss a fill in a fast gap, as explained in [Freqtrade stoploss guidance](https://www.freqtrade.io/en/stable/stoploss/). Binance's own API supports other order types; this repair preserves the bot's existing stop-limit choice. A replacement starts a new trailing observation window; it does not inherit the old order's peak. No guarantee of fill, profit, or uninterrupted service is made.

[SQLite WAL guidance](https://www.sqlite.org/wal.html) explains why read-only consumers need accessible WAL sidefiles. The anchor holds no read transaction, so checkpoints remain possible; immutable mode is not used on a changing database.

## Release and operations status

The old audit's 79% disk usage and obsolete size-sync timer were stale at fresh inspection: AWS was 66% used and the timer was absent. The old AWS release manifest really failed and multiple runtime overlays really differed. These facts require installing a new verified release, not marking the old directory clean.

Deployment remains gated on the owner merging the reviewed PR and the exact post-merge main artifact passing checks. Local/PR tests do not satisfy that deployment gate. The existing active exchange protection must be re-read immediately before any rollout. Do not cancel an order just to demonstrate a transition when its conditions are false. The Oracle host, off-host backup destination and restore/soak evidence remain separate outstanding work. No cloud resource or paid backup target is created by this change.

GitHub main protection requires PRs, passing checks, signed commits, linear history and resolved review conversations, and prohibits force pushes/deletion. Auto-merge is disabled. Only the owner was a collaborator at inspection. A sole owner cannot approve their own PR; requiring a second review would prevent the requested solo manual merge, so approval count is zero. Owner-controlled credentials remain owner powers; settings cannot distinguish the person from software using those credentials. Secret scanning and push protection are enabled at repository level.

## Shared AWS Testnet recovery preflight

Fresh installation preflight found that the host runs both Bitcoin Testnet and BINANA Testnet while its private configuration selected the isolated single-bot profile. The new explicitly selected `shared-testnet-experiment` profile supports only the Bitcoin Testnet package, permits only those two Testnet projects, and requires positive memory/CPU/PID limits plus a verifiable Testnet executor for the cohost. Its reservation calculation includes the replacement Bitcoin services, monitoring and OS/build headroom. The existing isolated profile still rejects cohosts, the Oracle profile remains the default, and all existing memory/swap/disk minima remain in force. The installer requires 8 GiB free before installation; this must be met rather than overridden. No Live deployment is permitted through this profile.

The legacy AWS configuration also differs from the running containers: external-context enablement/keyless mode and the Testnet fee assumption are provided by overrides. Recovery must preserve those effective values in a private configuration snapshot, validate all other differences, and record the old runtime as unverified rather than manufacture an old release manifest or provenance. The active exchange position must be reconciled before cutover, and the exact post-merge artifact must be verified again on the host.
