"""Small systemd ExecCondition helper for explicit enable/disable controls."""
from __future__ import annotations

import os
import sys

from .api.configuration import CONFIG, loopback_http_url, secret_is_configured


def check(kind: str) -> tuple[bool, str]:
    if kind == "api":
        if not CONFIG.enabled:
            return False, "MONITOR_ENABLED is false"
        errors = CONFIG.runtime_errors()
        return (not errors, "; ".join(errors) if errors else "ok")
    if kind == "telegram":
        if not CONFIG.telegram_reports_enabled:
            return False, "TELEGRAM_REPORTS_ENABLED is false"
        if not loopback_http_url(os.getenv("MONITOR_URL", "")):
            return False, "MONITOR_URL must be loopback HTTP"
        for name, minimum in (
            ("MONITOR_TOKEN", 32),
            ("TELEGRAM_MONITOR_BOT_TOKEN", 32),
            ("TELEGRAM_MONITOR_CHAT_ID", 1),
        ):
            if not secret_is_configured(os.getenv(name, ""), minimum=minimum):
                return False, f"{name} is missing or a placeholder"
        return True, "ok"
    return False, "kind must be api or telegram"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ok, reason = check(argv[0] if argv else "")
    if not ok:
        print(reason)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
