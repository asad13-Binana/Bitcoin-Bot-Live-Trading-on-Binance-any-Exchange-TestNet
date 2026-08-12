# Oracle deployment hardening report — 13 August 2026

Classification: **review only — local TestNet candidate, not committed or pushed**.

## Executive verdict

The TestNet repository's Oracle infrastructure has been hardened locally without
editing the trading strategy, trading services, pair policy, risk logic,
position sizing, entries, exits, indicators, thresholds, protections,
profitability evidence or LIVE-promotion gate.

The source-level release and security gates pass. Oracle A1 runtime validation,
authenticated Binance TestNet lifecycle validation, Telegram delivery, reboot,
failure, rollback and soak evidence remain pending because no Oracle host or
private credentials were available for this local change.

Infrastructure hardening does not make the strategy profitable or suitable for
real money. LIVE promotion remains prohibited.

## Baseline and scope

| Item | Recorded value |
|---|---|
| Repository | `asad13-Binana/Bitcoin-Bot-Live-Trading-on-Binance-any-Exchange-TestNet` |
| Branch | `main` |
| Starting HEAD | `cf9447ae6782b2c7faaa3305cd320991cd05ede3` |
| Starting status | clean, tracking `origin/main` |
| Release mode | `testnet` |
| Default execution | `simulation` |
| LIVE certified | false |
| Local working state | modified, uncommitted, not pushed |
| LIVE reference HEAD | `e3771aeba67061ea343301235e652aa99154bff6` |
| LIVE reference status | clean and untouched |

The local candidate is in:

```text
C:\Users\asadm\Desktop\Bitcoin Codex Working\oracle-hardening-2026-08-13\bitcoin-bot-testnet
```

The separate LIVE checkout is a read-only comparison reference. It has no local
change.

## Protected strategy proof

Full strategy SHA-256 before hardening:

```text
023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340
```

Full strategy SHA-256 after hardening:

```text
023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340
```

`git diff` is empty for:

```text
services/
freqtrade/user_data/strategies/
freqtrade/user_data/config.json
```

All four canonical strategy signal fingerprints continue to equal their
expected values. The LIVE reference has the same full strategy hash.

Result: **protected strategy and trading application code are byte-for-byte
unchanged**.

## Findings and fixes

| Severity | Finding | Local remediation |
|---|---|---|
| High | The normal deployment account was permanently added to the root-equivalent Docker group even though a narrow root wrapper already existed. | Removed Docker-group membership and made the root-owned, digest-verifying wrapper the privilege boundary. Setup now fails if the deployment account retains Docker, LXD, disk or root group membership. |
| High | A persistent self-hosted runner increased the impact of arbitrary workflow execution on the trading VM. | Runner registration and runner sudo policy are opt-in with `ENABLE_GITHUB_RUNNER=false` by default. The isolated account remains only as a non-privileged artifact-staging identity. |
| Medium | Docker used the older one-line `.list` apt-repository format and did not handle all currently documented conflicts. | Implemented Docker's current official deb822 `.sources` format, refreshed the official ASCII key, removed the obsolete list and handles Docker/Compose/Buildx/Podman/containerd/runc conflicts only before first CE installation. |
| Medium | Re-running setup could blindly upgrade Docker. | Existing Docker CE is retained unless an exact reviewed `DOCKER_VERSION` is supplied. Initial installation uses the official package family. |
| Medium | Simulation/TestNet monitoring defaulted to port 8090, colliding with the separate Binana bot. | Changed every Bitcoin monitoring default and URL to loopback `127.0.0.1:8091`; added numeric, loopback and occupied-port fail-closed validation. |
| Medium | Compose had memory limits but no explicit CPU or PID ceilings. | Added CPU and PID limits to all four services; aggregate CPU ceilings total 0.95 OCPU, leaving capacity for Ubuntu, Docker and administration. Existing bounded Docker logs, capabilities, health checks and non-root controls were retained. |
| Medium | There was no root fail-safe for disk or inode exhaustion. | Added a one-minute, root-only systemd resource guard with 85% warning and 95% critical defaults. At critical pressure it stops only the four label-verified Bitcoin project services and writes a redacted status record. |
| High | The inherited monitoring snapshot helper mounted the Docker Unix socket read-only. A read-only socket mount still permits Docker API commands and therefore remained root-equivalent runtime access. | Deleted the privileged monitoring helper. The root-owned deployment safety guard now produces the narrow, sanitized container-status file; the `botmon` verification unit only reads that file and has neither the Docker socket nor Docker CLI. |
| Medium | Host diagnostics did not cover the complete target or real HTTPS latency phases. | Added a redacted Oracle diagnostic covering host, Docker, Compose, Chrony, services, health, limits, ports, IMDS, providers and release identity, plus at least ten Binance Testnet HTTPS samples with DNS/connect/TLS/first-byte/total min, median, p95 and max. |
| Medium | Backup/restore evidence was incomplete. | Added root-only online SQLite backup, `PRAGMA quick_check`, config/audit/runtime snapshots, symlink rejection, checksums and non-destructive archive/restore validation. Off-host copies must be encrypted. |
| Medium | Package identity was implicit. | Added configurable `BOT_PRODUCT=BITCOIN-BOT`, `BOT_ENVIRONMENT=TESTNET` and `BOT_INSTANCE_ID=BITCOIN-TN-TYO-01`, propagated them into every service, and rejects TestNet identity or production execution-endpoint mismatch. |
| Low | Host target allowed multiple Ubuntu releases and architectures by default. | Default target is now Ubuntu 24.04 ARM64 for Oracle A1, with explicit reviewed override variables retained for portability. |
| Low | Security update and reboot behaviour were not explicit. | Ubuntu automatic security updates are enabled while automatic reboot is explicitly disabled. Docker's third-party repository is not added to unattended upgrades. |
| Low | Chrony presence was checked without an explicit wait/offset acceptance gate. | Added `chronyc waitsync` with a default maximum 0.5-second offset before deployment. Binance `recvWindow` remains 5,000 ms; time quality is not hidden by a large window. |

## Docker and container posture

The official Ubuntu repository method uses:

```text
docker-ce
docker-ce-cli
containerd.io
docker-buildx-plugin
docker-compose-plugin
```

The setup does not use `get.docker.com` and never enables a Docker daemon TCP
listener. Docker access is through the root-owned Unix socket and narrow root
programs. The deploy user, `gha-runner` and `botmon` are not Docker-group
members. No monitoring component receives the Docker socket or Docker CLI.
The root-owned resource guard is the sole scheduled component with Docker
control; it validates the Compose project and service labels before any
critical-pressure stop, and writes only sanitized status for `botmon`.

Compose retains four exact services:

```text
moneyflow
freqtrade
execution-sidecar
telegram-broker
```

No container port is published. Every service has a memory, CPU and PID limit,
health check, bounded JSON-file logging and restart policy. Existing capability
drops and `no-new-privileges` were retained. The three custom Python services
remain read-only with tmpfs. Freqtrade's existing writable exception remains
because blindly making its image root filesystem read-only was not proven safe
without an Oracle/Docker runtime test. Docker's default seccomp/AppArmor posture
is retained; custom profiles require target-host validation before enforcement.

## Secret and configuration handling

The private environment remains:

```text
/etc/bitcoin-bot/.env
owner root:root
mode 0600
```

The parser treats configuration as literal data. It does not source or evaluate
the environment file. It rejects symlinks, unsafe ownership/mode, malformed and
duplicate assignments, unapproved keys, placeholders and process-control keys.

The diagnostic, backup output and reports do not print Binance, Telegram,
CoinGecko, CoinMarketCap, HMAC, JWT, WebSocket or monitor secrets. Backup
configuration snapshots remain root-only and require encryption before leaving
the host.

## Identity and endpoint enforcement

The local TestNet package requires:

```text
BOT_PRODUCT=BITCOIN-BOT
BOT_ENVIRONMENT=TESTNET
BOT_INSTANCE_ID=BITCOIN-TN-...
```

The TestNet installer accepts only simulation or TestNet execution and rejects
a non-Testnet Binance execution endpoint. The optional self-hosted workflow
remains simulation-only, requires empty Binance credentials, keeps entries off
and accepts only a one-use independently approved artifact digest.

## Chrony, swap and operating-system controls

- Chrony is enabled and must report synchronisation within 0.5 seconds.
- A root-owned mode-0600 four-GiB swap file is created idempotently only when
  total swap is below the required level.
- `/etc/fstab` duplication is prevented.
- Swappiness remains 10.
- Ubuntu security updates remain automatic.
- Automatic reboot remains disabled and requires an owner-controlled maintenance
  window.
- SSH hardening is documented with key-only access, no root/password login,
  `sshd -t` validation and a second-session lockout check; it is not applied
  blindly by the script.

## OCI network and metadata posture

The deployment guide requires OCI ingress for SSH only from the narrowest
operator CIDR. Bot, monitoring, Freqtrade, database and Docker ports must not be
public. It explicitly records Docker's UFW bypass behaviour for published ports.

The bot has no metadata dependency. The diagnostic checks IMDSv2 with the
required `Authorization: Bearer Oracle` header and reports whether legacy v1
returns 404. OCI IMDSv2-only remains an operator-side instance setting after
image compatibility is confirmed.

## Monitoring, logs and disk

- Bitcoin monitoring defaults to `127.0.0.1:8091` and remains configurable.
- Occupied monitoring ports fail before service start.
- Monitoring reads sanitized container state generated by the root-owned
  deployment safety guard; its systemd units have no Docker socket or CLI.
- Docker logs remain `10m × 3` per container.
- Host Freqtrade/monitor log files have a 10-MiB, seven-generation compressed
  logrotate policy.
- Audit JSONL and reconciliation evidence retain their existing application
  retention and are excluded from destructive logrotate rules.
- The resource guard records OK/WARNING/CRITICAL state and stops only recognised
  Bitcoin services at critical pressure.

## Backup and restore

The root backup captures:

- execution and provider SQLite state through SQLite's online backup API;
- post-copy SQLite `quick_check` evidence;
- audit evidence;
- runtime/deployment metadata;
- configuration snapshots;
- complete SHA-256 inventory.

The verifier accepts only canonical direct timestamp children of
`/var/backups/bitcoin-bot`, checks every digest, reruns SQLite integrity and
rejects absolute, traversal or Windows-style archive paths. It never restores
data automatically. A restore remains an explicit stopped-stack maintenance and
reconciliation operation.

## Namespace isolation

All runtime resources retain the Bitcoin namespace:

```text
/opt/bitcoin-bot
/etc/bitcoin-bot
/var/lib/bitcoin-bot
/var/log/bitcoin-bot
/var/backups/bitcoin-bot
Compose project: bitcoin-bot
```

Regression tests reject use of the separate Binana runtime paths. Monitoring is
isolated on port 8091 rather than Binana's 8090.

## Release and rollback integrity

Retained controls include exact manifest/file-set hashes, strategy fingerprints,
approved artifact digest, safe archive extraction, root-only staging, one-use
approval consumption, release/config identity, atomic current-link switch,
deployment locks, bounded release retention, exact stack identity and
transactional rollback.

The local candidate is not an immutable release yet. After owner review it must
be committed and pushed; GitHub CI must write the final `.git-commit`, regenerate
the manifest and build the exact-head artifact. No local working-tree artifact
should be installed on Oracle.

## Tests and evidence

Completed locally:

```text
Complete release gate:       322 passed, 1 Windows symlink skip
Monitoring suite:             50 passed, 1 third-party deprecation warning
New hardening suite:          13 passed
Audit ledger:                186 files, 619 non-test Python/shell functions
Final manifest:              184 manifested files, exact set matched
Service dependency audit:    no known vulnerabilities
Monitoring dependency audit: no known vulnerabilities
Secret scan:                 clean
Ruff correctness gate:       passed
Bandit high-severity gate:    passed after excluding B413 false positive
Bash syntax:                  passed
Python compile:               passed
Systemd source validation:   13 units, hardening present
YAML/JSON/env validation:    passed
```

The B413 exclusion is documented: the locked maintained dependency is
`pycryptodome`, which intentionally exposes the `Crypto.*` namespace. The
dependency audit found no known vulnerability. No crypto/core migration was
made under this infrastructure-only task.

The sole release-gate skip is Windows symlink creation. The warning is the
existing third-party Starlette/httpx test-client deprecation and does not affect
the bot runtime.

## Current public Binance compatibility check

A credential-free, read-only BTCUSDT check on 13 August 2026 returned:

- status `TRADING` and Spot enabled;
- tick size `0.01`;
- minimum/step quantity `0.00001`;
- minimum notional `5 USDT`;
- `PERCENT_PRICE_BY_SIDE` and `NOTIONAL` filters;
- STOP_LOSS_LIMIT, OCO, OTO, trailing-stop and current self-trade-prevention
  capabilities;
- current execution price-range rules and a valid reference price.

No API key was used and no order was sent. Production public compatibility is
not authenticated TestNet validation.

## Failure and reboot acceptance plan

The deployment guide requires retained evidence for Docker restart, each
container crash, VM reboot, network/DNS loss, Binance Testnet failure, Telegram
failure, OOM, warning/critical disk pressure, corrupt/incomplete releases,
backup verification, restore validation and three rollback cycles.

After every drill, the operator must prove that simulation/TestNet did not
transition to LIVE, entries did not silently resume and the protected strategy
hash did not change.

## Changed files

Modified:

```text
.env.example
PACKAGE_NOTES.txt
README.md
RELEASE_MANIFEST.json
RELEASE_SHA256.txt
deploy/bitcoin-bot-deploy
deploy/install_artifact.sh
deploy/install_monitoring.sh
deploy/lib/envfile.sh
deploy/oracle_setup.sh
docker-compose.yml
docs/GITHUB_ORACLE_DEPLOYMENT.md
docs/OFFICIAL_DEPLOYMENT_REFERENCES.md
docs/audit/FILE_REVIEW_LEDGER.csv
docs/audit/FUNCTION_PARITY_MATRIX.csv
monitoring/.env.monitor.simulation.example
monitoring/.env.monitor.testnet.example
monitoring/INSTALL.md
monitoring/README.md
monitoring/api/configuration.py
monitoring/mcp/monitor_mcp_server.py
monitoring/systemd/bitcoin-bot-monitor-snapshot.service
monitoring/systemd/bitcoin-bot-monitor-snapshot.timer
monitoring/tests/test_monitoring.py
scripts/verify_systemd_units.py
tests/test_repository_hygiene.py
```

Removed as obsolete attack surface:

```text
monitoring/snapshot.py
```

Created:

```text
ORACLE_DEPLOYMENT_HARDENING_REPORT_2026-08-13.md
deploy/backup_state.sh
deploy/oracle_validate.sh
deploy/resource_guard.sh
deploy/systemd/bitcoin-bot-resource-guard.service
deploy/systemd/bitcoin-bot-resource-guard.timer
deploy/verify_backup.sh
docs/ORACLE_SETUP_GUIDE.md
tests/test_oracle_hardening_2026_08_13.py
```

## Residual risks and pending evidence

1. No OCI instance was provisioned; OS, Docker, systemd, firewall, IMDS, swap,
   Chrony, resource-guard, backup and reboot behaviour are not host-validated.
2. No authenticated Binance TestNet order lifecycle was run.
3. No real Telegram delivery was run.
4. No Oracle latency sample was collected; only the diagnostic implementation
   was locally syntax/static tested.
5. No disk/OOM/network/DNS/corrupt-release/rollback fault injection was executed
   on Linux.
6. No 14-day Oracle soak exists.
7. The local candidate is uncommitted and has no exact-head CI artifact.
8. The root-only local backup requires owner-managed encryption and off-host
   retention; automatic cloud backup was intentionally not authorised.
9. OCI A1 capacity and Tokyo-region availability are external and not guaranteed.
10. The strategy profitability gate remains failed.

## Primary official sources

- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle IMDSv2 guidance](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm)
- [Oracle Compute security](https://docs.oracle.com/en-us/iaas/Content/Security/Reference/compute_security.htm)
- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Docker group privilege warning](https://docs.docker.com/engine/install/linux-postinstall)
- [Docker firewall and UFW behaviour](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [Ubuntu automatic security updates](https://documentation.ubuntu.com/server/how-to/software/automatic-updates/)
- [Ubuntu OpenSSH server](https://documentation.ubuntu.com/server/how-to/security/openssh-server/)
- [Binance Spot REST timing security](https://developers.binance.com/en/docs/products/spot/rest-api)
- [GitHub compromised runner guidance](https://docs.github.com/en/enterprise-cloud@latest/actions/concepts/security/compromised-runners)

## Required final status

```text
INFRASTRUCTURE HARDENING: PASS (source/local)
LOCAL TESTS: PASS
PROTECTED STRATEGY UNCHANGED: PASS
TESTNET/SIMULATION ENFORCEMENT: PASS (source/local)
ORACLE HOST VALIDATION: PENDING
BINANCE TESTNET END-TO-END VALIDATION: PENDING
ORACLE SOAK: PENDING
STRATEGY PROFITABILITY GATE: FAILED/UNCHANGED
LIVE PROMOTION: PROHIBITED
```
