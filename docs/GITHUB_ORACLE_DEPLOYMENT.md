# GitHub and Oracle simulation deployment runbook

For the current Ubuntu 24.04 ARM64 A1 host baseline, optional-runner design,
monitoring port 8091, backup/resource guard and redacted diagnostic, use
[`ORACLE_SETUP_GUIDE.md`](ORACLE_SETUP_GUIDE.md). This document remains the
GitHub Actions simulation-artifact workflow reference.

This is the supported deployment path for the mode-separated Bitcoin Spot bot.
The downloadable ZIP is a source distribution. GitHub Actions verifies it and
creates the immutable `bitcoin-bot-<commit>.tar.gz` consumed by the Oracle
installer.

The exact packaged strategy failed its mandatory profitability gate. Therefore
this workflow is deliberately restricted to `simulation`, requires empty
Binance credentials and starts with entries off. It is an infrastructure drill,
not authorization for Testnet or real-money trading.

## 1. Create and protect the private GitHub repository

1. Create an empty **private** repository and upload the extracted ZIP contents
   so `README.md`, `.github/`, `deploy/`, and `docker-compose.yml` are at its root.
2. Copy `.github/CODEOWNERS.example` to `.github/CODEOWNERS`, replace
   `@YOUR_GITHUB_USERNAME`, and commit it.
3. Protect `main`: require the `verify` matrix and `artifact` job, require code
   owner review, block force pushes, and restrict who can push.
4. Keep Actions limited to trusted, fully pinned actions. Do not let pull
   requests from forks or untrusted collaborators target the Oracle runner.
5. Add repository variable `ORACLE_DEPLOY_ENABLED=false`. Change it to `true`
   only while performing an approved simulation deployment.

Required environment reviewers must not be treated as the primary control for a
private repository because availability depends on the GitHub plan. This release
instead uses independent main-branch, manual-dispatch, exact-confirmation,
repository-variable, server-wrapper, one-use artifact-digest and simulation-mode
gates.

Pushes and pull requests verify/package on isolated GitHub-hosted runners; they
never deploy. The Oracle job runs only after manual dispatch from `main` with:

- `deploy_oracle=true`
- `execution_mode=simulation`
- `confirmation=SIMULATION_ONLY`
- repository variable `ORACLE_DEPLOY_ENABLED=true`

The Oracle job does not contain an SSH key and does not open inbound SSH to
GitHub-hosted runner address ranges.

## 2. Prepare the Oracle host once

Use an Oracle Ubuntu 24.04 ARM64 Ampere A1 Flex instance. The
complete four-service stack requires at least 1,400 MiB physical RAM and 3,800
MiB swap; the 1 GB E2.1.Micro shape is intentionally rejected. Free capacity is
not guaranteed.

Restrict the VCN, security list and host firewall to SSH from the administrator's
own path. The bot and runner need outbound HTTPS. No application HTTP port needs
to be public. Verify that the selected image supports IMDSv2, and configure
IMDSv2-only. Do not install Oracle Database; this bot uses local SQLite state.

For the initial bootstrap only, transfer the final release ZIP and its adjacent
`.sha256` from the Desktop by the administrator. Verify the external ZIP hash,
freshly extract it, then verify the internal manifest before running any setup:

```bash
sha256sum -c BITCOIN_BOT_LIVE_GITHUB_ORACLE_2026-07-30.zip.sha256
python3 -m zipfile -e BITCOIN_BOT_LIVE_GITHUB_ORACLE_2026-07-30.zip bootstrap
cd bootstrap/bitcoin-bot-live-trading
python3 scripts/verify_manifest.py
python3 tests/secret_scan.py
chmod +x deploy/oracle_setup.sh
sudo -v
DEPLOY_USER="$(id -un)" ENABLE_GITHUB_RUNNER=false bash deploy/oracle_setup.sh
```

The setup configures the already selected deployment user and creates:

- the trusted deployment user and existing Docker-based release directories;
- `/etc/bitcoin-bot/.env`, mode `0600`, owned by `root:root`;
- dedicated `gha-runner`, separate from the deployment user;
- `/var/lib/bitcoin-bot/incoming`, mode `0700`, owned by `gha-runner`;
- root-only staging and `/etc/bitcoin-bot/approved-artifact.sha256`;
- root-owned `/usr/local/sbin/bitcoin-bot-deploy`;
- an optional sudoers rule for exactly `preflight`, `simulation`, and `verify`
  only when `ENABLE_GITHUB_RUNNER=true` is explicitly selected.

The runner is not in `docker`, `sudo`, `adm`, `lxd`, `disk`, or `root`. The
Docker group is root-equivalent; never add `gha-runner` to it. The runner also
cannot read `/etc/bitcoin-bot/.env`.

## 3. Create the private simulation configuration

Populate `/etc/bitcoin-bot/.env` from `.env.example` through `sudo`; it must
remain `root:root` mode `0600`. Do not commit, upload, print or place this file
in GitHub secrets:

```bash
sudo install -m 0600 -o root -g root .env.example /etc/bitcoin-bot/.env
sudoedit /etc/bitcoin-bot/.env
sudo chown root:root /etc/bitcoin-bot/.env
sudo chmod 0600 /etc/bitcoin-bot/.env
```

Generate every HMAC/API secret independently. For the self-hosted simulation
workflow, retain:

```text
EXECUTION_MODE=simulation
LIVE_TRADING_ENABLED=false
AUTO_CONFIRM=false
AUTO_PROTECTION_ENABLED=false
BINANCE_API_KEY=
BINANCE_API_SECRET=
SHARED_HOST_PATH=/var/lib/bitcoin-bot/shared
```

Set exact numeric `BOT_UID` and `BOT_GID` for the deployment user. The persistent
`entries_enabled` latch is runtime state, not an `.env` key; the installer and
sidecar initialize it to false.

The public signal/money-flow services intentionally use production public market
data in simulation. That is not authenticated execution. For later Spot Test
Network work, this bot's `BINANCE_SPOT_EXECUTION_PUBLIC_BASE` expects the origin
`https://testnet.binance.vision` because the client appends `/api/v3`; do not put
the trailing `/api` into this bot-specific variable.

## 4. Optionally register the dedicated self-hosted runner

The preferred manual immutable-artifact path leaves the runner application and
runner sudo policy disabled. Follow this section only when the owner explicitly
accepts the persistent-runner risk and reruns host setup with
`ENABLE_GITHUB_RUNNER=true`.

In the private repository, open:

`Settings → Actions → Runners → New self-hosted runner`

Select the Oracle host architecture and use the exact current commands generated
by GitHub. Run installation and registration as `gha-runner`, not as root or the
bot deployment user, and add the custom label:

```text
oracle-sim
```

Install the runner as a service using GitHub's generated service command. Do not
store the short-lived registration token in a file or in this repository. Keep
the operating system and runner updated; GitHub can stop queuing jobs to an
outdated self-hosted runner.

A persistent self-hosted runner is not an isolated clean VM. Keep this repository
private, limit collaborators, protect `main`, avoid untrusted third-party actions,
and stop the runner service when no deployment is planned if operationally
practical.

## 5. Approve one exact CI artifact out of band

The root wrapper does not trust a manifest and checksum merely because they came
from the same repository. It also requires the complete tarball SHA-256 to have
been placed in a root-owned file by the administrator.

Use a two-pass process:

1. Run the workflow with `deploy_oracle=false`.
2. Download the `bitcoin-bot-<commit>` workflow artifact.
3. Verify its adjacent `.sha256` locally and record the full tarball digest.
4. On Oracle, use `sudoedit` to place only that 64-character digest in:

```text
/etc/bitcoin-bot/approved-artifact.sha256
```

5. Confirm the file is root-owned and mode `0600`.
6. Re-run the workflow for the **same main-branch commit** with the three manual
   values listed in section 1.

The artifact build is deterministic for the same commit. The wrapper copies the
runner-owned files into root-only staging, checks the adjacent checksum, checks
the independent root-approved digest, safely extracts the archive, verifies the
manifest, secret scan and full protected-strategy hash, and validates the private
simulation environment.

Immediately before installation it consumes the approval. A failed or repeated
deployment requires the administrator to approve the digest again; a workflow
cannot silently replay an old approval.

The workflow never replaces the root-owned wrapper or root-managed installer.
If either deployment program changes in a future reviewed release, repeat the
manual checksum/manifest bootstrap and rerun `oracle_setup.sh` as an
administrator before enabling that release's workflow.

## 6. What the three restricted wrapper commands do

```bash
sudo /usr/local/sbin/bitcoin-bot-deploy preflight
sudo /usr/local/sbin/bitcoin-bot-deploy simulation
sudo /usr/local/sbin/bitcoin-bot-deploy verify
```

- `preflight` performs read-only host, secret-separation, artifact, manifest,
  strategy-integrity and simulation-policy checks.
- `simulation` repeats those checks, consumes the one-use root approval, then
  calls the existing transactional installer. The installer validates all
  detailed environment and release requirements before stopping the old stack.
- `verify` verifies the current manifest, exact four container/service/image/
  release/config identity, deployment and monitoring gates, fresh application
  health, one BTC pair, simulation state, zero unresolved intents, and
  `entries_enabled=false`.

The wrapper rejects empty, `testnet`, `live`, `production`, and all unknown
actions. It cannot edit the strategy or resume entries.

## 7. Verify from Oracle and Telegram

After a successful workflow:

```bash
readlink -f /opt/bitcoin-bot/current
sudo /usr/local/sbin/bitcoin-bot-deploy verify
cat /var/lib/bitcoin-bot/shared/runtime/deployment_status.json
cat /var/lib/bitcoin-bot/shared/runtime/release_validation.json
cat /var/lib/bitcoin-bot/shared/runtime/sidecar/sidecar_health.json
```

In the owner-only private Telegram chat, use:

```text
/audit
/status
/pair
/flow
/deploy
/backtest
```

`/audit` is read-only. It requires all release-hash, release-path and execution-
mode records to agree; checks service health freshness, Freqtrade ping, an idle
pair-switch state, BTC pair consistency across three records, zero unresolved
intents and entries-off state. It never repairs, starts or resumes the bot.

Set `ORACLE_DEPLOY_ENABLED=false` after the deployment window and retain the
redacted workflow, installer, health and restart evidence.

## 8. Testnet and live remain separate blocked promotions

Binance Spot Test Network keys are generated at the Spot Test Network website,
use virtual assets and Testnet endpoints, and Testnet state is periodically
reset. Testnet execution is a lifecycle drill, not realistic fill-quality or
profitability evidence.

This self-hosted workflow intentionally does not offer Testnet or live choices.
The Testnet package is ready for source upload and future controlled lifecycle
work, but an authenticated Testnet workflow must be separately reviewed and
approved. The live package remains uncertified because the exact strategy
backtest failed. No deployment success overrides `LIVE_PROMOTION_CHECKLIST.md`.

See `GITHUB_RELEASE_AND_ROLLBACK_GUIDE.md`,
`EXTERNAL_VALIDATION_RUNBOOK.md`, and `LIVE_PROMOTION_CHECKLIST.md`.
