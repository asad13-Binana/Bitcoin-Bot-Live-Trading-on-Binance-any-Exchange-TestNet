# Bitcoin Bot TestNet — Oracle A1 deployment guide

Status: **deployment procedure for owner review; Oracle host validation pending**.

This guide deploys the TestNet package to Oracle Cloud Infrastructure without
changing the protected trading strategy. Infrastructure success is not
profitability evidence and never authorises LIVE trading.

## 1. Supported target

Recommended identity and capacity:

| Setting | Value |
|---|---|
| Home region | Japan East (Tokyo), only if selected by the owner and capacity is available |
| Shape | `VM.Standard.A1.Flex` |
| Architecture | ARM64 / Ampere |
| Operating system | Ubuntu 24.04 LTS ARM64 |
| OCPU | 1 |
| RAM | 6 GB |
| Boot volume | approximately 50 GB |
| Host name | `bitcoin-testnet-tokyo` |
| Product | `BITCOIN-BOT` |
| Environment | `TESTNET` |
| Instance ID | `BITCOIN-TN-TYO-01` |

The host installer rejects non-Ubuntu hosts, Ubuntu releases other than 24.04
by default, non-ARM64 hosts by default, and machines below 1,400 MiB of physical
memory. A larger reviewed ARM64 server remains compatible.

## 2. OCI network and metadata controls

Create the instance with an SSH public key. Never upload the private key.

For a public-IP model, permit inbound TCP 22 only from the narrowest source
CIDR that is operationally possible. Do not add ingress rules for Freqtrade,
Telegram, monitoring, databases or bot services. The Bitcoin monitoring API
binds only to `127.0.0.1:8091`; it is not an OCI public service.

Check both the instance Network Security Group and subnet Security List. A host
firewall is defence in depth, not a replacement for OCI rules. Docker's
published-container traffic can bypass UFW processing, so this release publishes
no container ports. Do not expose the Docker daemon TCP API.

After confirming the Ubuntu image and Oracle agents use IMDSv2, edit the OCI
instance and select **Instance metadata service: Version 2 only**. The bot does
not call OCI metadata. The validation tool reports the v2 and legacy v1 HTTP
status without printing instance metadata.

Official references:

- [OCI Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [OCI instance metadata service](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm)
- [OCI Compute security](https://docs.oracle.com/en-us/iaas/Content/Security/Reference/compute_security.htm)
- [Docker firewall behaviour](https://docs.docker.com/engine/network/packet-filtering-firewalls/)

## 3. SSH hardening without lockout

Keep the original SSH session open and prove a second key-based session works
before disabling password or root login. Add a drop-in instead of editing the
vendor file directly:

```bash
sudoedit /etc/ssh/sshd_config.d/60-bitcoin-bot.conf
```

Owner-reviewed content:

```text
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
```

Validate before reload:

```bash
sudo sshd -t
sudo systemctl reload ssh.service
```

Do not close the first session until a second session succeeds. Ubuntu warns
that an invalid SSH configuration can lock the operator out; the deployment
script deliberately does not make this remote-access decision automatically.

## 4. Prepare the immutable source and artifact

Only deploy a reviewed commit whose GitHub CI passes. Do not deploy the local
working tree or manually fabricated checksums. The workflow writes the exact
commit to `.git-commit`, regenerates the manifest and creates an immutable
tarball plus adjacent SHA-256.

Before the local 13 August candidate is published, review its complete diff,
commit it, push it, and let CI regenerate the final artifact. Until then it is
not an Oracle release artifact.

Download the artifact for that exact commit to the administrator workstation.
Verify the adjacent checksum locally and independently record the complete
tarball SHA-256.

## 5. Run the host bootstrap

Transfer the reviewed repository bootstrap files over SSH, then run as the
normal Ubuntu administrative user:

```bash
chmod +x deploy/*.sh deploy/bitcoin-bot-deploy
DEPLOY_USER="$(id -un)" \
ENABLE_GITHUB_RUNNER=false \
REQUIRED_UBUNTU_VERSION=24.04 \
REQUIRE_ARM64=true \
bash deploy/oracle_setup.sh
```

The bootstrap:

- installs Docker CE from Docker's official Ubuntu `.sources` repository;
- removes conflicting distribution Docker packages only before first install;
- installs Compose and Buildx plugins;
- does not use Docker's convenience script;
- does not perform an unreviewed Docker upgrade on an existing CE host;
- removes permanent Docker-group privilege from the deployment user;
- enables Chrony and requires synchronisation within 0.5 seconds;
- creates an idempotent root-owned 4 GiB swap file when required;
- sets swappiness to 10;
- enables Ubuntu security updates but disables automatic reboot;
- creates the protected Bitcoin namespaces;
- installs the root-owned deployment wrapper, diagnostic, backup tools and
  storage-pressure guard;
- makes the root-owned storage guard produce sanitized container status while
  every monitoring component remains unable to access the Docker socket or CLI;
- leaves the GitHub runner application and runner sudo policy disabled by
  default.

To make a controlled Docker upgrade later, first list reviewed versions and
then rerun with an exact version string:

```bash
apt-cache madison docker-ce
sudo DOCKER_VERSION='EXACT_VERSION_FROM_APT_CACHE' \
  DEPLOY_USER="$(id -un)" bash deploy/oracle_setup.sh
```

Do not use an unreviewed major-version upgrade.

## 6. Create the private environment

Edit the root-only file:

```bash
sudoedit /etc/bitcoin-bot/.env
sudo chown root:root /etc/bitcoin-bot/.env
sudo chmod 0600 /etc/bitcoin-bot/.env
sudo stat -c '%U:%G %a %n' /etc/bitcoin-bot/.env
```

Copy keys from `.env.example`, never the example placeholder secrets. Required
non-secret identity:

```text
BOT_PRODUCT=BITCOIN-BOT
BOT_ENVIRONMENT=TESTNET
BOT_INSTANCE_ID=BITCOIN-TN-TYO-01
```

First deployment:

```text
EXECUTION_MODE=simulation
LIVE_TRADING_ENABLED=false
AUTO_CONFIRM=false
BINANCE_API_KEY=
BINANCE_API_SECRET=
MONITOR_BIND_HOST=127.0.0.1
MONITOR_PORT=8091
```

Create independent random service-bus and Freqtrade secrets. Add the Telegram
bot token and owner private-chat ID. Keep CoinGecko and CoinMarketCap providers
disabled unless their dedicated credentials are supplied. Never place any
credential in GitHub, shell history, command arguments, screenshots or reports.

The privileged parser treats every environment value as data. It rejects
symlinks, unsafe ownership/mode, malformed or duplicate keys, unapproved keys,
placeholders and shell-injection strings. It never sources or evaluates `.env`.

## 7. Stage and approve one exact artifact

With the optional GitHub runner disabled, use the isolated staging identity
without granting it Docker or secret access:

```bash
sudo install -m 0600 -o gha-runner -g gha-runner \
  bitcoin-bot-COMMIT.tar.gz \
  /var/lib/bitcoin-bot/incoming/bitcoin-bot-release.tar.gz
sudo install -m 0600 -o gha-runner -g gha-runner \
  bitcoin-bot-COMMIT.tar.gz.sha256 \
  /var/lib/bitcoin-bot/incoming/bitcoin-bot-release.tar.gz.sha256
sudoedit /etc/bitcoin-bot/approved-artifact.sha256
sudo chown root:root /etc/bitcoin-bot/approved-artifact.sha256
sudo chmod 0600 /etc/bitcoin-bot/approved-artifact.sha256
```

The approval file contains exactly one independently verified 64-character
tarball SHA-256. Then run:

```bash
sudo /usr/local/sbin/bitcoin-bot-deploy preflight
sudo /usr/local/sbin/bitcoin-bot-deploy simulation
sudo /usr/local/sbin/bitcoin-bot-deploy verify
```

Approval is consumed before installation and cannot be silently replayed.

## 8. Validate the Oracle host

Run the redacted diagnostic:

```bash
sudo /usr/local/sbin/bitcoin-bot-oracle-validate \
  | sudo tee /var/log/bitcoin-bot/oracle-validation.txt
```

It reports identity, OS, architecture, kernel, CPU, RAM, swap, disk, Docker,
Compose, Chrony, services, containers, health, resource limits, loopback ports,
release identity, execution mode and reachability. It performs at least ten
HTTPS calls to Binance Testnet `/api/v3/time` and calculates DNS, connect, TLS,
first-byte and total min/median/p95/max. It does not use ICMP as API proof and
does not print credentials.

Confirm monitoring is loopback-only:

```bash
sudo ss -ltnp | grep ':8091'
curl --fail http://127.0.0.1:8091/api/v1/health
```

The authenticated monitoring check requires the bearer token and should be run
without recording that token in shell history.

## 9. Backup and restore validation

Create a root-only backup:

```bash
sudo /usr/local/libexec/bitcoin-bot/backup_state.sh
sudo /usr/local/libexec/bitcoin-bot/verify_backup.sh \
  /var/backups/bitcoin-bot/YYYYMMDDTHHMMSSZ
```

The backup uses SQLite's online backup API and `PRAGMA quick_check`, captures
audit/deployment/config snapshots, rejects symlinked content and writes a full
checksum inventory. It can contain private configuration and must remain mode
`0700/0600` on the Oracle host. Encrypt it before any off-host transfer.

Restore is deliberately not automatic. During a maintenance window: stop the
stack, verify the backup, preserve the failed state, restore only the selected
database/config generation, rerun SQLite `quick_check`, then start in simulation
and reconcile before TestNet resumes.

## 10. Failure and reboot acceptance

Retain redacted evidence for:

1. Docker service restart;
2. each container crash and health recovery;
3. VM reboot;
4. network and DNS loss;
5. Binance Testnet and Telegram failure;
6. memory pressure/OOM;
7. disk-warning and disk-critical resource-guard behaviour;
8. corrupt and incomplete artifact rejection;
9. backup validation;
10. three verified rollback cycles.

After every test prove that `RELEASE_MODE=testnet`, execution remains
`simulation` or `testnet`, `LIVE_TRADING_ENABLED=false`, `AUTO_CONFIRM=false`,
and the protected strategy hash is unchanged.

## 11. Authenticated TestNet is a separate acceptance gate

Only after simulation validation, create a dedicated Binance Spot Test Network
key. Use TestNet endpoints, Spot-only permission and no withdrawal capability.
Do not enlarge `recvWindow` to hide poor time synchronisation; Binance recommends
5,000 ms or less.

Authenticated TestNet order creation, cancellation, user-data-stream handling,
unknown-order reconciliation, Telegram confirmations, reboot recovery and soak
remain **pending until performed on the actual Oracle host**. None of these
results authorises LIVE money.

## 12. Prohibited actions

- Do not set `EXECUTION_MODE=live` in this package.
- Do not use a production Binance execution endpoint.
- Do not enable withdrawals on any API key.
- Do not expose port 8091, Freqtrade, databases or Docker publicly.
- Do not add the deploy, runner or botmon account to the Docker group.
- Do not deploy a working tree or an artifact whose digest is not independently approved.
- Do not describe the strategy as profitable or real-money ready.

The recorded strategy profitability gate remains failed and unchanged.
