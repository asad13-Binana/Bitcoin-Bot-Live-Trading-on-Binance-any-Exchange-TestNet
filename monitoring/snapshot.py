#!/usr/bin/env python3
"""Root-owned helper that emits sanitized Docker status for the botmon user.

The monitor service itself never receives Docker-socket access.  This fixed,
root-owned helper has no order or container-mutation subcommands.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT = "bitcoin-bot"
DEFAULT_OUTPUT = Path(
    "/var/lib/bitcoin-bot/shared/runtime/container_status.json"
)


def collect() -> dict:
    docker = "/usr/bin/docker"
    if not Path(docker).is_file():
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "error": "docker_missing", "containers": []}
    listed = subprocess.run(
        [docker, "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if listed.returncode != 0:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "error": "docker_query_failed", "containers": []}
    identifiers = listed.stdout.split()
    if not identifiers:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "containers": []}
    inspected = subprocess.run(
        [docker, "inspect", *identifiers], capture_output=True, text=True,
        timeout=15, check=False,
    )
    if inspected.returncode != 0:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "error": "docker_inspect_failed", "containers": []}
    try:
        raw = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        raw = []
    containers = []
    for item in raw:
        state = item.get("State") or {}
        labels = (item.get("Config") or {}).get("Labels") or {}
        health = (state.get("Health") or {}).get("Status") or "none"
        containers.append({
            "service": labels.get("com.docker.compose.service"),
            "name": str(item.get("Name") or "").lstrip("/"),
            "status": state.get("Status") or "unknown",
            "health": health,
            "restart_count": int(item.get("RestartCount") or 0),
            "started_at": state.get("StartedAt"),
        })
    containers.sort(key=lambda value: str(value.get("service")))
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "containers": containers}


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".container-status.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    resolved = args.output.resolve()
    allowed = DEFAULT_OUTPUT.parent.resolve()
    if resolved.parent != allowed:
        raise SystemExit(f"output must be directly inside {allowed}")
    atomic_write(resolved, collect())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
