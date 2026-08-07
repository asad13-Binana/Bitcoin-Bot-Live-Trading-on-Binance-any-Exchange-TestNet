from __future__ import annotations
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {'RELEASE_MANIFEST.json', 'RELEASE_SHA256.txt'}
EXCLUDE_PARTS = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache'}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_release_files() -> set[str]:
    files: set[str] = set()
    for path in ROOT.rglob('*'):
        rel = path.relative_to(ROOT).as_posix()
        if path.is_symlink():
            raise ValueError(f'symlink is not allowed in source release: {path.relative_to(ROOT)}')
        if not path.is_file():
            continue
        if rel in EXCLUDE or any(part in EXCLUDE_PARTS for part in path.parts):
            continue
        files.add(rel)
    return files


def main():
    manifest_path = ROOT / 'RELEASE_MANIFEST.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    errors: list[str] = []
    try:
        release_mode = (ROOT / 'RELEASE_MODE').read_text(encoding='utf-8').strip()
    except OSError:
        release_mode = ''
    if release_mode not in {'live', 'testnet'}:
        errors.append('RELEASE_MODE missing or invalid')
    if manifest.get('package_mode') != release_mode:
        errors.append('manifest package_mode does not match RELEASE_MODE')
    manifest_files = set(manifest.get('files', {}))
    try:
        actual_files = current_release_files()
    except ValueError as exc:
        errors.append(str(exc))
        actual_files = set()
    for rel in sorted(actual_files - manifest_files):
        errors.append(f'unmanifested file: {rel}')
    for rel in sorted(manifest_files - actual_files):
        errors.append(f'manifest lists missing file: {rel}')
    for rel, meta in manifest.get('files', {}).items():
        path = ROOT / rel
        if not path.is_file():
            errors.append(f'missing: {rel}')
            continue
        actual = sha(path)
        if actual != meta.get('sha256'):
            errors.append(f'hash mismatch: {rel}')
        if path.stat().st_size != int(meta.get('size', -1)):
            errors.append(f'size mismatch: {rel}')
    preservation = manifest.get('preservation', {})
    if preservation.get('strategy_signal_fingerprints') != preservation.get('expected_strategy_signal_fingerprints'):
        errors.append('strategy signal fingerprint mismatch in manifest')
    expected_services = ['moneyflow', 'freqtrade', 'execution-sidecar', 'telegram-broker']
    if manifest.get('runtime_scope', {}).get('services') != expected_services:
        errors.append('manifest runtime service topology is not the canonical four-service Bitcoin stack')
    expected_modes = (
        ['simulation', 'live']
        if release_mode == 'live'
        else ['simulation', 'testnet']
    )
    if manifest.get('supported_execution_modes') != expected_modes:
        errors.append('manifest execution modes do not match package mode')
    # Recompute the canonical fingerprints from the installed strategy file so a
    # manifest edited in transit cannot vouch for a modified strategy.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from services.common.strategy_fingerprint import fingerprints
    live = fingerprints(
        ROOT / 'freqtrade/user_data/strategies/IctSmcStrategy.py', 'IctSmcStrategy',
        ['populate_indicators_5m', 'populate_indicators', 'populate_entry_trend', 'populate_exit_trend'],
    )
    if live != preservation.get('expected_strategy_signal_fingerprints'):
        errors.append('installed strategy signal methods differ from expected fingerprints')
    release_line = (ROOT / 'RELEASE_SHA256.txt').read_text(encoding='utf-8').split()[0]
    if release_line != sha(manifest_path):
        errors.append('manifest release hash mismatch')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        raise SystemExit(1)
    print(f"manifest verified: {len(manifest_files)} files; exact file set matched")


if __name__ == '__main__':
    main()
