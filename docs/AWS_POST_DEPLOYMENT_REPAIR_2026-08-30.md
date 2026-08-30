# AWS post-deployment repair — 30 August 2026

## Evidence and scope

Reviewed the supplied post-deployment issue report, both AWS terminal transcripts
and the accompanying ChatGPT review. The source baselines are Bitcoin TestNet
`1389666de2f9e76ab2c30bc78f52f10a6d089109` and Bitcoin LIVE
`c2f6d86856ebde6e95927fbfd796da883306c213`. These are historical baseline
identities, not the commit of this repair. The AWS observations are supplied
evidence, not a fresh connection to the server. LIVE applicability is established
from matching source, not from a LIVE deployment experiment.

The user's request covers **both Bitcoin repositories**, overriding the report's
TestNet-only proposed scope. Strategy, execution/reconciliation, risk, signal
formula, credentials, balances, orders and persistent state are not changed.
No AWS/Oracle deployment or automatic GitHub merge is part of this change.

## Confirmed corrections

1. **RemotePairList local path.** Both repositories shipped a three-slash URL.
   [The pinned upstream implementation](https://github.com/freqtrade/freqtrade/blob/2026.6/freqtrade/plugins/pairlist/RemotePairList.py)
   strips the literal `file:///` prefix. The old value consequently resolves to
   `/freqtrade/freqtrade/shared/pair/current_pairlist.json` from `/freqtrade`.
   The configured value is now
   `file:////freqtrade/shared/pair/current_pairlist.json`, which retains the
   leading slash and targets the existing read-only mount. The pair selection,
   one-pair limit, refresh interval and fail-closed failure policy are unchanged.
2. **Shipped health-check invocation.** Separately reproduced that
   `scripts/healthcheck.sh` cleared its environment without supplying
   `DEPLOYED_RELEASE_HASH` and `DEPLOYED_CONFIG_SHA256`. It now derives them from
   the release and selected private snapshot, just as the installer does, and
   checks Compose interpolation before reporting service health. The required
   Compose bindings are not made optional. This fixes the shipped helper; it
   does not repair the ad-hoc AWS audit script retroactively.
3. **Missing integration coverage.** `freqtrade/scripts/verify.sh`, already
   required in the exact-commit GitHub artifact job, now runs
   `freqtrade/tests/remote_pairlist_probe.py` in the digest-pinned image with
   networking disabled and read-only source/pair mounts. It invokes the real
   plugin with in-memory exchange metadata, not an exchange connection. It
   checks the shipped URL, three uncached refreshes, an old-URL negative control,
   missing/malformed files, fail-closed refresh and empty-to-populated recovery.
   There is no API-ping shortcut and no application worker or order is started.

The regular offline regressions also verify configuration/mount agreement and
the health-check helper's exact hash bindings, resistance to poisoned ambient
hashes, and rejection of unhealthy, missing and unrenderable stacks. Before the
repair, the focused suite had three failures (path, missing container gate and
healthy-stack invocation) and four passes. After repair all seven passed locally.
The pinned plugin's six tests must additionally pass in GitHub's artifact job;
ordinary Python unit tests do not substitute for that execution.

## Findings deliberately not misclassified or overwritten

| Supplied observation | Assessment / next action |
| --- | --- |
| Pair file existed on host and container with matching hashes | The mount was not the demonstrated defect; leave it unchanged. |
| Compose audit lacked deployment hashes | Audit invocation failure; preserve mandatory integrity bindings. |
| Both BAD and FIXED smoke tests accepted their first observation | Neither output alone proves sustained worker health. The exact parser and repeated production file-not-found errors establish the path defect; retain a real post-install observation window. Some pasted shell blocks are visibly incomplete, so their hidden bodies cannot be fully audited. |
| 200 group-writable checkout files | Reported host permission hygiene issue, not a tracked-content property Git can repair. Review ownership and containing directories on the host; normalise only the verified application checkout, preserving executable bits. Never apply a broad recursive change to persistent state or private configuration. The immutable installer and archive should be used instead of running a mutable checkout. |
| 19 files in the signal inbox | Reported operational cutover blocker. Only the displayed samples were confirmed old-release; classify all files before any move. Do not blindly delete, accept, replay or re-sign them. |
| Telegram exit 137 during an intentional stop, OOMKilled=false | Does not demonstrate a Telegram OOM. |
| Historical Python cgroup OOM | Container attribution remains unproven; no resource-limit change justified. |
| Event loop closed / unclosed connector / chown warning | Observed around the primary failure; independent causation is not established. Do not change CCXT/Freqtrade dependencies or remove hardening on this evidence. |
| pytest unavailable on AWS | Suite was not run there; not a failing test result. |
| Root-only rollback snapshot read by unprivileged awk | Ad-hoc cutover script permission bug, not a reproduced repository defect. Use privileged read access when authorised; do not relax snapshot permissions. |
| Stale simulation=false sidecar files with sidecar absent | Historical records, not current execution-mode evidence. |

## Host recovery gates — still required, not performed here

After the user manually merges green PRs, obtain the artifact from the workflow
whose commit exactly matches the merged `main`. Verify its digest and manifest.
Do not use the previous image tag or assume a source merge updates a container.

On the AWS experiment, keep `/var/lib/bitcoin-testnet` and
`/var/backups/bitcoin-testnet` intact, including the execution database, WAL/SHM,
rollback snapshots and quarantined commands. With producers and consumers safely
quiesced, inventory **every** queued command/signal by filename and SHA-256;
verify release binding, signature, expiry and processed status using read-only
evidence. Any authorised quarantine must preserve the original bytes and hashes
outside the active inbox. Require both inboxes empty before the controlled
cutover. No tool in this change automatically moves a signal or clears a marker.

The supplied rollback state has a missing sidecar and an unhealthy old worker.
The normal immutable installer expects a healthy rollback generation for an
upgrade. Do not bypass that precondition or pretend a routine upgrade can repair
this already-broken generation. A separately reviewed incident-recovery cutover
must preserve the forensic state and use the new verified artifact in simulation.

Validate all four exact-instance containers, fresh state and identity, API and
signal heartbeat, then observe stability across multiple health/refresh intervals
with no increasing restart count. A first API ping, an old health file or a green
GitHub job is insufficient. Retain the logs and image/config/release identities.
Complete real Telegram delivery and menu checks separately; offline routing
coverage does not prove delivery with the user's token.

The durable `EXIT_FILLED` record contains filled `0.00126000` versus protected
`0.00125000` BTC and `ORDER_LIST_TERMINAL_RECONCILE_REQUIRED`. The difference is
`0.00001000` BTC **in stored bot state**, not a verified current account balance.
Keep TestNet entries disabled until separately authorised authenticated GET-only
account/state reconciliation resolves ownership. This configuration repair must
not clear the marker, declare the amount harmless dust or submit an order.

For Oracle, use the existing mode-specific immutable installation and four-bot
host contract; do not weaken it to match the smaller AWS experimental host.
The URL correction is application-level and applies on both x86_64 AWS and
ARM64 Oracle. Actual Oracle installation, recovery and soak remain unperformed.
Both LIVE trading certification and strategy profitability remain separate
NO-GO gates. No claim of all bugs removed or 100% host readiness is made.
