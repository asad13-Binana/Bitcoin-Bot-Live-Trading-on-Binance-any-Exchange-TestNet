# Bitcoin AWS / Oracle deployment repair — 31 August 2026

Status: paired non-core repairs, owner review/manual merge required. This is not a claim that every possible crash, exchange race or strategy defect is eliminated.

## Exact starting sources

- Bitcoin TestNet main: `dbd084d30e913038968ea6d9aaf861fe8f9c854f`.
- Bitcoin LIVE main: `b881eb98a7425d9b7fcab9ecf0342853de6bb2e3`.
- Only these two repositories are edited. BINANA files are reference evidence, not Bitcoin code or permission to change BINANA.
- Whole protected trees `services/`, `freqtrade/` and `shared/` remain unchanged from those respective bases.
- Strategy full-file SHA-256 remains `023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340`.
- Existing PR18 RemotePairList configuration and real pinned-plugin tests remain intact.

## Confirmed repairs

| ID | Severity / classification | Evidence and minimum repair | Regression evidence |
| --- | --- | --- | --- |
| D1 | P1 code defect | Root Compose overrides the upstream Freqtrade UID, but its executable and editable installation reside under ftuser's 0700 home/user-site. Preserve the exact upstream digest, change only home traversal to 0711 and set PYTHONUSERBASE. No new packages, source, entrypoint or strategy changes. | Real upstream negative control and derived-image normal entrypoint, trade help, imports and writable mounts under UID 994/GID 985 on native amd64 and ARM64; repeated from the release artifact. |
| D2 | P1 code defect | Ubuntu resolves /var/lock to /run/lock, so strict canonical-file validation rejected the old identity. Use /run/lock consistently. Recreate missing volatile locks before every caller; reject symlinks, hardlinks, wrong owner/group/mode and unsafe parents without repairing existing objects. | Real Linux root fixtures cover inode/content preservation, simulated reboot disappearance, FIFO/link/ownership/mode rejection and caller ordering. |
| D3 | P1 code defect | backup_state.sh used undefined PERSIST_ROOT under set -u although identity defines PERSIST_PARENT. Use the real canonical parent. | Full backup script plus verify_backup.sh run against disposable Linux fixture directories. |
| D4 | P2 recovery gap | Telegram dedup and Freqtrade signal-only SQLite databases were omitted. Add online backups, finalise target copies in DELETE journal mode, quick-check each; exclude live SQLite/WAL/SHM files from the metadata tar. | Restored execution and Telegram records checked; no WAL/SHM dependency. Source databases are not switched out of WAL mode. |
| D5 | P2 code defect, TestNet only | Off-host uploader's timestamp glob expected too many date digits and could miss valid YYYYMMDDTHHMMSSZ backups. Correct only the selector. | Explicit timestamp regression. Real off-host upload/restore remains untested. |
| D6 | P2 false-health defect | Three Compose health checks accepted future timestamps and/or truthy non-boolean ok values. Use exact true, finite bounded numeric age and explicit exit instead of removable assertions. | Actual health-program execution with Python optimisation enabled; true/false, strings, booleans, future/stale times, NaN, infinity and absent timestamp. |
| D7 | P2 deployment policy gap | The reported ~7.6 GiB AWS experiment is not a four-bot Oracle host. Add an explicit single-bot-experiment profile; keep Oracle as the default and reject undersized/cross-mode/co-hosted experiments. | Capacity/mode/architecture/occupancy fixtures; no live cloud capacity certification. |
| D8 | P2 CI gap | Previous container checks could pass without starting the normal upstream executable as the actual dedicated UID. Add native amd64 and ARM64 jobs, required by the artifact job, with a real failing upstream control. Bind the derived recipe and base digest in SBOM metadata. | GitHub-hosted native container jobs and freshly extracted artifact checks. SBOM metadata is not a complete transitive inventory of the upstream image. |

## Newly supplied AWS terminal transcript

`Welcome to Ubuntu 24.04.4 LTS (GNUL.txt` was read in full: 858 lines; SHA-256
`902589c0d672126c4ab111a1c052ec0e2776465be6579d576f95fa43a45e1789`.

It records this sequence:

1. Root private environment passed root:root 0600 checks; Binance keys empty and simulation interlocks preserved.
2. Official wrapper failed on canonical /var/lock.
3. A host-only /run/lock adaptation let preflight pass; a first diagnostic incorrectly compared Git blob SHA-1 against file SHA-256 and was corrected.
4. Actual root Compose startup then failed with Freqtrade executable not found.
5. chmod-only, python-module-only and global .pth attempts failed normal entrypoint/dependency checks.
6. Home traversal plus PYTHONUSERBASE finally passed all eleven reported probes, including the normal console entrypoint and existing Compose PYTHONPATH.
7. The transcript ends with zero running Bitcoin containers. It proves a successful isolated candidate probe, not a completed bot deployment.

The repair uses that successful minimal approach, with independent native CI regression tests. No terminal commands in the transcript were executed against AWS.

## Host profiles — deliberately different

| Contract | Default Oracle four-bot | Explicit single-bot experiment |
| --- | --- | --- |
| Selection | DEPLOYMENT_PROFILE=oracle-four-bot | DEPLOYMENT_PROFILE=single-bot-experiment |
| Architecture | ARM64 / Oracle A1 | amd64 or ARM64 |
| Physical RAM minimum | 11264 MiB | 7168 MiB |
| CPUs minimum | 2 | 2 |
| Swap minimum at installation | 3800 MiB | 3800 MiB |
| RAM + swap minimum | 14336 MiB | 10968 MiB |
| Free root disk at bootstrap | 80 GiB | 12 GiB |
| Free root disk before artifact installation | 8 GiB | 8 GiB |
| Other running containers | Four-stack contract still applies | Refused unless they belong to this exact Bitcoin Compose project |
| LIVE-money mode | Existing certification gates unchanged | Always refused, including rollback to an old LIVE-money configuration |

These are repository policy floors, not guarantees of sustained performance or cloud Free Tier eligibility. A 1 GiB micro instance does not qualify. The experiment must not share its host with the other Bitcoin or BINANA bot. Two different Bitcoin experiments require different hosts or the full co-host contract.

For a small experimental host, the owner must select the profile explicitly for BOTH host setup and the root-only private .env. The installer reads the latter, not shell MIN_* overrides. For example, after verifying the intended source and backing up any existing deployment, host setup may be invoked with `DEPLOYMENT_PROFILE=single-bot-experiment`; the same literal key/value belongs in `/etc/bitcoin-testnet/.env` (or the isolated LIVE-simulation host's `/etc/bitcoin-live/.env`). Missing selection defaults to Oracle and fails on the smaller host.

No account plan, instance size, disk allocation, paid service, security group, API credential or cloud resource was changed. AWS compute guidance informed the resource separation; sustained burstable CPU use and storage costs must be checked by the owner rather than assumed free.

## Current gates and unresolved issues

- **Known execution-core residual issue remains.** The existing repository records a prior AWS order lifecycle ending with 0.00001000 BTC and fail-closed unowned-balance reconciliation. Current bitcoin_adapter.py still rejects unowned BTC. Do not discard the balance, falsify ownership, zero fee assumptions or reset state to make startup green. Resolving ownership/commission semantics is protected execution work, not this deployment patch.
- **Existing filter work is not duplicated.** MAX_POSITION, symbol-scoped MAX_NUM_ORDER_LISTS and root exchange-wide order limits already exist with regressions. Public BTCUSDT metadata was refreshed read-only; it does not prove account-specific myFilters/MAX_ASSET or TestNet acceptance.
- **No real orders or authenticated requests in this run.** Network failure, accepted-timeout, partial fills, protection races and restart reconciliation still need dedicated authorised TestNet evidence.
- **Telegram source tests are not real-chat proof.** Existing owner/menu/replay tests remain; real owner-only delivery, emergency and restart/replay drills are still required. No second Freqtrade Telegram controller was added.
- **LIVE off-host parity remains incomplete.** LIVE lacks TestNet's complete encrypted off-host tooling. The shared local-backup fixes are ported; uploading/copying a recovery package is not proof of successful fresh-host restore.
- **Recovery is not fully certified.** SQLite fixture restore is not a coordinated multi-database/queue/account point-in-time restore. Local backup retention, durable off-host storage, exact release/config binding, disk pressure, fresh-host restore and rollback with exchange reconciliation need separate evidence.
- **Core strategy profitability remains failed in existing repository evidence.** Approximately -49.10% return, PF 0.3019 and drawdown 49.11%; not rerun or improved in this patch. production_live_certified remains false.
- **Manual merge retained.** PRs do not update main until the owner merges. New successful main CI/artifacts must then be checked. Cloud installation is a separate step, not performed by these PRs.
- **No complete 98/120-point certification.** The master document contains broad audit requests as well as evidence. Questions about every possible financial race, data loss or cloud fault remain unverified unless an explicit regression/host result is listed. Passing an existing suite is not an exhaustive proof.

## Disposition of the master document's forty priority questions

| Questions | Evidence-supported disposition |
| --- | --- |
| 1–4: duplicate order, duplicate/lost fill, partial fill, commissions | Existing core/regressions preserved and suite rerun; authenticated failure matrix remains required. No new universal safety claim. |
| 5–8: dust, unknown balances, stale intents, unowned orders/lists | Known dust blocker remains; unowned-balance/order rejection preserved. No automatic ownership adoption or state deletion. |
| 9–10: missing protection, terminal sibling legs | Existing safety controls unchanged; real exchange protection/cancel races not reproduced here. |
| 11–13: filter drift/scopes and private endpoint isolation | Existing filter/endpoint tests rerun; read-only public metadata refreshed; account-specific authenticated coverage still pending. |
| 14–17: advisory authority, freshness, false green, expected pauses | No confluence policy change. Three Compose false-health cases repaired. Intentional fail-closed pauses remain safety, not errors to suppress. |
| 18–19: release/config mismatch and stale-state rearming | Existing hash gates and entries-off restart controls preserved; new deployed-host proof still required. |
| 20–22: Telegram replay/owner/HMAC replay | Existing tests rerun, transactional Telegram backup added. Real delivery/recovery testing remains external. |
| 23–26: malformed state, backoff, disk full, reboot | Malformed health checks fail; volatile locks recreated safely. Broader runtime corruption/outage/OOM/disk drills not certified. |
| 27–28: cross-mode and four-stack interference | Namespaces unchanged; explicit small-host experiment rejects other running projects. Actual four-stack host validator remains required. |
| 29: secret exposure | Source/history scans retained; no private values requested or published. Docker administrators can inspect container environments; no false claim against root access. |
| 30–32: realistic CI, arbitrary UID, reboot locks | Addressed by real normal-entrypoint and Linux lock regressions, including negative control and both native architectures. |
| 33: missing manifested runtime file | New files included in exact-set manifest/file ledger; gate rejects omission. |
| 34: rollback package/state consistency | Existing rollback controls preserved; derived-image retention updated. Full state/account restoration remains unproven. |
| 35: future/stale heartbeat | Three Compose health consumers repaired/tested; no claim every possible data producer is certified. |
| 36: pair/config divergence | PR18 pinned RemotePairList and release/config-bound tests preserved; fresh host still required. |
| 37: monitoring green while sidecar unsafe | Existing monitoring/Telegram audit tests rerun; no bypass of paused/reconciliation state. |
| 38: runner identities | Immutable Bitcoin TestNet/LIVE identities retained; no host registration or settings change. |
| 39: restored ownership/modes | Root-only backups/verification and standalone copies tested; actual fresh-host restore ownership remains pending. |
| 40: repaired defects without regression | D1–D8 each have the listed focused test; no replacement of production paths with mocks in the normal-entrypoint proof. |

## Source review and limits

The master handoff, its ZIP index/checksums and duplicate payload identities, BINANA handoff/main bug ledger, and the new 858-line terminal transcript were inspected for the repairs above. Older accumulated AWS chat scripts and duplicated appendices were used as historical/reference material, not treated as current host truth or independently validated code. This report does not claim a line-by-line certification of every historical terminal script or every file in the entire upstream Freqtrade repository.

ZIP SHA-256: `5b0664e25254892609635aaf824d7ccd43411b63daee5898b9085e06cff0533c`.
Master SHA-256: `7ff7f40413d624d1c58cc39446e5ade179621ef450af35b1c8e91ef5da6fcd7e`.

The supplied Freqtrade links were opened. Duplicate URLs/anchors refer to the same page. Relevant guidance was checked against the **pinned** image rather than treating current/latest documentation as permission to upgrade:

- [Upstream repository](https://github.com/freqtrade/freqtrade) and [pinned Dockerfile](https://github.com/freqtrade/freqtrade/blob/2026.6/Dockerfile): preserve upstream executable, editable user installation and dependencies.
- [Docker quickstart](https://www.freqtrade.io/en/stable/docker_quickstart/): derived images and Compose are appropriate deployment mechanisms; no mutable automatic upgrades adopted.
- [Installation, including script/manual sections](https://www.freqtrade.io/en/stable/installation/): Docker/ARM64, isolated dependencies and reliable timekeeping are relevant. No upstream reset/install command is run over the custom bot.
- [Exchange notes / Binance](https://www.freqtrade.io/en/stable/exchanges/#binance): exchange/API/fee/stop-order limitations inform testing, not permission to change the sidecar execution model.
- [Stable documentation home](https://www.freqtrade.io/en/stable/), [bot basics](https://www.freqtrade.io/en/latest/bot-basics/) and [strategy 101](https://www.freqtrade.io/en/latest/strategy-101/): signals, running process and completed trades are distinct; signal-only Freqtrade remains separate from sidecar execution.
- [Strategy customisation](https://www.freqtrade.io/en/latest/strategy-customization/): startup candles, closed candles and lookahead/recursive checks are useful audit concepts, not changes to indicators or entry conditions.
- [Backtesting](https://www.freqtrade.io/en/latest/backtesting/): fee assumptions, cached results, intrabar simulation limitations and reproducibility require separate strategy evidence; none of this certifies profitability here.
- [Telegram usage](https://www.freqtrade.io/en/stable/telegram-usage/): useful command/owner concepts, but copying force-entry or a second polling controller would violate this bot's architecture.
- [Strategy repository / free-strategy section](https://github.com/freqtrade/freqtrade-strategies#free-trading-strategies) and [strategy directory](https://github.com/freqtrade/freqtrade-strategies/tree/main/user_data/strategies): educational examples, not ready-made profit guarantees. No sample, futures strategy or indicator was copied.

## Evidence and next step

Initial PR commits passed full GitHub workflows: TestNet `02b873a194cf3480a50db80de8eafcb0e0a6f4d2` / run `33344432436`; LIVE `096b6028b5dd4cfbd6361784b46ca2f1dfb6cb48` / run `33344435960`. Each had successful Python 3.10–3.13, SAST, quality evidence, native amd64/ARM64 runtime jobs and artifact verification. Each native runtime job ran all 50 deployment regressions with no skips. Oracle deployment was skipped.

Those runs cover the initial container/lock/backup/health patch, **not later commits** containing this document and the host profile. Before merging, require the latest exact PR head to pass all jobs again. Local Windows cannot execute Docker/systemd; Linux CI proof is separate from AWS/Oracle-host proof.

Next: owner reviews and manually merges green PRs; verify exact post-merge main artifacts; refresh trusted root deployment helpers from that verified release (updating GitHub alone does not replace installed root helpers); preserve/back up existing state; install simulation/TestNet first; collect host, Telegram, authorised TestNet lifecycle, restore and soak evidence. Both LIVE packages stay simulation-only until separately certified.
