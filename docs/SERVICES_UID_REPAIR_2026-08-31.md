# Bitcoin services runtime repair and historical regression review

Scope: Bitcoin TestNet and Bitcoin LIVE. Owner-controlled manual merge only.
No AWS/Oracle deployment, account requests, credentials or orders are authorised
by this repair. LIVE remains simulation/read-only. This is not a claim that all
possible failures have been eliminated.

## Exact bases and supplied evidence

- TestNet: `d323b6ffd179a78c82ad5ebd1b861068d54aed99` (merged PR20).
- LIVE: `fd6cb57d8954eb96817c040c34844034306781e2` (merged PR20).
- Both Desktop PR20 transcripts were read completely; the second includes the
  plugin-markup Bash error. They describe successful checks/setup/backup followed
  by an unhealthy moneyflow and rollback. These files were no longer present at
  their supplied paths when a later checksum read was attempted; no checksum is
  invented and no file was removed by this task.
- Complete supplied repair brief SHA-256:
  `1fd5ed03757222118a6e461ffab88b09256cad6fb4d6864617ee38c49b5e5c47`.
- Later isolated AWS diagnostic SHA-256:
  `24a98a3bf0ee32359d17cd3e7f3e1c3400fded73448b4063838aca3bf9eb243b`.
- The latter proves an import PermissionError and missing fresh output. Its
  earlier exit-zero/empty-output attempt does not prove a successful collection.
  Public Binance HTTP successes are supplied AWS evidence, not a fresh server
  connection made during this repair.

## Confirmed repairs

1. **Image permissions.** Root-only deployment extraction produces restrictive
   modes (`umask 077`, tar `--no-same-permissions`). Docker COPY preserves modes;
   `--chown=bot:bot` changes ownership, not read permission. Compose intentionally
   overrides image UID 10001 with the dedicated host identity. The old image
   therefore fails for UID 994/GID 985. Root owns immutable `/app` content in the
   repaired image; directories are 0555 and regular files 0444 after the final
   COPY. No host directory, volume, UID, strategy, package or resource limit is
   changed. See [Docker's COPY contract](https://docs.docker.com/reference/dockerfile/#copy).
2. **Real identity regression.** Native amd64 and ARM64 jobs build from a
   deliberately 0700-directory/0600-file context. The old recipe must fail
   imports for both `994:985` and `12345:23456`; the repaired image must pass.
   Tests include the built-in UID 10001 too, and verify DAC on a writable root
   layer so `--read-only` cannot hide an ownership mistake.
3. **Actual moneyflow output.** The real MoneyFlowClient and service `run_once`
   consume a deterministic public-only BTCUSDT endpoint fixture. All seven
   timeframes, fresh latest.json and moneyflow_health.json are asserted, not just
   process exit zero. Pair input is mounted read-only; correctly owned 0750/0640
   disposable state is writable only by its owner. Network is disabled and no
   order interface exists on the fixture. This is not real Binance connectivity
   or WebSocket/soak evidence.
4. **Rollback evidence.** Before Compose down, a helper gets a maximum of four
   attempted-release containers, bounded health history and bounded log input.
   Only allowlisted state fields/error categories survive; raw messages,
   environments, arbitrary labels, command arguments and credentials are never
   written. Reports are root:root 0600 under root-only 0700
   `/var/log/bitcoin-testnet-deployment` or `/var/log/bitcoin-live-deployment`.
   Individual reads are limited to 3 seconds/64 KiB; the entire helper is limited
   to 30 seconds. Capture failure warns and never prevents rollback. No old
   backups, runtime state or failed containers are kept alive to preserve logs.
5. **Read-only status displays.** Telegram's `_flow_summary` previously repeated
   historical ok/provider freshness without checking snapshot age. It now marks
   old, missing, malformed/non-finite and future timestamps unavailable and
   labels even fresh data as a snapshot, not a current-service probe. Monitoring
   no longer clamps future heartbeats to age zero or calls a fresh `ok:false`
   heartbeat healthy. Only reporting changes; no command routing, authorisation,
   signal computation or execution policy changes.
6. **Earlier Freqtrade proof extended.** The existing pinned normal-entrypoint
   `--version`/`trade --help`, import and writable-mount checks additionally run
   under `12345:23456`. This still does NOT prove a running Freqtrade REST server
   or exchange-connected trading worker; that remains host validation.

## All sixteen supplied failure headings: current disposition

| Heading | Current disposition |
| --- | --- |
| 1 Freqtrade arbitrary UID | Earlier PR19 fix retained; actual normal-entrypoint proof extended to the second identity. Full worker/API startup remains external. |
| 2 False canonical-path rejection | PR20 root-before-side-effects setup remains; protected 0750 state is not relaxed. Existing root-context regressions retained. |
| 3 Invalid chown option | PR20 descriptor-bound helper and native mount-namespace tests retained. No unsupported chown option reintroduced. |
| 4 Unhealthy moneyflow / rollback | Confirmed AWS observation; image import failure is repaired. Actual new-artifact AWS deployment remains required. |
| 5 Arbitrary-UID services permission | Repaired inside Dockerfile.services, including strategy and metadata readability. |
| 6 CI tested wrong identity/context | Both native architectures now test both arbitrary identities and restrictive context; artifact image tested again. |
| 7 Binance connectivity suspected | No networking redesign. Failure preceded imports; supplied public HTTP probes do not prove authenticated TestNet. |
| 8 Logs lost during cleanup | Bounded sanitised pre-rollback evidence added; capture never blocks cleanup. Real failed-deploy evidence persistence remains a host drill. |
| 9 Failed artifact consumed | Intentional one-shot approval consumption occurs before installer invocation, not on success. See precise semantics below. No replay control changed. |
| 10 Old healthy snapshot | Historical data retained; Telegram/monitoring status semantics repaired. Existing execution freshness checks unchanged. |
| 11 Residual BTC | Unresolved ownership evidence/core issue, not fixed by permissions. Existing unknown-balance/fill/commission/emergency regressions rerun; do not clear pause or assume dust. |
| 12 Sidecar restart storm | Existing `on-failure:5` and durable reconciliation pause retained. No unbounded restart policy restored; real restart lifecycle remains external. |
| 13 CoinMarketCap parser | Already supports successful one-item BTC list/v3 envelope; exact-envelope and wrong-asset tests retained. No duplicate parser or new provider. |
| 14 Signal audit false positive | Supplied probe failure/zero signals is not inability to signal. Existing pinned strategy tests retained; no signal formula change. |
| 15 Volatile hash comparison | Historical ad-hoc audit issue, not a reproduced immutable-release defect. Heartbeats are runtime state; immutable manifest is not a convergence hash for runtime files. |
| 16 Private env manifest scope | Private env belongs outside immutable release, root:root 0600. Exact-set rejection of an extra repository-root .env remains correct; no secret file added to manifest. |

Terminal/plugin markup pasted into Bash, missing stdin on a diagnostic, and a
debug image absent after cleanup are operator/probe conditions, not new bot code
defects. Their outputs are not accepted as runtime passes.

## Historical repair comparison requested by the owner

Comparison uses current source, Git history and the complete current regression
suite, not an assertion that every past AI narrative or external experiment was
correct. Historical audit documents remain unchanged; this is an addendum.

| Earlier repair family | Current guard/evidence retained |
| --- | --- |
| Dependency lock/cache and release mode (early CI) | Hash locks, exact mode file, pip installation/audits and manifest checks; now tested in the services image under real UID overrides. |
| Literal private-env parsing and ownership | `test_env_file_parser`, `test_oracle_hardening_2026_08_13`; no shell evaluation or relaxed key/owner/mode checks. |
| SAST, ledgers, source integrity, provenance | CI correctness/security/history-secret jobs and exact-file-set/ledger/SBOM/provenance verification remain mandatory; regenerated for this change. |
| Durable risk state and explicit filter bounds | `test_execution_safety`; transactional state, explicit fail-closed exception, fee and ownership controls unchanged. |
| Telegram replay / menus / callbacks / owner controls | Existing `test_control_and_flow`, completion/property/API tests rerun. This patch changes only the read-only flow presentation function. Real delivery is not certified. |
| Binance symbol/exchange filters and lifecycle | `test_binance_official_sources`, execution safety and metadata tests retained. MAX_POSITION/order-list scopes not reimplemented; myFilters/MAX_ASSET account relevance remains unverified. |
| Spot-only flow and optional provider context | `test_spot_only_moneyflow`, `test_external_market_context`; no futures client or new provider, default advisory policy unchanged. |
| API readiness and co-host isolation | `test_api_readiness`, `test_four_bot_cohosting`, instance identity and capacity tests retained. No API credentials or four-stack host actions. |
| Local/off-host recovery | Linux backup tests retained; TestNet off-host tests retained. LIVE off-host package parity and fresh-host restore still incomplete; do not claim otherwise. |
| PR17 restart/menu/health binding | Five retries, Telegram sidecar-independent availability, immutable identity/private snapshot retained. |
| PR18 RemotePairList and health helper | Exact pinned plugin/path tests and release/config-binding tests retained. No path reversion. |
| PR19 locks, backup, health, capacity and Freqtrade | All 50 deployment fixtures and native entrypoint tests remain. This patch closes the missed services-image identity gap, not an AWS/Oracle certification. |
| PR20 ownership | All 26 ownership cases remain, including private mount namespace on both native architectures. |

## Artifact consumption: do not confuse attempt with success

`consume_approval` records the attempted digest and **empties** approved-artifact
before invoking the installer. A recorded consumed digest is not a success
marker. The current wrapper does not independently reject all future copies of
that same digest: a root administrator can explicitly approve it again. Thus the
claim that a failed digest is permanently forbidden is too strong. Do not
automatically restore approval after rollback. If both files were non-empty,
that alone does not describe the current wrapper's immediate consumption result.
For this incident do NOT reapprove PR20: it has a known defect. Use a newly built,
verified exact-main artifact after the owner merges this repair.

## Verification and protected boundary

Run the complete release gate, focused tests, native container jobs, ownership
tests, SAST/dependency audits, and freshly extracted artifact verification.
Measured counts belong to the exact-head CI logs; local skips are not passes.
Windows has no Docker runtime here, so native amd64/ARM64 proof must come from
GitHub's jobs. The artifact job depends on both architecture jobs.

No file under `freqtrade/`, `shared/`, `services/execution_sidecar/`,
`services/moneyflow/` or `services/common/` changes. The ONLY services-source edit
is the read-only Telegram `_flow_summary`; its other function bodies are
unchanged. Strategy full-file SHA-256 remains
`023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340`.
Compose identity, read-only flag, capability drop, no-new-privileges, memory/CPU/
PID limits and all host-state modes remain unchanged.

## Owner handoff

Review the two PRs and exact-head CI; do not merge automatically. After manual
squash merge require new exact-main CI/artifacts, checksum/provenance verification
and refreshed trusted deployment helpers. PR artifacts and PR20 artifacts are
not the future merged release. This task performs no AWS installation.

Simulation-first host validation still requires four-service health, Telegram,
restart, network/disk-pressure, coordinated backup/restore and soak. Authenticated
GET-only account/state reconciliation must resolve residual BTC before any new
TestNet order mutation. Oracle/four-stack validation is separate from the AWS
single-bot experiment. No strategy redesign or LIVE-money promotion is made.
