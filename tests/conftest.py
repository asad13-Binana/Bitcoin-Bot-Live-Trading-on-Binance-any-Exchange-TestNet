from __future__ import annotations

import os
import tempfile
from pathlib import Path


# Keep generated property-test state outside the immutable release tree.
os.environ.setdefault(
    "HYPOTHESIS_STORAGE_DIRECTORY",
    str(Path(tempfile.gettempdir()) / "bitcoin-testnet-hypothesis"),
)
