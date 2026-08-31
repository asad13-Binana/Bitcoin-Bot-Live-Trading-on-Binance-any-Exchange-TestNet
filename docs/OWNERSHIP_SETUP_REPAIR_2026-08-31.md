# Bitcoin installer ownership portability repair — 31 August 2026

Scope: Bitcoin TestNet and Bitcoin LIVE deployment only. Manual owner merge;
no AWS/Oracle action, credentials, exchange orders or trading-core changes.

## Verified starting points

- TestNet main: `643a5081b4eecc527c69d76fcd884b68a8d10dd6`.
- LIVE main: `212b06c0c2f477d0d5da9039b36b25d2f4898a98`.
- Both protected strategy files: `023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340`.
- Prior PR19 changes remain intact. Nothing under `services/`, `freqtrade/`
  or `shared/` is part of this patch.

## Evidence and classification

**CLAIM:** The merged installer fails at recursive ownership on Ubuntu.
**VERIFICATION:** The supplied AWS Ubuntu 24.04.4 amd64 transcript shows the
exact PR19 artifact, inner checksum, manifest, provenance, secret scan and
strategy hash passing. Root-context path and capacity checks then pass, followed
by `chown: unrecognized option '--one-file-system'`. No successful container
deployment follows. Both current setup files contain two such calls. Local Git
Bash also rejects the option; the added native test independently executes the
GNU command and asserts failure without ownership mutation.
**VERDICT: CONFIRMED.**
**ACTION:** Replace both ownership call sites in both repositories.

AWS transcript SHA-256:
`6fe1f1912e5410c94739fcb32b9c21b3b828a7129bd78593c374273cef5e9fda`.
Supplied repair-request SHA-256:
`f7849202a8f7eb607979eb0df0f792e437700c5e2dbc07daacb5b82bdcacf6d9`.

The handoff states Docker 29.7.2 and Compose 5.5.0. Their version-command output
is not present in the supplied terminal excerpt, so they are reported handoff
metadata, not independently verified runtime versions.

**CLAIM:** Every use of `--one-file-system` should be removed.
**VERIFICATION:** Only the two `chown` calls per setup file are invalid. Other
occurrences belong to `rm`, `tar` and their regression assertions.
**VERDICT: DISPUTED.**
**ACTION:** Preserve those valid cleanup/archive boundaries unchanged.

**CLAIM:** The earlier audit-path error necessarily means a malicious path.
**VERIFICATION:** The terminal shows mode-0750 bot-owned directories, no symlink,
and a successful canonical result as root. Current main guides nevertheless show
unprivileged setup invocation, while setup mixes sudo operations with ordinary
path checks.
**VERDICT: DISPUTED as a path-corruption finding; CONFIRMED invocation mismatch.**
**ACTION:** Require root before source loading, package operations or other
setup side effects; document sudo with explicit non-root DEPLOY_USER. Keep the
root-context canonical-path comparison and protected 0750 permissions unchanged.

## Engineering decision

Deleting the flag and retaining unrestricted `chown -R` is rejected.
A simple `find -xdev -exec chown --no-dereference ...` is also insufficient:
-xdev prevents descent into different-device directories, not action on the
mounted directory itself; same-device bind mounts are not excluded by -xdev.

The replacement is a small standard-library Python helper using existing Linux
and libc interfaces, not a new package or shell dependency:

1. Open the canonical parent without symlink traversal, then the selected root
   with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV)`.
   Reject a selected root that is itself a mount. Ancestor volumes such as a
   separate /var are permitted; the selected tree boundary is not crossed.
2. Inventory all requested trees before changing ownership. Every descendant
   open is relative to the original root descriptor with the same restrictions.
   This rejects foreign filesystems AND same-device bind mounts, including
   individual-file mounts, before operating on their roots.
3. Allow normal files, directories and symlink objects only. Reject hard-linked
   non-directories and special files. O_PATH plus O_NOFOLLOW allows a terminal
   symlink object to be inspected, never its target.
4. Reopen each inventoried object under the same kernel restrictions and verify
   device/inode/type. Apply `fchownat` with an empty path and
   AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW to that descriptor, then verify ownership.
   Replacing the pathname cannot redirect ownership to a symlink target.
5. Fail on unsupported architecture/kernel/interfaces or unexpected changes;
   never fall back to unrestricted recursive ownership.

This is Linux amd64/ARM64 portability for the supported Ubuntu host, not POSIX
portability to Windows/macOS or kernels without openat2. No strategy,
dependencies, entrypoint, sizing, risk, execution ownership or residual-BTC
reconciliation behaviour changes.

Maintenance must have stopped writers. This helper is not an atomic snapshot,
a rollback transaction or protection against a concurrent privileged host
administrator. A later concurrent mutation/error can leave already-processed
in-bound objects updated; the command reports failure rather than success.
Do not claim a universal no-race guarantee for a live attacker-writable tree.

Sources: [GNU chown](https://www.gnu.org/software/coreutils/manual/html_node/chown-invocation.html),
[GNU find filesystem boundaries](https://www.gnu.org/software/findutils/manual/html_node/find_html/Filesystems.html),
[Linux openat2](https://man7.org/linux/man-pages/man2/openat2.2.html),
[Linux fchownat](https://man7.org/linux/man-pages/man2/fchownat.2.html).

## Tests and release controls

`tests/test_ownership_tree.py` contains 26 focused cases. They exercise the
actual helper CLI, both numeric bot ownership and root:root, root directories,
nested/empty directories, unusual filenames, symlinks including dangling links,
hardlinks, FIFO rejection, namespace replacement, root invocation, and real
tmpfs/directory-bind/file-bind mounts. A real find/chown counterexample proves
why the shorter shell replacement was rejected.

CI runs all ownership cases as root inside a private mount namespace on native
Ubuntu 24.04 amd64 and ARM64. A failed mount or unavailable syscall fails these
jobs; it is not silently skipped. Packaging depends on both jobs. The 50 PR19
deployment regressions and normal Freqtrade entrypoint tests remain required.

On Windows, only the five platform-independent cases run; 21 native cases are
explicit skips. Full source suites, shell parsing, source-context systemd,
secret scanning, ledgers, manifest, SBOM/provenance, native container tests and
freshly extracted release verification remain required for each exact PR head.
Consult that exact workflow for measured counts/results; this tracked document
does not self-certify mutable GitHub or host state.

## Owner-controlled completion and next AWS run

Create two feature PRs; do not merge or enable auto-merge. After the owner merges,
require each new main's successful workflow and its newly generated artifact.
Do not locally patch or reuse the PR19 artifact as if it contained this repair.

Preserve the private root:root 0600 environment, audit records, state and backups.
Refresh trusted deployment helpers from the verified new release. Use an isolated
TestNet simulation host with single-bot-experiment explicitly selected; keep
Binance keys empty initially, LIVE_TRADING_ENABLED=false and AUTO_CONFIRM=false.
Root setup through sudo does not require giving the administrator Docker/lxd/disk/root
group membership. Actual AWS installation remains unproven until rerun;
Oracle installation and API/recovery/soak evidence remain separate. Residual BTC
and LIVE off-host parity are explicitly not repaired by this installer PR.
