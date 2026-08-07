#!/usr/bin/env bash
set -euo pipefail
echo 'BLOCKED: do not manage the runtime from the Freqtrade-only subtree.' >&2
echo 'Use the root docker-compose.yml or the root rollback/emergency procedures.' >&2
exit 1
