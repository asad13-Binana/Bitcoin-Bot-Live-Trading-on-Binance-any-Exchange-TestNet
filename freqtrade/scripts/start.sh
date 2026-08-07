#!/usr/bin/env bash
set -euo pipefail
echo 'BLOCKED: the Freqtrade subtree is signal-only and cannot be started independently.' >&2
echo 'Start the Bitcoin Bot stack from the repository root with docker-compose.yml.' >&2
exit 1
