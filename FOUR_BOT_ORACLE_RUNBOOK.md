# Four-bot Oracle co-host runbook

This repository is one member of a four-instance deployment. The protected
strategy, indicators, entry/exit logic, Sharia rules, position sizing and
legacy core are not changed by this host-isolation layer.

## Fixed instance allocation

| Instance | Compose project/image prefix | Private configuration | Persistent state | Monitor |
| --- | --- | --- | --- | --- |
| Binana TestNet | `binana-testnet` | `/etc/binana-testnet` | `/var/lib/binana-testnet` | `127.0.0.1:8090` |
| Bitcoin TestNet | `bitcoin-testnet` | `/etc/bitcoin-testnet` | `/var/lib/bitcoin-testnet` | `127.0.0.1:8091` |
| Binana LIVE | `binana-live` | `/etc/binana-live` | `/var/lib/binana-live` | `127.0.0.1:8092` |
| Bitcoin LIVE | `bitcoin-live` | `/etc/bitcoin-live` | `/var/lib/bitcoin-live` | `127.0.0.1:8093` |

The canonical machine-readable allocation is
`deploy/four_bot_host_contract.json`. Do not change one repository's copy
alone. All four copies must remain byte-identical.

## Oracle host minimum

Use one Ubuntu 24.04 ARM64 A1 Flex VM configured with at least 2 OCPUs,
12 GB physical memory and 80 GB free disk before installation. The four
Compose stacks are capped at 1.80 CPUs and 5,140 MiB combined. A shared 4 GiB
swap file is emergency headroom, not a substitute for physical memory.

Oracle's current Always Free documentation is the authority for available
shape and account limits:

- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm

Availability and tenancy limits can change. Confirm the console allocation
before creating the VM.

## Credential isolation is mandatory

Create and store credentials only in the four root-owned private `.env`
files. Never put real values in Git, archives, CI variables, screenshots or
chat.

- Use a different Telegram BotFather token for every trading-bot instance.
  Telegram long polling by two processes with one token is unsafe and can
  cause update conflicts.
- Use independent signal and command HMAC keys for every instance.
- Use a different Binance account or subaccount and API key for every bot that
  is not in simulation. TestNet bots need separate TestNet keys; LIVE bots need
  separate production subaccounts and keys. Disable withdrawals and restrict
  production keys to the Oracle public IP.
- CoinGecko and CoinMarketCap free keys may be used, but either allocate
  separate keys or calculate and enforce the combined request budget across
  all enabled instances. Four independent local rate limiters do not create
  four times the provider quota.
- Use a separate monitor Telegram bot token where Telegram monitor reports
  are enabled.

## Installation order

1. Keep all LIVE repositories in simulation mode.
2. Clone the four repositories into four different source directories.
3. Run each repository's `deploy/oracle_setup.sh`. It creates only that
   repository's dedicated users, paths, wrappers, locks and units.
4. Populate each private `.env` with `sudoedit`. Use the exact BOT_UID and
   BOT_GID printed by its setup script.
5. Deploy Binana TestNet, then Bitcoin TestNet.
6. Deploy Binana LIVE in simulation, then Bitcoin LIVE in simulation.
7. From any current checkout, run:
   `sudo python3 scripts/verify_four_bot_cohost.py --host`
8. Run each repository's redacted API preflight and Oracle diagnostic.
9. Complete authenticated TestNet order-lifecycle, reboot, network-loss,
   disk-pressure, restore and soak tests before considering LIVE promotion.

The host validator never prints credentials or their hashes. It fails when
paths, users or wrappers are missing, secrets are reused, LIVE Binance keys
are reused, resources are insufficient, or a legacy generic bot project is
still present.

## Monitoring

Keep all monitor APIs bound to loopback. Reach them through SSH tunnels, for
example `ssh -L 8090:127.0.0.1:8090 -L 8091:127.0.0.1:8091
-L 8092:127.0.0.1:8092 -L 8093:127.0.0.1:8093 user@oracle-host`.
Each API requires its own bearer token.

## Acceptance boundary

Passing source tests and the static co-host validator proves namespace and
configuration isolation. It does not prove that one particular Oracle VM can
run four market-data workloads continuously. Require a sustained all-four
soak on the real VM and inspect CPU throttling, memory pressure, disk growth,
API quotas, websocket stability, reconciliation and Telegram delivery.

LIVE-money activation remains a separate decision. It requires authenticated
TestNet lifecycle evidence, recovery evidence and fee/slippage-aware
performance evidence. Co-host safety does not prove profitability.
