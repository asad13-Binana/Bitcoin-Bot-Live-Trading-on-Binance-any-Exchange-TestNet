"""Deployment-only capacity policy; no credentials, orders or cloud changes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess


def validate(profile: str, mode: str, instance: str, phase: str,
             memory_mib: int, swap_mib: int, cpu_count: int, free_gib: int,
             architecture: str, projects: list[str]) -> None:
    if instance not in {"bitcoin-testnet", "bitcoin-live"}:
        raise ValueError("invalid Bitcoin instance")
    allowed = {"simulation", "testnet"} if instance.endswith("testnet") else {"simulation", "live"}
    if mode not in allowed or phase not in {"bootstrap", "install"}:
        raise ValueError("invalid package mode or capacity phase")
    if profile == "oracle-four-bot":
        required_memory, required_disk = 11264, 80 if phase == "bootstrap" else 8
        if architecture not in {"aarch64", "arm64"}:
            raise ValueError("Oracle A1 profile requires ARM64")
        required_total = 14336
    elif profile == "single-bot-experiment":
        required_memory, required_disk, required_total = 7168, 12 if phase == "bootstrap" else 8, 10968
        if mode == "live":
            raise ValueError("single-bot experiment forbids LIVE execution")
        if architecture not in {"x86_64", "amd64", "aarch64", "arm64"}:
            raise ValueError("unsupported experimental host architecture")
        if any(project != instance for project in projects):
            raise ValueError("single-bot experiment rejects other running containers/projects")
    else:
        raise ValueError("unknown deployment profile")
    measurements = (memory_mib, swap_mib, cpu_count, free_gib)
    if any(type(value) is not int or value < 0 for value in measurements):
        raise ValueError("invalid capacity measurement")
    if memory_mib < required_memory or cpu_count < 2 or free_gib < required_disk:
        raise ValueError(f"insufficient host capacity: require {required_memory} MiB RAM, "
                         f"2 CPUs and {required_disk} GiB free for {profile}/{phase}")
    # Bootstrap provisions swap only after these physical-resource checks.
    if phase == "install" and (swap_mib < 3800 or memory_mib + swap_mib < required_total):
        raise ValueError("insufficient swap/total memory; complete host setup first")


def running_projects(docker: str) -> list[str]:
    result = subprocess.run([docker, "ps", "--format", "{{json .}}"],
                            capture_output=True, text=True, timeout=30, check=True)
    projects = []
    for line in result.stdout.splitlines():
        row = json.loads(line)
        labels = dict(item.split("=", 1) for item in row.get("Labels", "").split(",") if "=" in item)
        projects.append(labels.get("com.docker.compose.project", ""))
    return projects


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--phase", choices=("bootstrap", "install"), required=True)
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("host capacity check requires root on Linux")
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.split()[0]) // 1024
    docker = shutil.which("docker")
    if docker is None and args.phase == "install":
        raise SystemExit("Docker unavailable; capacity/occupancy cannot be verified")
    try:
        projects = running_projects(docker) if docker and args.profile == "single-bot-experiment" else []
        validate(args.profile, args.mode, args.instance, args.phase,
                 memory["MemTotal"], memory["SwapTotal"], os.cpu_count() or 0,
                 shutil.disk_usage("/").free // (1024 ** 3), platform.machine(), projects)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        raise SystemExit(f"host capacity check failed: {exc}") from exc
    print(f"capacity profile passed: {args.profile}/{args.phase}; not a runtime certification")


if __name__ == "__main__":
    main()
