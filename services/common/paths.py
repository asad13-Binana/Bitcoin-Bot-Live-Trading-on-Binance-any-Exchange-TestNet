from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(os.getenv('SHARED_ROOT', '/app/shared'))
SIGNAL_INBOX = Path(os.getenv('SIGNAL_INBOX', ROOT / 'signals/inbox'))
SIGNAL_PROCESSED = Path(os.getenv('SIGNAL_PROCESSED', ROOT / 'signals/processed'))
SIGNAL_REJECTED = Path(os.getenv('SIGNAL_REJECTED', ROOT / 'signals/rejected'))
ACTIVE_PAIR_FILE = Path(os.getenv('ACTIVE_PAIR_FILE', ROOT / 'pair/active_pair.json'))
PAIRLIST_FILE = Path(os.getenv('PAIRLIST_FILE', ROOT / 'pair/current_pairlist.json'))
FREQTRADE_ACTIVE_CONFIG = Path(os.getenv(
    'FREQTRADE_ACTIVE_CONFIG', ROOT / 'pair/freqtrade-active.json'))
ELIGIBLE_PAIRS_FILE = Path(os.getenv(
    'ELIGIBLE_PAIRS_FILE', ROOT / 'pair/eligible_pairs.json'))
FREQTRADE_HEARTBEAT_FILE = Path(os.getenv(
    'FREQTRADE_HEARTBEAT_FILE', ROOT / 'freqtrade/signal_seam_heartbeat.json'))
MONEYFLOW_FILE = Path(os.getenv('MONEYFLOW_FILE', ROOT / 'moneyflow/latest.json'))
RUNTIME = Path(os.getenv('RUNTIME_DIR', ROOT / 'runtime'))
COMMAND_INBOX = Path(os.getenv('COMMAND_INBOX', ROOT / 'commands/inbox'))
COMMAND_RESULTS_DIR = Path(os.getenv('COMMAND_RESULTS_DIR', ROOT / 'command_results'))
AUDIT_DIR = Path(os.getenv('AUDIT_DIR', ROOT / 'audit'))

for p in [SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED, ACTIVE_PAIR_FILE.parent,
          MONEYFLOW_FILE.parent, RUNTIME, COMMAND_INBOX, COMMAND_RESULTS_DIR, AUDIT_DIR]:
    # Best effort: services with read-only mounts cannot (and must not)
    # create directories owned by other services; the owning service and the
    # installer guarantee their own paths.
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
