# Verification command ledger

Audit date: 2026-07-29

Run commands from the release root. This file defines reproducible commands and their
meaning; it does not claim that environment-dependent commands were executed. Record
actual final counts and failures in `VALIDATION_STATUS.json` and the final audit report.

## Windows offline audit host

The established environments for this workspace are:

```powershell
$CorePython = 'D:\Claude Binance Bot Cowork\_MASTER_AUDIT\codex_bitcoin_build_2026-07-22\verifyenv\Scripts\python.exe'
$MonitorPython = $CorePython
$GitBash = 'C:\Program Files\Git\bin\bash.exe'
$ReleaseRoot = 'D:\Claude Binance Bot Cowork\_MASTER_AUDIT\codex_bitcoin_final_2026-07-29\source\bitcoin-bot'
Set-Location -LiteralPath $ReleaseRoot
```

Core and monitoring regression suites:

```powershell
& $CorePython -m pytest -q tests
& $MonitorPython -m pytest -q monitoring/tests
```

Compilation, secrets and manifest checks:

```powershell
& $CorePython -m compileall -q services freqtrade/user_data/strategies tests scripts monitoring deploy
& $CorePython tests/secret_scan.py
& $CorePython scripts/build_audit_ledgers.py --check
& $CorePython scripts/verify_manifest.py
```

Parse every JSON file and both Compose YAML files without starting services:

```powershell
@'
import json
from pathlib import Path
import yaml
for path in Path('.').rglob('*.json'):
    if not any(part in {'.git', '__pycache__', '.pytest_cache'} for part in path.parts):
        json.loads(path.read_text(encoding='utf-8'))
for path in list(Path('.').rglob('*.yml')) + list(Path('.').rglob('*.yaml')):
    if not any(part in {'.git', '__pycache__', '.pytest_cache'} for part in path.parts):
        yaml.safe_load(path.read_text(encoding='utf-8'))
print('JSON/YAML parse passed')
'@ | & $CorePython -
```

POSIX shell syntax through Git Bash:

```powershell
& $GitBash -lc "cd '$($ReleaseRoot.Replace('\','/'))' && find . -type f -name '*.sh' -not -path './.git/*' -print0 | xargs -0 -n1 bash -n"
& $GitBash -lc "cd '$($ReleaseRoot.Replace('\','/'))' && bash -n deploy/bitcoin-bot-deploy"
```

Dependency-lock installation and vulnerability checks:

```powershell
& $CorePython -m pip install --dry-run --require-hashes -r requirements.services.lock
& $MonitorPython -m pip install --dry-run --require-hashes -r monitoring/requirements-monitoring.lock
& $CorePython -m pip_audit -r requirements.services.lock --strict
& $MonitorPython -m pip_audit -r monitoring/requirements-monitoring.lock --strict
& $CorePython -m pip check
& $MonitorPython -m pip check
```

## Linux/GitHub canonical offline gate

Use Python 3.10, 3.11, 3.12 and 3.13 as encoded by the GitHub Actions matrix:

```bash
python3 -m pip install -r requirements-dev.txt
PYTHON=python3 bash deploy/verify_release.sh
python3 -m pip_audit -r requirements.services.lock --strict
python3 -m pip_audit -r monitoring/requirements-monitoring.lock --strict
```

`deploy/verify_release.sh` performs compilation, core and monitoring tests, secret scan,
manifest verification, JSON/env validation, Bash syntax, systemd service/timer pairing,
and either Docker Compose validation or a YAML structural fallback. A fallback is not a
Docker runtime pass.

## Docker and Freqtrade — externally required

With Docker Engine and Compose available:

```bash
make compose
docker compose --env-file .env.example build moneyflow
bash freqtrade/scripts/verify.sh
docker compose --env-file .env.example up -d
bash scripts/healthcheck.sh
docker compose --env-file .env.example ps
docker compose --env-file .env.example logs --no-color --tail=500
```

Confirm exactly these root services: `moneyflow`, `freqtrade`, `execution-sidecar`, and
`telegram-broker`. Do not treat the nested offline-analysis Compose profile as a second
production stack.

Offline strategy analysis, after downloading exact pair data:

```bash
bash freqtrade/scripts/download_data.sh 20240101-20260101 BTC/USDT
bash freqtrade/scripts/backtest.sh 20240101-20250101 BTC/USDT
bash freqtrade/scripts/backtest.sh 20250101-20260101 BTC/USDT
bash freqtrade/scripts/lookahead.sh 20240101-20260101 BTC/USDT
bash freqtrade/scripts/recursive.sh 20240101-20260101 BTC/USDT
```

Retain the official result ZIP, resolved config, complete logs and lookahead CSV. Review
signal counts and analysis output; process exit alone is not a promotion verdict.

## systemd and Oracle — externally required

```bash
systemd-analyze verify monitoring/systemd/*.service monitoring/systemd/*.timer
sudo bash deploy/oracle_setup.sh
sudo systemctl daemon-reload
systemctl list-timers --all | grep bitcoin-bot-monitor
sudo journalctl -u bitcoin-bot-monitor-testnet.service --no-pager
sudo journalctl -u bitcoin-bot-monitor-report-testnet.service --no-pager
```

Follow `docs/GITHUB_ORACLE_DEPLOYMENT.md`; do not substitute a `git pull` deployment for
the verified immutable artifact installer. Repeat restart, network-loss, OOM, disk-full,
backup and rollback drills and preserve redacted evidence.

## Final freeze, ZIP and checksum

Only after source, tests, audit documents and truthful validation status are final:

```powershell
& $CorePython scripts/build_manifest.py
& $CorePython scripts/verify_manifest.py
$Desktop = [Environment]::GetFolderPath('Desktop')
$ReleaseName = if ((Get-Content RELEASE_MODE -Raw).Trim() -eq 'live') {
  'BITCOIN_BOT_LIVE_GITHUB_ORACLE_2026-07-30.zip'
} else {
  'BITCOIN_BOT_TESTNET_GITHUB_ORACLE_2026-07-30.zip'
}
$ReleaseZip = Join-Path $Desktop $ReleaseName
& $CorePython scripts/build_release_zip.py $ReleaseZip
Get-FileHash -Algorithm SHA256 -LiteralPath $ReleaseZip
Get-Content -LiteralPath ($ReleaseZip + '.sha256')
```

Freshly extract into a new empty directory, rerun manifest/secret/compile/test checks from
the extraction, and separately verify archive member uniqueness, traversal rejection,
symlink/device absence and CRC. Never update validation evidence after the final manifest
without rebuilding the manifest and ZIP again.

## Original 2026-07-30 host blockers and current evidence boundaries

- The original 2026-07-30 source path was not a Git repository. The current private
  repositories and Git history exist, but workflow and artifact proof remains bound to
  the exact commit shown by GitHub Actions; repository creation alone is not a pass.
- Docker Compose parsing passed, but the Docker Desktop Linux engine and systemd runtime
  were not available on the Windows audit host.
- Authenticated Binance Testnet orders, real Telegram delivery, Oracle deployment and
  soak were not performed locally. The exact canonical Freqtrade backtest was performed
  and failed the profitability gate; lookahead/recursive analyses remain deferred until a
  redesigned frozen strategy passes that gate.
- Missing optional local tools such as `shellcheck` are not equivalent to failed Bash
  syntax, but their checks must not be claimed.
