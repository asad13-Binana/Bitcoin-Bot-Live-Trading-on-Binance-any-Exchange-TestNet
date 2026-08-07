from __future__ import annotations

"""Host-side verifier for Ed25519-signed, release-bound live promotion evidence."""

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.execution_sidecar.live_evidence import (  # noqa: E402
    LiveEvidenceError,
    verify_live_evidence,
)
from services.common.market_policy import allowed_quotes_from_env  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_live_evidence.py LIVE_EVIDENCE.json")
    manifest = json.loads((ROOT / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    release_hash = (ROOT / "RELEASE_SHA256.txt").read_text(encoding="utf-8").split()[0]
    strategy = manifest.get("preservation", {}).get("strategy_signal_fingerprints")
    if not isinstance(strategy, dict) or not strategy:
        raise SystemExit("manifest lacks strategy fingerprints")
    strategy_sha256 = manifest.get("files", {}).get(
        "freqtrade/user_data/strategies/IctSmcStrategy.py", {}).get("sha256")
    if not isinstance(strategy_sha256, str) or len(strategy_sha256) != 64:
        raise SystemExit("manifest lacks the full strategy file hash")
    try:
        active_pair_path = Path(
            os.environ.get(
                "ACTIVE_PAIR_FILE", "/var/lib/bitcoin-bot/shared/pair/active_pair.json"
            )
        )
        active_pair = json.loads(active_pair_path.read_text(encoding="utf-8"))
        minimum_remaining = int(os.environ.get("LIVE_EVIDENCE_MIN_REMAINING_SECONDS", "0"))
        verify_live_evidence(
            release_hash=release_hash,
            strategy_fingerprints=strategy,
            strategy_file_sha256=strategy_sha256,
            active_pair_state=active_pair,
            allowed_quotes=allowed_quotes_from_env(),
            path=Path(sys.argv[1]),
            min_remaining_seconds=minimum_remaining,
        )
    except (LiveEvidenceError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"live evidence rejected: {exc}") from exc
    print("signed live promotion evidence verified")


if __name__ == "__main__":
    main()
