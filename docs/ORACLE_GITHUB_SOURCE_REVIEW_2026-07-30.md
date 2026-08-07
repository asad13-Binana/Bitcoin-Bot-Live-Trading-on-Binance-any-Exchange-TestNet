# Oracle and GitHub source review — 30 July 2026

## Verdict

The supplied links do not contain trading logic that belongs in this bot.
Only deployment facts and security lessons were adopted. The protected
`IctSmcStrategy.py`, signal formula, indicators, entry conditions, exits,
position sizing, and order policy were not changed.

The current strategy SHA-256 remains:

`023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340`

The canonical backtest remains failed. Oracle or GitHub deployment cannot turn
that result into a live-trading approval.

## Sources inspected

| Source | Inspected identity | Decision |
|---|---|---|
| `mohankumarpaluru/oracle-freetier-instance-creation` | `main` commit `abbe2cc7d508b36e2c196c4236fad40e6234800e` | Useful only as an optional, separate OCI Compute capacity helper. Not copied or executed. |
| GitHub Marketplace `Setup-Oracle-DB-Free` | Marketplace version `v1.2.0` | Not relevant. It starts an Oracle Database container in a Linux GitHub Actions runner. |
| `gvenzl/setup-oracle-free` | `main`, `v1`, and `v1.2.0` commit `a8df5179017826f38de252982830cd9caab03bae` | Same implementation as the Marketplace action. Rejected for this bot. |
| Oracle Always Free documentation | Live review on 30 July 2026 | Used for current A1 limits and capacity warnings. |
| Oracle IMDS and Compute security documentation | Live review on 30 July 2026 | Used for IMDSv2 and metadata-access guidance. |
| GitHub Actions security documentation | Live review on 30 July 2026 | Existing full-commit action pinning retained. |

## Useful findings adopted

### 1. Current Oracle Always Free sizing

Oracle currently documents 1,500 OCPU hours and 9,000 GB-hours each month for
`VM.Standard.A1.Flex`, equivalent to **2 OCPU and 12 GB RAM total** for an
Always Free tenancy. The resource must be created in the tenancy home region,
and capacity is not guaranteed.

The recommended host for this four-service Docker stack is one A1 Flex instance
using 2 OCPU and 12 GB RAM. `VM.Standard.E2.1.Micro` has only 1 GB RAM and is
below this release's 1,400 MiB physical-memory gate, so it is not a supported
full-stack target.

### 2. ARM64 container compatibility

Registry manifests were inspected without running the bot:

- `freqtradeorg/freqtrade:2026.6@sha256:d451af021d5e08b70580c0eea5848534e9846b57391b34821c0a5814416397e6`
  includes `linux/amd64`, `linux/arm64`, and `linux/arm/v7`.
- `python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`
  includes `linux/amd64` and `linux/arm64/v8`.

Those are the exact multi-platform digests already pinned by this release. No
image tag or runtime version was changed.

### 3. Fail early on an unsuitable host

`deploy/oracle_setup.sh` now accepts only `amd64` or `arm64` Ubuntu hosts and
fails before package installation when physical RAM is below 1,400 MiB. This
makes the existing install-time resource requirement visible during host setup.

### 4. OCI host-security guidance

Use a current Ubuntu platform image, verify IMDSv2 support, and then configure
the instance for IMDSv2 only. Restrict SSH through OCI networking and the host
firewall. The bot and monitor require outbound access but no public inbound HTTP
port. Do not expose SSH to `0.0.0.0/0` merely to make GitHub-hosted deployment
convenient.

## Why the OCI instance-creation repository was not integrated

That project automates creation of an OCI Compute instance when capacity is
temporarily unavailable; it does not install or operate this bot. Its retry and
shape-selection concepts are informative, but the inspected checkout is not a
safe dependency for the bot release:

- runtime dependencies are unpinned;
- `oci.env`, `oci_config`, PEM/API keys, and generated private keys are not all
  protected by repository ignore rules;
- the interactive setup reads notification secrets visibly and writes a
  plaintext environment file without an explicit restrictive mode;
- `setup_init.sh` sources that file as shell code;
- launch attempts can continue without a fixed attempt or elapsed-time ceiling;
- the launch request explicitly keeps legacy IMDSv1 enabled;
- notification tokens and OCI identifiers create additional secret/logging
  exposure.

If an operator independently chooses to use it to obtain capacity, it must stay
outside the bot repository and release archive. Use a separate working
directory, a least-privilege OCI principal, mode-0600 credential files,
notifications disabled, a bounded retry window, and IMDSv2-only configuration.
Stop and revoke the provisioning credentials once the instance exists.

## Why the Marketplace action and database repository were rejected

The Marketplace page and `gvenzl/setup-oracle-free` are the same Oracle
Database Free GitHub Action. The bot uses local SQLite state and does not need
Oracle Database. Adding the action would increase CI time, memory use, attack
surface, and secret handling without testing a bot dependency.

The inspected action also defaults its database image to `latest`, changes a
mounted directory to mode `0777`, builds a shell command using string
concatenation and `eval`, and prints the command containing database password
arguments. It is therefore not used in this release.

For an unrelated project that genuinely needs this action, review the source,
use masked GitHub secrets, pin the action to the full reviewed commit
`a8df5179017826f38de252982830cd9caab03bae`, and pin the database image rather
than using `latest`.

## Binance compatibility recheck

A read-only Binance plugin request on 30 July 2026 returned `BTCUSDT` as
`TRADING` with Spot trading allowed. The response included a price tick of
`0.01`, lot step of `0.00001`, minimum notional of `5 USDT`, and the expected
Spot order/protection capabilities. No credentials, account data, or order
operation were used.

This is a point-in-time compatibility observation. The sidecar must continue to
fetch current exchange and execution rules before money-moving requests.

## External evidence still required

This review does not prove:

- a successful user-owned GitHub Actions run;
- a Docker build or four-service runtime on the target A1 host;
- OCI networking, SSH, systemd, backup, rollback, restart, OOM, or disk-full
  behavior;
- an authenticated Binance Spot Testnet lifecycle;
- real Telegram delivery;
- a fourteen-day Oracle simulation/Testnet soak;
- strategy profitability or live readiness.

## References

- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)
- [Oracle instance metadata service](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm)
- [Oracle Compute security](https://docs.oracle.com/en-us/iaas/Content/Security/Reference/compute_security.htm)
- [GitHub secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)
- [OCI instance-creation repository](https://github.com/mohankumarpaluru/oracle-freetier-instance-creation)
- [Setup-Oracle-DB-Free Marketplace action](https://github.com/marketplace/actions/setup-oracle-db-free)
- [Setup Oracle Database Free source](https://github.com/gvenzl/setup-oracle-free)
