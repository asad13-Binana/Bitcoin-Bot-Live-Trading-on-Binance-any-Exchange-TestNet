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
    elif profile == "shared-testnet-experiment":
        required_memory, required_disk, required_total = 7168, 12 if phase == "bootstrap" else 8, 10968
        if instance != "bitcoin-testnet" or mode not in {"simulation", "testnet"}:
            raise ValueError("shared experiment permits only the Bitcoin Testnet package")
        if architecture not in {"x86_64", "amd64", "aarch64", "arm64"}:
            raise ValueError("unsupported experimental host architecture")
        if any(project not in {"bitcoin-testnet", "binana-testnet"} for project in projects):
            raise ValueError("shared experiment permits only Bitcoin and BINANA Testnet projects")
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


def validate_shared_runtime(containers: list[dict], memory_mib: int, cpu_count: int) -> None:
    """Budget the cohost while replacing Bitcoin's legacy stack with bounded services."""
    cohost_memory = cohost_cpu = 0
    cohost_seen = cohost_executor = False
    for container in containers:
        config = container.get("Config") or {}
        labels = config.get("Labels") or {}
        project = labels.get("com.docker.compose.project", "")
        if project not in {"bitcoin-testnet", "binana-testnet"}:
            raise ValueError("unrecognised cohost in shared Testnet experiment")
        environment = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
        mode = environment.get("EXECUTION_MODE")
        if mode is not None and mode not in {"simulation", "testnet"}:
            raise ValueError("shared experiment rejects non-Testnet execution")
        if project == "bitcoin-testnet":
            continue  # Replacement Compose has 1200 MiB / 0.45 CPU hard limits.
        cohost_seen = True
        if labels.get("com.docker.compose.service") == "execution-sidecar":
            cohost_executor = mode in {"simulation", "testnet"}
        host = container.get("HostConfig") or {}
        limits = [host.get(key) for key in ("Memory", "NanoCpus", "PidsLimit")]
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("BINANA cohost must have explicit memory, CPU and PID limits")
        cohost_memory += limits[0]
        cohost_cpu += limits[1]
    if cohost_seen and not cohost_executor:
        raise ValueError("cannot verify the cohost Testnet executor")
    # Reserve 1200 MiB for Bitcoin, 512 MiB for monitoring and 2 GiB for the OS/builds.
    if cohost_memory + 3760 * 1024**2 > memory_mib * 1024**2:
        raise ValueError("shared Testnet memory reservations exceed physical capacity")
    # Bitcoin 0.45 CPU plus 0.5 CPU headroom; do not rely on the cohost's idle snapshot.
    if cohost_cpu + 950_000_000 > cpu_count * 1_000_000_000:
        raise ValueError("shared Testnet CPU reservations exceed physical capacity")


def running_container_snapshot(docker: str) -> list[dict]:
    ids = subprocess.run([docker, "ps", "-q"], capture_output=True, text=True,
                         timeout=30, check=True).stdout.split()
    if not ids:
        return []
    return json.loads(subprocess.run([docker, "inspect", *ids], capture_output=True,
                                     text=True, timeout=30, check=True).stdout)


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
        projects = running_projects(docker) if docker and args.profile in {
            "single-bot-experiment", "shared-testnet-experiment"} else []
        validate(args.profile, args.mode, args.instance, args.phase,
                 memory["MemTotal"], memory["SwapTotal"], os.cpu_count() or 0,
                 shutil.disk_usage("/").free // (1024 ** 3), platform.machine(), projects)
        if args.profile == "shared-testnet-experiment" and docker:
            validate_shared_runtime(running_container_snapshot(docker), memory["MemTotal"], os.cpu_count() or 0)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        raise SystemExit(f"host capacity check failed: {exc}") from exc
    print(f"capacity profile passed: {args.profile}/{args.phase}; not a runtime certification")


if __name__ == "__main__":
    main()
