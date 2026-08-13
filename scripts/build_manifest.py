from __future__ import annotations
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.strategy_fingerprint import fingerprints  # noqa: E402

EXCLUDE = {
    'RELEASE_MANIFEST.json', 'RELEASE_SHA256.txt',
}
EXCLUDE_PARTS = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache', '.hypothesis'}

SIGNAL_METHODS = [
    'populate_indicators_5m', 'populate_indicators',
    'populate_entry_trend', 'populate_exit_trend',
]
# Canonical source/token fingerprints (interpreter-independent; see
# services/common/strategy_fingerprint.py). These correspond to the exact
# reviewed signal-method text and avoid interpreter-dependent AST dumps.
EXPECTED_SIGNAL_FINGERPRINTS = {
    'populate_indicators_5m': {
        'source_sha256': '3c01ceda9807efbcf63b32297879c04af6cc65744387dd4821a2ed1328025969',
        'token_sha256': '071a394a70a2370b05700ba8e58bfbbeec6d66fb48ca78995dc8fc5dd98e265b',
    },
    'populate_indicators': {
        'source_sha256': '11c39597e4c7f535808e36db290f36f8908dc0e3b98578d9d92dd1e8abd93526',
        'token_sha256': 'ab8b017314652ed1d08f9c23813eade8a79d5a67c0831a624095f315ef6ecd59',
    },
    'populate_entry_trend': {
        # The indicator/condition formula is unchanged from the parent bot.
        # Its former Sharia eligibility tail was deliberately removed for the
        # Bitcoin-only mandate; exact-pair emission is enforced in bot_loop_start.
        'source_sha256': '8913eb105ed4e5b7195482e89510cf1b2d0db3f13b7dba3bb213ce0b78c0dc28',
        'token_sha256': '8316280ffc4b9c72c0a5687df071a5713ac5bdae0d1dec7b4b795351d70356fc',
    },
    'populate_exit_trend': {
        'source_sha256': 'fdd2c099edf44b4db4408a5aec183f2c493c95db34d66975697dc6a50c50c196',
        'token_sha256': 'dc79eb4e19e4c68db8c3e877c671a115edb11f87f40cf1a9b34fbb0c457ccd7f',
    },
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    mode_path = ROOT / 'RELEASE_MODE'
    package_mode = mode_path.read_text(encoding='utf-8').strip() if mode_path.is_file() else 'unconfigured'
    if package_mode not in {'live', 'testnet'}:
        raise SystemExit('RELEASE_MODE must be live or testnet before building the manifest')
    files = {}
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in EXCLUDE or any(part in EXCLUDE_PARTS for part in path.parts):
            continue
        files[rel] = {'sha256': sha(path), 'size': path.stat().st_size}
    strategy = ROOT / 'freqtrade/user_data/strategies/IctSmcStrategy.py'
    manifest = {
        'release': f'BITCOIN-BOT-1.1-{package_mode.upper()}',
        'package_mode': package_mode,
        'lineage': ('audited Binance Bot safety baseline + reviewed Bitcoin Bot '
                    'upgrade pack; Bitcoin-only consolidation'),
        'manifest_format': 2,
        'safety_default': 'simulation',
        'supported_execution_modes': (
            ['simulation', 'live']
            if package_mode == 'live'
            else ['simulation', 'testnet']
        ),
        'live_certified': False,
        'source_archives': {
            'binance-bot-live-trading-CLAUDE.zip': '4945ba6fa0ae82a6bf84f836f5308d6f9f9473975bc17c579d719e291bc456b4',
            'bitcoin_bot.zip': '8399b74b62e2e673618b3e21dfab43bebfb661be6f3c05b2a792d2556480a7e3',
        },
        'runtime_scope': {
            'services': ['moneyflow', 'freqtrade', 'execution-sidecar', 'telegram-broker'],
            'market': 'Binance Spot',
            'base_asset': 'BTC',
            'pair_count': 1,
            'asset_selection': (
                'one explicitly owner-selected, current Binance BTC-base Spot pair'
            ),
            'excluded_capabilities': [
                'multi-asset discovery or ranking',
                'non-trading asset eligibility classification',
            ],
        },
        'preservation': {
            'strategy_signal_fingerprints': fingerprints(strategy, 'IctSmcStrategy', SIGNAL_METHODS),
            'expected_strategy_signal_fingerprints': EXPECTED_SIGNAL_FINGERPRINTS,
            'fingerprint_method': 'canonical source segment + logical token stream '
                                  '(services/common/strategy_fingerprint.py); '
                                  'interpreter-independent, Python 3.10-3.13',
        },
        'files': files,
    }
    payload = json.dumps(manifest, indent=2, sort_keys=True) + '\n'
    # Write raw bytes so the on-disk manifest matches the hashed payload on
    # every OS. Path.write_text uses text mode and would translate '\n' to
    # '\r\n' on Windows, making sha256(file) != sha256(payload).
    (ROOT / 'RELEASE_MANIFEST.json').write_bytes(payload.encode('utf-8'))
    release_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    (ROOT / 'RELEASE_SHA256.txt').write_bytes(
        (release_hash + '  RELEASE_MANIFEST.json\n').encode('utf-8'))
    print(release_hash)

if __name__ == '__main__':
    main()
