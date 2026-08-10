# Security and secrets guide

## Credential boundaries

- `execution-sidecar`: only service with the Binance Spot key and secret.
- `telegram-broker`: trading Telegram token and owner ID; no Binance key.
- `freqtrade`: internal API secrets and signal HMAC; no Binance key.
- `moneyflow`: public endpoints only; no secrets or signed methods.
- monitoring: its own token/reporting bot in a separate mode-specific env; no trading
  env, Binance key, Docker socket, or writable runtime tree.

The Binance key must permit Spot trading only. Disable withdrawals and restrict the
source IP. Never enable margin, futures orders, transfers, universal transfer, or
withdrawal. Rotate a key after any suspected disclosure.

## Storage

Secrets live only in `/etc/bitcoin-bot/.env` (`root:root`, mode 0600) and separate
root/botmon monitor env files. Config snapshots are mode 0600 under a mode-0700
directory and are bound into a success marker by SHA-256. Do not put any private env,
key, evidence private key, database, or logs in Git or the ZIP.

Generate the signal HMAC, command HMAC, Freqtrade password, JWT secret, and WebSocket
token independently. Reuse is rejected. The live Ed25519 private key stays offline;
Oracle receives only its public key.

## Host and network

Use a private GitHub repository and protect `main`; environment-reviewer availability
is plan-dependent and is not the primary deployment control. Keep the dedicated
`gha-runner` outside Docker and privileged groups, separate it from the deployment
account, and require the one-use root-approved artifact hash. GitHub Actions does not
SSH to Oracle. Pin the host key for administrator SSH/SCP access and restrict that
inbound path. Docker group membership is effectively privileged and must be limited
to the deployment account. Expose no Compose ports publicly. Bind monitoring to
loopback and reach it through an administrator SSH tunnel if needed.

Keep Ubuntu, Docker, and the bot dependencies patched through a new reviewed release.
Do not mutate an installed release in place. Preserve clock sync: signatures, candles,
command TTLs, and evidence expiry depend on UTC.

## Incident response

Pause entries first; do not cancel protective sells blindly. Revoke/rotate affected
keys, preserve audit/database/deployment files, enumerate all Binance open orders and
order lists, and reconcile before restarting. If a request outcome is ambiguous,
lookup by deterministic client ID rather than resubmitting it.
