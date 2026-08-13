#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
PYTHON=${PYTHON:-python3}
command -v "$PYTHON" >/dev/null 2>&1 || { echo "$PYTHON is required" >&2; exit 1; }

# Test-only message-bus secrets. They are regenerated for every verification
# run and cannot be used as deployment credentials.
export SIGNAL_HMAC_KEY="$("$PYTHON" -c 'import secrets;print(secrets.token_hex(24))')"
export COMMAND_HMAC_KEY="$("$PYTHON" -c 'import secrets;print(secrets.token_hex(24))')"
export ENVELOPE_RELEASE_HASH="$("$PYTHON" -c 'print("a"*64)')"
export EXECUTION_MODE=simulation
export TELEGRAM_BOT_TOKEN="$("$PYTHON" -c 'print("123456789:" + "t" * 32)')"
export TELEGRAM_OWNER_CHAT_ID='123456789'
export SIDECAR_RELEASE_HASH="$ENVELOPE_RELEASE_HASH"
export DEPLOYED_RELEASE_HASH="$ENVELOPE_RELEASE_HASH"
export DEPLOYED_CONFIG_SHA256="$("$PYTHON" -c 'print("b"*64)')"

case "$(<RELEASE_MODE)" in
live|testnet) ;;
*) echo 'RELEASE_MODE must be live or testnet' >&2
  exit 1
  ;;
esac

for forbidden in legacy_core services/sharia_screener services/universe_service shared/sharia shared/universe; do
  [[ ! -e "$forbidden" ]] || {
    echo "forbidden non-Bitcoin component remains: $forbidden" >&2
    exit 1
  }
done

"$PYTHON" -m compileall -q services freqtrade/user_data/strategies tests scripts monitoring
"$PYTHON" -m pytest -q tests
"$PYTHON" -m pytest -q monitoring/tests
"$PYTHON" tests/secret_scan.py
"$PYTHON" scripts/build_audit_ledgers.py --check
"$PYTHON" scripts/build_sbom.py --check
"$PYTHON" scripts/verify_artifact_provenance.py
"$PYTHON" scripts/verify_manifest.py

"$PYTHON" - <<'PY'
import json
import pathlib
import re

for path in pathlib.Path('.').rglob('*.json'):
    if any(part in {'.git', '__pycache__', '.pytest_cache', '.hypothesis'} for part in path.parts):
        continue
    json.loads(path.read_text(encoding='utf-8'))
names = []
for line in pathlib.Path('.env.example').read_text(encoding='utf-8').splitlines():
    match = re.match(r'^([A-Z][A-Z0-9_]*)=', line)
    if match:
        names.append(match.group(1))
duplicates = sorted({name for name in names if names.count(name) > 1})
assert not duplicates, f'duplicate .env.example keys: {duplicates}'
print('JSON and env-template validation passed')
PY

find . -type f -name '*.sh' -not -path './.git/*' -not -path '*/__pycache__/*' -print0 |
  while IFS= read -r -d '' script; do bash -n "$script"; done
bash -n deploy/bitcoin-bot-deploy

"$PYTHON" - <<'PY'
from pathlib import Path

units = Path('monitoring/systemd')
services = {path.stem for path in units.glob('*.service')}
for timer in units.glob('*.timer'):
    text = timer.read_text(encoding='utf-8')
    target = next(
        (line.split('=', 1)[1].strip() for line in text.splitlines() if line.startswith('Unit=')),
        timer.with_suffix('.service').name,
    )
    assert Path(target).stem in services, f'{timer.name} has no matching service {target}'
print(f'systemd service/timer pairing passed ({len(services)} services)')
PY
# systemd-analyze verify resolves each unit's ExecStart binary against the
# machine running the check. Those paths exist only after installation on the
# deployment host, so verification runs in one of two explicit contexts:
#
#   SYSTEMD_VERIFY_CONTEXT=installed  nothing is excused; systemd-analyze must
#                                     exit 0 and every project-owned ExecStart,
#                                     ExecStartPre, ExecStop and EnvironmentFile
#                                     must exist (and be executable where it is
#                                     a command).
#   SYSTEMD_VERIFY_CONTEXT=source     only the specific, enumerated paths that
#                                     the Oracle installer creates may be
#                                     absent, plus the documented external
#                                     docker.service dependency. Unit syntax
#                                     errors, malformed directives, bad
#                                     dependency relationships, security-setting
#                                     regressions and any unrecognised
#                                     systemd-analyze output still fail.
#
# Default is auto-detection, which resolves to 'installed' when the monitoring
# interpreter created by deploy/install_monitoring.sh is present. Set the
# variable explicitly to force a context (CI uses 'source'; the Oracle host
# post-install check uses 'installed').
SYSTEMD_VERIFY_CONTEXT="${SYSTEMD_VERIFY_CONTEXT:-auto}"
case "$SYSTEMD_VERIFY_CONTEXT" in
auto)
  if [[ -x /opt/bitcoin-bot/monitoring-current/bin/python ]]; then
    SYSTEMD_VERIFY_CONTEXT=installed
  else
    SYSTEMD_VERIFY_CONTEXT=source
  fi
  ;;
source|installed) ;;
*)
  echo "SYSTEMD_VERIFY_CONTEXT must be auto, source or installed" >&2
  exit 1
  ;;
esac
export SYSTEMD_VERIFY_CONTEXT

"$PYTHON" scripts/verify_systemd_units.py

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose --env-file .env.example config -q
  mapfile -t compose_services < <(docker compose --env-file .env.example config --services | sort)
  expected_services=(execution-sidecar freqtrade moneyflow telegram-broker)
  [[ "${compose_services[*]}" == "${expected_services[*]}" ]] || {
    echo "root compose services differ: ${compose_services[*]}" >&2
    exit 1
  }
  echo 'Docker Compose validation passed'
else
  "$PYTHON" - <<'PY'
import pathlib
import yaml

parsed = {}
for path in list(pathlib.Path('.').rglob('*.yml')) + list(pathlib.Path('.').rglob('*.yaml')):
    if any(part in {'.git', '__pycache__', '.ruff_cache', '.hypothesis'} for part in path.parts):
        continue
    parsed[path.as_posix()] = yaml.safe_load(path.read_text(encoding='utf-8'))
root = parsed.get('docker-compose.yml')
assert isinstance(root, dict), 'root compose is missing or malformed'
expected = {'moneyflow', 'freqtrade', 'execution-sidecar', 'telegram-broker'}
actual = set(root.get('services', {}))
assert actual == expected, f'root compose services differ: {sorted(actual)} != {sorted(expected)}'
assert isinstance(parsed.get('freqtrade/docker-compose.yml'), dict)
print(f'YAML structural validation passed for {len(parsed)} file(s) (Docker unavailable)')
PY
fi

echo 'Bitcoin Bot mode-separated release verification passed.'
