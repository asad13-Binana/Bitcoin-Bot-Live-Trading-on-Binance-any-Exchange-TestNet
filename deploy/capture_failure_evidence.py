"""Best-effort bounded failure evidence before rollback; no raw logs or secrets.

Only fixed Docker state fields and allowlisted error categories survive. Never
serialize Config.Env, command arguments, labels, health output or log messages.
The caller must time-bound this helper and continue rollback if capture fails.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

SERVICES = {"moneyflow", "freqtrade", "execution-sidecar", "telegram-broker"}
ERRORS = ("PermissionError", "ModuleNotFoundError", "ImportError", "FileNotFoundError",
          "RuntimeError", "TimeoutError", "ConnectionError", "OSError")


def bounded_command(args, limit=65536):
    """Cap wall time and bytes before parsing even a maliciously long log line."""
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    data = bytearray()
    deadline = time.monotonic() + 3
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while len(data) < limit and time.monotonic() < deadline:
                if not ready.select(timeout=0.1):
                    continue
                chunk = os.read(process.stdout.fileno(), min(4096, limit - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        process.stdout.close()
    return data.decode("utf-8", errors="replace")


def categories(text):
    return [kind for kind in ERRORS if re.search(r"\b" + kind + r"\b", text)]


def project_state(record, project, tag):
    """Allowlist only: Docker metadata/log messages may contain arbitrary secrets."""
    config = record.get("Config") or {}
    labels = config.get("Labels") or {}
    service = labels.get("com.docker.compose.service")
    if labels.get("com.docker.compose.project") != project or service not in SERVICES:
        raise ValueError("container is not a known service in this project")
    image = config.get("Image", "")
    expected = {f"{project}-services:{tag}", f"{project}-freqtrade:{tag}"}
    if image not in expected:
        raise ValueError("container image is not the attempted release")
    state = record.get("State") or {}
    health = state.get("Health") or {}
    history = []
    for entry in (health.get("Log") or [])[-5:]:
        history.append({"exit_code": integer(entry.get("ExitCode")),
                        "error_categories": categories(str(entry.get("Output", ""))[:65536])})
    image_id = record.get("Image", "")
    return {"service": service, "image_tag": image,
            "image_id": image_id if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) else None,
            "exit_code": integer(state.get("ExitCode")), "oom_killed": state.get("OOMKilled") is True,
            "status": state.get("Status") if state.get("Status") in
            {"created", "running", "paused", "restarting", "removing", "exited", "dead"} else "unknown",
            "health": health.get("Status") if health.get("Status") in
            {"starting", "healthy", "unhealthy"} else "unknown",
            "health_history": history,
            "state_error_categories": categories(str(state.get("Error", ""))[:65536])}


def integer(value):
    return value if type(value) is int and -255 <= value <= 255 else None


def private_directory(path):
    # /var/log is root-controlled; reject any link, foreign owner or writable parent.
    for parent in (Path("/"), Path("/var"), Path("/var/log")):
        stat = parent.lstat()
        if parent.is_symlink() or not parent.is_dir() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise ValueError("unsafe diagnostics parent")
    path.mkdir(mode=0o700, exist_ok=True)
    stat = path.lstat()
    if path.is_symlink() or not path.is_dir() or (stat.st_uid, stat.st_gid) != (0, 0) or stat.st_mode & 0o777 != 0o700:
        raise ValueError("diagnostics directory must be root:root 0700")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", choices=("bitcoin-testnet", "bitcoin-live"))
    parser.add_argument("release_hash")
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{64}", args.release_hash):
        raise SystemExit("root and a full release hash required")
    tag = "bitcoin-" + args.release_hash[:16]
    report = {"schema_version": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
              "project": args.project, "release_hash": args.release_hash,
              "step": "rollback_before_container_removal", "containers": [],
              "raw_logs_retained": False, "capture_incomplete": False}
    ids = bounded_command(["docker", "ps", "-aq", "--no-trunc", "--filter",
                           f"label=com.docker.compose.project={args.project}"], 8192).split()
    report["capture_incomplete"] = len(ids) > 4
    for identifier in ids[:4]:
        try:
            if not re.fullmatch(r"[0-9a-f]{64}", identifier):
                raise ValueError("invalid container ID")
            record = json.loads(bounded_command(["docker", "inspect", identifier]))[0]
            row = project_state(record, args.project, tag)
            row["container_id"] = identifier
            logs = bounded_command(["docker", "logs", "--tail", "80", identifier])
            row["log_error_categories"] = categories(logs)
            # Preserve safe traceback locations, never source lines or exception text.
            row["services_import_permission_error"] = (
                "PermissionError" in logs and "/app/services/__init__.py" in logs)
            report["containers"].append(row)
        except (ValueError, KeyError, IndexError, TypeError, OSError):
            report["capture_incomplete"] = True
    directory = Path("/var/log") / (args.project + "-deployment")
    private_directory(directory)
    name = datetime.now(timezone.utc).strftime("failure-%Y%m%dT%H%M%S.%fZ-") + args.release_hash[:16] + ".json"
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("Sanitised failure evidence saved:", directory / name)


if __name__ == "__main__":
    main()
