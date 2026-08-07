from __future__ import annotations
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache'}
SKIP_FILES = {'secret_scan.py'}
# High-confidence token/key formats.
PATTERNS = [
    re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}'),
    re.compile(r'\bghp_[A-Za-z0-9]{30,}'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{40,}'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}\b'),
]
SENSITIVE_NAMES = {
    'BINANCE_API_KEY', 'BINANCE_API_SECRET', 'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_MONITOR_BOT_TOKEN', 'MONITOR_TOKEN',
    'SIGNAL_HMAC_KEY', 'COMMAND_HMAC_KEY', 'LIVE_EVIDENCE_PRIVATE_KEY',
    'FREQTRADE_API_PASSWORD', 'FREQTRADE_API_JWT_SECRET', 'FREQTRADE_API_WS_TOKEN',
    'ORACLE_SSH_PRIVATE_KEY', 'OPENAI_API_KEY', 'GITHUB_TOKEN',
    'COINGECKO_API_KEY', 'COINMARKETCAP_API_KEY',
}
ASSIGNMENT = re.compile(
    r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*(?:=|:)\s*["\']?([^"\'\s#]*)'
)
PLACEHOLDER_MARKERS = (
    'CHANGE_ME', 'GENERATE_', 'YOUR_', 'REPLACE_', '<', '$(', 'EXAMPLE',
    'OS.GETENV', 'OS.ENVIRON', 'GETENV(', 'ENVIRON[',
)
SHELL_VARIABLE_REFERENCE = re.compile(
    r'^\$(?:[A-Za-z_][A-Za-z0-9_]*|'
    r'\{[A-Za-z_][A-Za-z0-9_]*(?:(?::?[-+?])[^}]*)?\})$'
)
# ASSIGNMENT intentionally stops at whitespace.  A Compose expression such as
# ``${KEY:?set KEY}`` is therefore captured as ``${KEY:?set``.  That prefix is
# still an indirect environment reference, not credential material.
SHELL_REQUIRED_REFERENCE_PREFIX = re.compile(
    r'^\$\{[A-Za-z_][A-Za-z0-9_]*:\?[A-Za-z0-9_.-]+$'
)


def _placeholder(value: str) -> bool:
    value = value.strip()
    return (
        not value
        or SHELL_VARIABLE_REFERENCE.fullmatch(value) is not None
        or SHELL_REQUIRED_REFERENCE_PREFIX.fullmatch(value) is not None
        or any(marker in value.upper() for marker in PLACEHOLDER_MARKERS)
    )


def scan(root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in root.rglob('*'):
        if not path.is_file() or path.name in SKIP_FILES or any(x in path.parts for x in SKIP_PARTS):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            match = ASSIGNMENT.match(line)
            if match and match.group(1) in SENSITIVE_NAMES and not _placeholder(match.group(2)):
                findings.append(f'{path.relative_to(root)}:{lineno}:non-placeholder {match.group(1)}')
                continue
            if any(pattern.search(line) for pattern in PATTERNS):
                # A token-shaped example is still unsafe unless the matched value
                # itself is visibly a placeholder; variable names do not suppress it.
                if not any(marker in line.upper() for marker in PLACEHOLDER_MARKERS):
                    findings.append(f'{path.relative_to(root)}:{lineno}:token pattern')
    return findings


def main():
    findings = scan(ROOT)
    if findings:
        print('possible secrets:\n' + '\n'.join(findings), file=sys.stderr)
        raise SystemExit(1)
    print('secret scan: no high-confidence secrets or populated sensitive assignments found')


if __name__ == '__main__':
    main()
